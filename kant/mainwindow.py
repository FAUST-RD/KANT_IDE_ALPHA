"""Application orchestration for KANT IDE.

AI navigation:
- UI construction and global shortcuts are near the top of ``MainWindow``.
- Project/xref lifecycle precedes ``# ---- project tree``.
- File tabs, open/save, diagnostics/LSP, section rendering, and workspace menus follow in that order.
- Reusable widgets belong in ``widgets.py``; filesystem mutation belongs in ``workspace.py``;
  deterministic scans/parsing belong in their service modules.

This module owns coordination and cache invalidation, not the underlying parser, persistence,
workspace safety, or graph algorithms.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html import escape as html_escape
from pathlib import Path

import shiboken6

from PySide6.QtCore import QFileSystemWatcher, QPoint, Qt, QSettings, QSize, Signal, QTimer
from PySide6.QtGui import (
    QColor, QFont, QKeySequence, QPainter, QShortcut, QTextCharFormat, QTextCursor, QTextDocument,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QPushButton, QScrollArea,
    QSizeGrip, QSplitter, QStackedWidget, QTabBar, QTabWidget, QTextEdit, QToolButton,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from kant import theme
from kant.theme import set_theme
from kant.icons import draw_icon
from kant.i18n import install_ui_language, translate_text
from kant.model import (
    Run, Node, parse_kant, serialize_kant, read_top_level_label,
    KantParseError, ELEMENT_LANGUAGES, build_new_element_node,
)
from kant.fileio import file_fingerprint, write_file_atomic
from kant.syntax import (
    run_command_for_path, _quote_arg, audit_kant_headers, repair_kant_error,
)
from kant import skeleton
from kant.xref import build_xref, _walk_nodes
from kant.groupings import (
    load_reconciled_groupings, save_groupings, new_grouping, add_member, member_hint,
)
from kant.lsp import LspClient
from kant.dialogs import IdeDialogsMixin
from kant.gitops import GitOpsMixin
from kant.project_panel import ProjectPanelMixin
from kant.file_ops_panel import FileOpsMixin
from kant.lsp_panel import LspPanelMixin
from kant.projectops import (
    _kant_error_lookup, definition_locations, iter_kant_tagged_files, iter_project_text_files,
    reference_locations, scan_project_replace, search_project,
)
from kant.pyenv import (
    dependency_file, detect_venvs, has_module, interpreter_label, interpreter_version,
    load_interpreter, save_interpreter,
)
from kant.workspace import ROLE_PATH, WorkspaceMixin, discard_snapshot, rollback_snapshot
from kant.widgets import (
    CodeEdit, TerminalPane, ClaudePane, ConversationSidebar, CollapsibleSection, LeafSection,
    ProjectTree, ScanlineOverlay, make_app_icon, make_app_pixmap, TitleBar, FileTab,
    MODEL_DEFAULT, _tag_header_html,
    set_vim_mode, vim_mode_enabled,
    _write_system_prompt_file, _KantTabBar,
)
from kant.mappa import XrefMapDialog


ROLE_KIND = Qt.UserRole
# ROLE_PATH (Qt.UserRole + 1) lives in kant/workspace.py — workspace's tree-item rename/delete
# code needs it too, and mainwindow imports workspace (not the other way around)
ROLE_UID = Qt.UserRole + 2
ROLE_LINE = Qt.UserRole + 5
ROLE_TEXT = Qt.UserRole + 6
ROLE_KEY = Qt.UserRole + 7   # xref element key '<rel_path>::<uid>', on Incoming/Outgoing list items
# document order (pre-order over tagged nodes, matching _nodes_in_order) of a 'section' tree item —
# the reliable fallback when a legacy (no #id) file's uid doesn't survive _open_file's own reparse
ROLE_ORDER = Qt.UserRole + 8


# [FN CATEGORY] _write_kant_fill_markdown — writes the blanks listing + fill instructions to a
# temp .md file instead of folding them into the visible chat prompt — the same "the path is
# named in the prompt, the CLI's own Read tool opens it" delivery ClaudePane._attach_files already
# uses for user-attached documents, so a listing of dozens of elements never turns into one huge
# command line. Shared by the single-file button (_ai_fill_kant_blanks) and the project-wide
# launcher (_launch_kant_fill_blanks) — same WHAT/HOW framing and the same "structure is already
# correct, don't touch it" guardrail either way.
# [FN] _write_kant_fill_markdown — writes a fill-blanks instruction file, returns its path
# [FN OPEN] _write_kant_fill_markdown
def _write_kant_fill_markdown(intro, listing):
    body = (
        f'# Campi KANT da compilare\n\n{intro}\n\n{listing}\n\n'
        '## Istruzioni\n\n'
        'Per ciascuno degli elementi elencati sopra, compila SOLO il testo mancante, in due parti:\n\n'
        "1. **Riga descrittiva** (subito sotto CATEGORY, l'unica riga con solo `[TAG] Nome`): "
        'spiegazione di COSA fa quel pezzo di codice, max 8 parole.\n'
        '2. **Riga CATEGORY**: spiegazione di COME funziona, non semplicemente cosa è.\n\n'
        'Non aggiungere, spostare o rinominare marker OPEN/CLOSED, non cambiare tag, nesting o '
        '#id, non toccare elementi che hanno già una descrizione.'
    )
    return _write_system_prompt_file(body)
# [FN CLOSED] _write_kant_fill_markdown


# [FN CATEGORY] MainWindow — wires the project tree, the section view and the toolbar together;
# owns the currently-open file's parsed tree and dirty state
# [FN] MainWindow — the KANT Editor application window
# [FN OPEN] MainWindow
class MainWindow(IdeDialogsMixin, WorkspaceMixin, GitOpsMixin, ProjectPanelMixin, FileOpsMixin, LspPanelMixin, QMainWindow):
    backgroundFinished = Signal(object, object, object)

    def __init__(self, splash=None):
        super().__init__()
        # kant_editor.py's startup splash has Qt.WindowStaysOnTopHint (so it stays above whatever
        # else is loading) — which also meant it rendered on top of any modal dialog shown while
        # THIS constructor runs (_check_crash_recovery/_check_pending_ai_snapshot below), making
        # that dialog unreachable even though it was technically still there and still modal. Hidden
        # right before those checks so the loading icon only "comes back" (via main()'s own
        # splash.finish(window) once the window is shown) after they're resolved, not over them.
        self._startup_splash = splash
        self.setWindowTitle('KANT Editor')
        self.setWindowIcon(make_app_icon())
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.resize(1500, 950)
        self.setFont(QFont('Consolas', 10))

        self.open_tabs = {}  # path -> FileTab, every currently open file
        self._ai_context_page = None  # last coding tab selected by the user
        self.settings = QSettings('KANT', 'KANT Editor')  # persists the dragged column width
        self.ui_language = 'it' if str(self.settings.value('language', 'en')).lower().startswith('it') else 'en'
        self._ui_language = install_ui_language(QApplication.instance(), self.ui_language)
        self._pure_ai_data = self._load_pure_ai_data()
        self._active_ai_conversation_id = None
        self._active_ai_conversation_path = None
        self._pending_ai_conversation_id = None
        self._loading_ai_conversation = False
        self.pure_ai_mode = False
        self._normal_splitter_sizes = None
        self._normal_workspace_sizes = None
        self.night_mode = self.settings.value('nightMode', False, type=bool)
        set_theme(self.night_mode)
        self.project_root_path = None
        self.git_root = None
        self.git_status = {}
        self.view_mode = 'code'  # left project tree: 'code' = KANT-labeled, 'file' = plain filenames
        self.compact_kant_view = False
        self.kant_map_path = None
        self._xref_cache = None  # project cross-reference graph, rebuilt lazily after invalidation
        self._xref_generation = 0
        self._xref_pending_generation = None
        self._last_io_uid = None
        self.map_dialog = None   # internal XrefMapDialog, created on first MAPPA click
        self.git_panel = None    # internal GitPanelDialog, created on first Git click
        self._closing = False
        self._background = ThreadPoolExecutor(max_workers=2, thread_name_prefix='kant')
        self.backgroundFinished.connect(self._finish_background)
        self._git_refresh_pending = False
        self._test_run_pending = False
        self._ai_snapshot = None
        # rel_path -> {'kind': 'created'|'deleted'|'modified', 'additions', 'deletions'} while an AI
        # review is pending — read by _tree_label to color file rows, cleared the moment the review
        # resolves (see workspace._enter_ai_review_mode/_exit_ai_review_mode)
        self._ai_review_status = {}
        self._map_sync_generation = 0
        self._map_sync_running = False
        self._map_sync_rerun_needed = False
        self._flow_sync_generation = 0
        self._flow_sync_running = False
        self._flow_sync_rerun_needed = False
        # how many times each _KANT_ERROR_KB pattern has shown up across validation runs this
        # session — session-only (in-memory), not persisted; reset on restart
        self._kant_error_pattern_counts = Counter()
        self._splitter_save_timers = {}
        self.syntax_timer = QTimer(self)
        self.syntax_timer.setSingleShot(True)
        self.syntax_timer.timeout.connect(self._update_syntax_status)
        self.lsp_diagnostics = {}
        self.lsp_pending_requests = {}
        self.lsp_completion_requests = {}  # request_id -> the specific CodeEdit awaiting its popup
        self.lsp_hover_requests = {}  # request_id -> (edit, global_pos) awaiting its tooltip
        self.lsp_client = LspClient(self)
        self.lsp_client.diagnosticsChanged.connect(self._on_lsp_diagnostics)
        self.lsp_client.responseReceived.connect(self._on_lsp_response)
        self.lsp_client.serverError.connect(self._on_lsp_server_error)
        self.lsp_timer = QTimer(self)
        self.lsp_timer.setSingleShot(True)
        self.lsp_timer.timeout.connect(self._update_lsp_diagnostics)
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

        # deterministic real-time project tracking: QFileSystemWatcher fires on every add/remove/
        # rename inside a watched directory (event-driven, not a polling guess), debounced so a burst
        # of changes (git checkout, a script writing many files) triggers one rebuild, not dozens
        self.fs_watcher = QFileSystemWatcher(self)
        self.fs_watcher.directoryChanged.connect(self._on_fs_directory_changed)
        self.fs_watcher.fileChanged.connect(lambda path: QTimer.singleShot(150, lambda p=path: self._on_fs_file_changed(p)))
        self.fs_refresh_timer = QTimer(self)
        self.fs_refresh_timer.setSingleShot(True)
        self.fs_refresh_timer.timeout.connect(self._refresh_after_fs_change)

        self.shell = QWidget()
        self.shell.setObjectName('appShell')
        self.shell.setStyleSheet(f'#appShell {{ border:1px solid {theme.BORDER}; background:{theme.BG}; }}')
        shell_layout = QVBoxLayout(self.shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        self.title_bar = TitleBar(self)
        shell_layout.addWidget(self.title_bar)
        # the titlebar owns these widgets (they sit to the right of its menu bar); keep short
        # aliases so the rest of MainWindow doesn't need to know that
        self.filename_label = self.title_bar.filename_label
        self.syntax_label = self.title_bar.syntax_label
        # spliced into the title bar's own row (embed_toolbar) rather than a second stacked bar —
        # one consolidated top bar instead of two, each previously painting its own background/border
        self.title_bar.embed_toolbar(self._build_action_toolbar())

        self.stack = QStackedWidget()
        shell_layout.addWidget(self.stack, 1)
        self.setCentralWidget(self.shell)
        self.welcome_page = self._build_welcome_page()
        self.stack.addWidget(self.welcome_page)  # index 0: shown until a folder is opened
        self.stack.addWidget(self._build_main_page())      # index 1: project tree + view
        self.stack.setCurrentIndex(0)
        self._set_project_chrome_visible(False)
        self.size_grip = QSizeGrip(self.shell)
        self.size_grip.setFixedSize(18, 18)
        # app-wide, not tied to any one panel's particular shade of light background — see
        # ScanlineOverlay's own comment for why this can't just be a QSS background-image
        self.scanline_overlay = ScanlineOverlay(self.shell)
        self.scanline_overlay.resize(self.shell.size())
        self.scanline_overlay.raise_()

        self.setStyleSheet(theme.APP_STYLE)
        saved_geometry = self.settings.value('windowGeometry')
        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)
        self._setup_shortcuts()
        if self._startup_splash is not None:
            self._startup_splash.hide()
        self._check_crash_recovery()
        self._check_pending_ai_snapshot()
        self._ui_language.apply(self)

    # [FN CATEGORY] _setup_shortcuts — window-wide keyboard shortcuts (work regardless of which
    # child widget has focus); Ctrl+F is handled separately by _build_find_bar's own Escape shortcut
    # scoped to the find input, since it must not steal Escape everywhere else in the window
    # [FN] _setup_shortcuts — wires the standard save/open/find/run/back key sequences
    # [FN OPEN] _setup_shortcuts
    def _setup_shortcuts(self):
        QShortcut(QKeySequence.Save, self, self._save_file)
        QShortcut(QKeySequence.Open, self, self._open_folder)
        QShortcut(QKeySequence.Find, self, self._show_find_bar)
        # QKeySequence.Close resolves to Ctrl+F4 on Windows (the MDI convention) rather than the
        # Ctrl+W most editors use — bind the literal combo instead of the semantic role here.
        # With tabs, Ctrl+W closes the active tab (the ← arrow is the separate "leave the project
        # entirely" action) — the more familiar meaning once more than one file can be open.
        QShortcut(QKeySequence('Ctrl+W'), self, self._close_active_tab)
        QShortcut(QKeySequence('Ctrl+R'), self, self._run_current_file)
        QShortcut(QKeySequence('F5'), self, self._debug_current_file)
        QShortcut(QKeySequence('Ctrl+Shift+T'), self, self._run_tests)
        QShortcut(QKeySequence('Ctrl+Shift+P'), self, self._show_command_palette)
        QShortcut(QKeySequence('Ctrl+Shift+F'), self, self._search_project)
        QShortcut(QKeySequence('Ctrl+Shift+H'), self, self._replace_project)
        QShortcut(QKeySequence.Undo, self, self._undo_file)
        QShortcut(QKeySequence.Redo, self, self._redo_file)
        QShortcut(QKeySequence('F12'), self, lambda: self._lsp_command('definition'))
        QShortcut(QKeySequence('Shift+F12'), self, lambda: self._lsp_command('references'))
    # [FN CLOSED] _setup_shortcuts

    # [FN] _confirm_close — asks before quitting; its own method (not inlined) so tests can stub
    # just this decision without touching _ide_yes_no's other, unrelated uses elsewhere in the app
    def _confirm_close(self):
        return self._ide_yes_no('Chiudi KANT IDE', 'Sei sicuro di voler chiudere KANT IDE?', danger=True)

    def closeEvent(self, event):
        if not self._confirm_close():
            event.ignore()
            return
        if not self._flush_all_tabs():
            event.ignore()
            return
        self._closing = True
        for timer in (self.syntax_timer, self.lsp_timer, self.fs_refresh_timer):
            timer.stop()
        # QApplication is a process-wide singleton that outlives this window — without this, its
        # focusChanged connection keeps a strong reference to self forever, so this MainWindow (and
        # everything it owns) is never garbage-collected even after close()
        QApplication.instance().focusChanged.disconnect(self._on_focus_changed)
        self._background.shutdown(wait=False, cancel_futures=True)
        for proc in (self.terminal.process, self.claude_pane.process):
            if proc is not None:
                proc.kill()
                proc.waitForFinished(1000)
        self._save_active_ai_conversation()
        self.claude_pane.permission_bridge.stop()
        # an in-flight AI run is rolled back synchronously above, by _finish_ai_review reacting to
        # claude_pane.process finishing during waitForFinished(). If self._ai_snapshot is still set
        # here, it's a review that was deliberately left for manual recovery after an apply/rollback
        # OSError — leave it on disk instead of silently discarding the backup the user was just
        # told was preserved; _check_pending_ai_snapshot offers to resolve it on the next launch.
        self.lsp_client.shutdown()
        self.settings.setValue('windowGeometry', self.saveGeometry())
        self.settings.setValue('session/cleanExit', True)
        self.settings.sync()
        super().closeEvent(event)

    def _run_background(self, work, done):
        if self._closing:
            return
        future = self._background.submit(work)

        def complete(result):
            try:
                value, error = result.result(), None
            except Exception as e:
                value, error = None, e
            if not self._closing:
                self.backgroundFinished.emit(done, value, error)

        future.add_done_callback(complete)

    def _finish_background(self, done, value, error):
        if not self._closing:
            done(value, error)

    # [FN] _debounce_splitter_save — coalesces a splitter's own QSettings write to one per pause in
    # dragging, instead of once per mouse-move tick splitterMoved otherwise fires on
    # [FN OPEN] _debounce_splitter_save
    def _debounce_splitter_save(self, key, splitter):
        if self.pure_ai_mode and key in ('splitterSizes', 'workspaceSplitterSizes'):
            return
        timer = self._splitter_save_timers.get(key)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self.settings.setValue(key, splitter.sizes()))
            self._splitter_save_timers[key] = timer
        timer.start(200)
    # [FN CLOSED] _debounce_splitter_save

    # [FN CATEGORY] _check_crash_recovery — a "session/cleanExit" flag is set False the moment a run
    # starts and only set True again in closeEvent; if it's still False on the NEXT startup, the
    # previous run never reached closeEvent (crash, force-kill, power loss), so this offers to reopen
    # the last known folder/file. The actual file content is never at risk regardless — see _autosave.
    # [FN] _check_crash_recovery — offers to resume the last session after an unclean exit
    # [FN OPEN] _check_crash_recovery
    def _check_crash_recovery(self):
        was_clean = self.settings.value('session/cleanExit', True, type=bool)
        self.settings.setValue('session/cleanExit', False)
        self.settings.sync()
        if was_clean:
            return
        folder = self.settings.value('session/openFolder')
        if not folder or not os.path.isdir(folder):
            return
        if not self._ide_yes_no(
            'Ripristina sessione',
            'La sessione precedente si è interrotta improvvisamente. '
            'Vuoi riprendere a lavorare da dove avevi lasciato?',
        ):
            return
        self._open_project_folder(folder)
        file_path = self.settings.value('session/openFile')
        if file_path and os.path.isfile(file_path):
            self._open_file(file_path)
    # [FN CLOSED] _check_crash_recovery

    # [FN CATEGORY] _check_pending_ai_snapshot — an AI snapshot survives past its own run (in
    # QSettings, set in _prepare_ai_snapshot) whenever it wasn't cleanly resolved: a crash, or a
    # deliberate "leave it for manual recovery" after an apply/rollback OSError. Runs on every
    # startup regardless of the clean-exit flag, since the latter case is a clean exit.
    # [FN] _check_pending_ai_snapshot — offers to resolve a leftover AI snapshot on startup
    # [FN OPEN] _check_pending_ai_snapshot
    def _check_pending_ai_snapshot(self):
        snapshot = self.settings.value('ai/pendingSnapshot')
        project = self.settings.value('ai/pendingSnapshotProject')
        if not snapshot or not project or not os.path.isdir(snapshot):
            self._clear_ai_snapshot_marker()
            return
        choice = self._ide_choice(
            'Revisione AI in sospeso',
            f'E rimasta una revisione AI non conclusa per:\n{project}\n'
            'Vuoi ripristinare i file come erano prima delle modifiche AI, o tenere i file attuali '
            'e scartare solo lo snapshot?',
            [
                ('Decidi più tardi', None, 'Chiudi senza decidere; verra richiesto di nuovo al prossimo avvio'),
                ('Ripristina originali', 'rollback', 'Riporta i file com\'erano prima delle modifiche AI, scartandole'),
                ('Tieni i file attuali', 'discard', 'Mantiene le modifiche AI gia applicate, scarta solo lo snapshot di backup'),
            ],
        )
        if choice is None:
            return
        if choice == 'rollback':
            try:
                skipped_dirs = rollback_snapshot(project, snapshot, theme.IGNORE_DIRS | {'.kant-trash'})
            except OSError as error:
                self._ide_message('Revisione AI in sospeso', f'Impossibile ripristinare: {error}')
                return
            if skipped_dirs:
                self._ide_message(
                    'Revisione AI in sospeso',
                    'Ripristinato, ma alcune cartelle non sono state rimosse (probabilmente non vuote): '
                    + ', '.join(skipped_dirs),
                )
        discard_snapshot(snapshot)
        self._clear_ai_snapshot_marker()
    # [FN CLOSED] _check_pending_ai_snapshot

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'size_grip'):
            self.size_grip.move(self.width() - 22, self.height() - 22)
            self.size_grip.raise_()
        if hasattr(self, 'scanline_overlay'):
            self.scanline_overlay.resize(self.shell.size())
            self.scanline_overlay.raise_()
        self._position_map_tab()
        self._position_claude_tab()
        self._position_map_dialog()
        # deferred: welcome_page is nested inside self.stack (a QStackedWidget), whose own
        # geometry isn't guaranteed updated yet at this exact point in a resize pass — reading its
        # width/height synchronously here returned a stale, too-small size (reproduced live: the
        # button landed mid-card instead of the page's actual bottom-right corner)
        QTimer.singleShot(0, self._position_welcome_theme_btn)

    def _position_claude_tab(self):
        if not hasattr(self, 'claude_tab_btn') or self.pure_ai_mode:
            return
        # centered ON the splitter boundary (half the button's own width on each side), not flush
        # against it — a tall narrow pill straddling the seam between the coding board and the AI
        # pane, "metà dentro e metà fuori". Collapsed, that seam coincides with the shell's own right
        # edge, so half the pill visibly hangs past the last real content — reading as a handle
        # pointing at wherever the (now zero-width) pane would reopen; expanded, both halves sit over
        # real content on either side, no particular "pointing outward" — see _style_claude_tab_button
        x = self.splitter.widget(0).width() - self.claude_tab_btn.width() // 2
        self.claude_tab_btn.move(x, (self.shell.height() - self.claude_tab_btn.height()) // 2)
        self.claude_tab_btn.raise_()

    # [FN CATEGORY] _style_claude_tab_button — the pane-collapsed state is the one that should read
    # as "grab me" — accent-colored pill, full border since it's now floating free over the coding
    # board rather than flush against a real pane edge. Expanded is deliberately understated: it's
    # sitting right on top of live content on both sides, not something that needs to shout.
    # [FN] _style_claude_tab_button — accent pill when collapsed, subdued pill when expanded
    # [FN OPEN] _style_claude_tab_button
    def _style_claude_tab_button(self, collapsed):
        if collapsed:
            self.claude_tab_btn.setStyleSheet(
                f'QPushButton {{ background:{theme.ACCENT}; color:{theme.BG if theme.NIGHT else "#111827"}; '
                f'border:1px solid {theme.ACCENT}; border-radius:{theme.RADIUS}px; font-weight:700; }} '
                f'QPushButton:hover {{ background:{theme.ACCENT}; }}'
            )
        else:
            self.claude_tab_btn.setStyleSheet(
                f'QPushButton {{ background:{theme.PANEL}; color:{theme.TEXT}; '
                f'border:1px solid {theme.BORDER}; border-radius:{theme.RADIUS}px; font-weight:700; }} '
                f'QPushButton:hover {{ color:{theme.ACCENT}; border-color:{theme.ACCENT}; }}'
            )
    # [FN CLOSED] _style_claude_tab_button

    def _position_map_tab(self):
        if not hasattr(self, 'map_tab_btn'):
            return
        # positions relative to whichever widget currently owns it: the shell while closed, the map
        # dialog itself while open — reparenting there is what keeps it clickable (see _open_xref_window)
        parent = self.map_tab_btn.parentWidget()
        if parent is None:
            return
        at_top = parent is self.map_dialog
        x = (parent.width() - self.map_tab_btn.width()) // 2
        # while closed the tab stays on the shell's bottom edge (the top is already the title bar
        # and action toolbar); once MAPPA is open and it's reparented onto the dialog itself, it
        # sits centered on the dialog's own top edge instead — where its removed header row used
        # to be — pointing down at the map content below it
        y = 0 if at_top else parent.height() - self.map_tab_btn.height()
        self.map_tab_btn.move(x, y)
        self.map_tab_btn.raise_()
        self._style_map_tab_button(at_top)

    # [FN CATEGORY] _style_map_tab_button — the "sticking out of the edge" look flips with which
    # edge the tab is actually on: rounded top / flat bottom when it sticks up from the shell's
    # bottom edge, rounded bottom / flat top when it sticks down from the map dialog's top edge.
    # at_top (MAPPA closed) is also the "fully minimized" state — same accent-pill/subdued-pill
    # split _style_claude_tab_button uses, so both drawer tabs read the same "grab me" language when
    # their panel is put away, and both go quiet once the panel is actually showing.
    # [FN] _style_map_tab_button — rounds whichever corners face away from the edge it's stuck to
    # [FN OPEN] _style_map_tab_button
    def _style_map_tab_button(self, at_top):
        radius = self.map_tab_btn.height() // 2
        if at_top:
            self.map_tab_btn.setStyleSheet(
                f'QPushButton {{ background:{theme.ACCENT}; color:#111827; '
                f'border:1px solid {theme.ACCENT}; border-top:none; '
                f'border-bottom-left-radius:{radius}px; border-bottom-right-radius:{radius}px; font-weight:700; }} '
                f'QPushButton:hover {{ background:{theme.ACCENT}; }}'
            )
        else:
            self.map_tab_btn.setStyleSheet(
                f'QPushButton {{ background:{theme.PANEL}; color:{theme.TEXT}; '
                f'border:1px solid {theme.BORDER}; border-bottom:none; '
                f'border-top-left-radius:{radius}px; border-top-right-radius:{radius}px; font-weight:700; }} '
                f'QPushButton:hover {{ color:{theme.ACCENT}; border-color:{theme.ACCENT}; }}'
            )
    # [FN CLOSED] _style_map_tab_button

    # [FN CATEGORY] _position_map_dialog — the MAPPA dialog spans the full page: left/right edges
    # match this window's own, top sits just under the action toolbar (the Save row), bottom sits
    # just above the status bar (the UTF-8 row). Lives here (not in XrefMapDialog itself) because
    # it needs this window's own action_toolbar/statusBar — the same "coordination stays in
    # mainwindow.py" boundary _position_map_tab already follows for the dialog's drawer-handle
    # button. Called every time the dialog is (re)opened, not just once, so resizing or moving
    # this window between an MAPPA close and reopen doesn't leave the dialog at a stale rectangle.
    # [FN] _position_map_dialog — aligns the MAPPA dialog to the toolbar/status-bar band
    # [FN OPEN] _position_map_dialog
    def _position_map_dialog(self):
        if self.map_dialog is None or not self.map_dialog.isVisible():
            return
        top = self.action_toolbar.mapToGlobal(QPoint(0, self.action_toolbar.height())).y()
        bottom = self.statusBar().mapToGlobal(QPoint(0, 0)).y()
        left = self.mapToGlobal(QPoint(0, 0)).x()
        self.map_dialog.setGeometry(left, top, self.width(), bottom - top)
    # [FN CLOSED] _position_map_dialog

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    # [FN CATEGORY] dropEvent — dropping a folder opens it as the project; dropping a loose file
    # opens its containing folder as the project (the tree/context needs a project root) and then
    # opens that specific file
    # [FN] dropEvent — opens a folder or file dragged onto the window
    # [FN OPEN] dropEvent
    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if not path:
            return
        if os.path.isdir(path):
            self._open_project_folder(path)
        elif os.path.isfile(path):
            self._open_project_folder(os.path.dirname(path))
            self._open_file(path)
        event.acceptProposedAction()
    # [FN CLOSED] dropEvent

    def _toggle_theme(self):
        self.night_mode = not self.night_mode
        self.settings.setValue('nightMode', self.night_mode)
        set_theme(self.night_mode)
        self._apply_theme()

    # [FN CATEGORY] _tree_stylesheet — the ONE place the project tree's QSS is built, called from
    # both initial construction and _apply_theme's refresh pass. Those two used to duplicate this
    # string independently and had drifted apart (padding:6px 4px boxed-look vs. a later padding:
    # 14px 10px flat-look; a hardcoded #eef4ff selection color vs. a night-aware one) — real dead
    # space and a wrong-color selection highlight that only showed up after a theme toggle. One
    # shared builder means the two call sites can't diverge like that again.
    # [FN] _tree_stylesheet — boxed KANT tree QSS with a theme-aware selection color
    # [FN OPEN] _tree_stylesheet
    def _tree_stylesheet(self):
        padding = '2px' if self.view_mode == 'code' and self.compact_kant_view else '6px 4px'
        item_padding = '1px 2px' if self.view_mode == 'code' and self.compact_kant_view else '2px 0px'
        # selection reads as a left accent bar + a slightly raised surface, not a large yellow
        # fill — border-left is the one QSS-reliable way to fake a left bar on a tree row (no
        # separate "bar" sub-control exists), paired with a hairline right inset via padding so
        # the accent bar doesn't visually collide with the row's own left icon/text padding
        return (
            f'QTreeWidget {{ background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:{theme.RADIUS}px; padding:{padding}; }} '
            f'QTreeWidget::item {{ padding:{item_padding}; border-left:3px solid transparent; }} '
            f'QTreeWidget::item:hover {{ background:{theme.PANEL2}; }} '
            f'QTreeWidget::item:selected {{ background:{theme.PANEL2}; color:{theme.TEXT}; '
            f'border-left:3px solid {theme.ACCENT}; }}'
        )
    # [FN CLOSED] _tree_stylesheet

    def _apply_theme(self):
        self.setStyleSheet(theme.APP_STYLE)
        self.shell.setStyleSheet(f'#appShell {{ border:1px solid {theme.BORDER}; background:{theme.BG}; }}')
        self.title_bar.apply_style()
        if hasattr(self, 'welcome_title'):
            self.welcome_title.setStyleSheet(f'color:{theme.ACCENT}; letter-spacing:3px;')
            self.welcome_desc.setStyleSheet(f'color:{theme.DIM};')
            self.recent_title.setStyleSheet(f'color:{theme.DIM};')
            self._style_welcome_page()
        if hasattr(self, 'tree'):
            self.tree.setStyleSheet(self._tree_stylesheet())
            self._rebuild_tree()
        if hasattr(self, 'tabs'):
            # python_terminal used to keep its construction-time (day) colors forever: this only
            # ever restyled self.terminal, never its sibling in the same terminal_stack — invisible
            # until is_python_majority_project auto-switches to it (_open_project_folder), the one
            # case a real user actually sees the terminal dock in night mode with a still-light REPL
            terminal_style = (
                f'background:{theme.CODE_BG}; color:{theme.TEXT}; border-top:1px solid {theme.BORDER}; padding:12px;'
            )
            self.terminal.setStyleSheet(terminal_style)
            self.python_terminal.setStyleSheet(terminal_style)
            self.add_file_btn.setStyleSheet(self._add_row_button_style())
            self.add_grouping_btn.setStyleSheet(self._add_row_button_style())
            self.claude_pane.apply_style()
            self.conversation_sidebar.apply_style()
            self._style_io_tabs()
            self._style_view_mode_bar()
            self._style_action_toolbar()
            self._style_find_bar()
            self._style_status_bar()
            self._update_kant_map_label()
            self.tabs.tabBar().setStyleSheet(
                f'QTabBar::tab {{ background:{theme.PANEL}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
                f'padding:6px 8px; }} QTabBar::tab:selected {{ background:{theme.CODE_BG}; '
                f'border-bottom:2px solid {theme.ACCENT}; }}'
            )
            for key, btn in self.action_toolbar_buttons.items():
                btn.setIcon(draw_icon(key, btn.iconSize().width()))
            self._style_kant_quick_button()  # overrides the generic 'sparkle' icon set just above —
            # this one slot's icon/tooltip depends on view_mode, not just the theme
            for btn in self.terminal_sidebar_group.buttons():
                btn.setIcon(draw_icon(btn.property('kantIcon'), btn.iconSize().width()))
            map_open = self.map_dialog is not None and self.map_dialog.isVisible()
            self.map_tab_btn.setIcon(draw_icon('arrow-down' if map_open else 'arrow-up', 12))
            self.claude_tab_btn.setIcon(draw_icon(
                'arrow-left' if self.splitter.sizes()[1] == 0 else 'arrow-right', 12,
            ))
            for tab in self.open_tabs.values():
                tab.apply_style()
                self._render_view(tab, tab.filter_uid)
                # element pages already got their tab label re-styled by _update_element_tab_title
                # right below — a plain FileTab's own tab-strip label was missing that call, so its
                # text color (set explicitly in code, not inherited via QSS cascade) went stale on
                # a theme toggle instead of following BG/TEXT to the new theme
                self._update_tab_title(tab)
            for page in self._element_pages.values():
                self._update_element_tab_title(page)
            for btn in self.tabs.tabBar().findChildren(QToolButton):
                kind = btn.property('kantIcon')
                if kind:
                    btn.setIcon(draw_icon(kind, 14))
            if hasattr(self.active_page, '_element_key'):
                self._render_element_page(self.active_page)
            if self.active_tab is not None:
                self._update_io_tabs(self._active_filter_uid())
        self._refresh_recent_folders()
        self._ui_language.apply(self)

    # [FN CATEGORY] _build_action_toolbar — a persistent one-click icon row for the highest-
    # frequency actions (Save/Undo/Redo/Run/Find), in its own row below the title bar rather than
    # squeezed into it — the title bar's own essential window chrome (minimize/maximize/close) was
    # getting crowded out once these were added there. The File/Search/LSP menus stay too, for
    # discoverability and the lower-frequency actions; these are just a faster path to the same
    # window methods.
    # [FN] _build_action_toolbar — builds the Save/Undo/Redo/Run/Find icon row
    # [FN OPEN] _build_action_toolbar
    def _build_action_toolbar(self):
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(theme.SPACE_2, 0, 0, 0)
        layout.setSpacing(2)
        # leading separator groups this cluster apart from the menu bar it now sits beside —
        # spliced into TitleBar's own row (embed_toolbar), no longer a second stacked bar with
        # its own background/border
        lead_separator = QFrame()
        lead_separator.setFrameShape(QFrame.VLine)
        lead_separator.setFixedHeight(20)
        self._action_toolbar_lead_separator = lead_separator
        layout.addWidget(lead_separator)
        self.action_toolbar_buttons = {}
        for key, tooltip, callback in (
            ('save', 'Salva (Ctrl+S)', self._save_file),
            ('undo', 'Annulla file (Ctrl+Z)', self._undo_file),
            ('redo', 'Ripeti file (Ctrl+Y)', self._redo_file),
            ('find', 'Trova nel file (Ctrl+F)', self._show_find_bar),
        ):
            btn = QToolButton()
            btn.setIcon(draw_icon(key, 16))
            btn.setIconSize(QSize(16, 16))
            btn.setToolTip(tooltip)
            btn.setFixedSize(theme.ICON_BTN, theme.ICON_BTN)
            btn.clicked.connect(callback)
            layout.addWidget(btn)
            self.action_toolbar_buttons[key] = btn
        layout.addSpacing(theme.SPACE_2)
        # the AI-fill-blanks/deterministic-tag action lives next to file_path_label at the bottom
        # of the coding board now (see _build_io_tabs) — it acts on whatever's isolated there, so
        # it sits where that scope is actually shown, not up here disconnected from it
        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setFixedHeight(20)
        self._action_toolbar_separator = separator
        layout.addWidget(separator)
        layout.addSpacing(theme.SPACE_1)
        # Run/Debug at the trailing edge of this same cluster
        for key, tooltip, callback in (
            ('run', 'Esegui (Ctrl+R)', self._run_current_file),
            ('debug', 'Debug (F5)', self._debug_current_file),
        ):
            btn = QToolButton()
            btn.setIcon(draw_icon(key, 16))
            btn.setIconSize(QSize(16, 16))
            btn.setToolTip(tooltip)
            btn.setFixedSize(theme.ICON_BTN, theme.ICON_BTN)
            btn.clicked.connect(callback)
            layout.addWidget(btn)
            self.action_toolbar_buttons[key] = btn
            if key == 'run':
                # what Ctrl+R actually runs is always the whole file, so this names the KANT
                # parent element (module/class — never a leaf) that identifies whatever's
                # currently isolated in the coding board; blank whenever that's a leaf, since a
                # leaf has no run identity of its own
                self.run_target_label = QLabel('')
                layout.addWidget(self.run_target_label)
        self.action_toolbar = bar
        self._style_action_toolbar()
        self._style_kant_quick_button()
        return bar
    # [FN CLOSED] _build_action_toolbar

    # [FN CATEGORY] _kant_quick_action_has_structure — whether the file currently open in the
    # coding board has any real KANT node already, i.e. whether "fill blanks" has anything to
    # scope onto. Deliberately reads the active tab's own tree, not the left project tree's
    # unrelated KANT/File display-mode toggle — that toggle only changes how the SIDEBAR lists
    # files, and using it to decide this button's behavior meant the action could target the
    # whole file instead of whatever the coding board is actually isolating, whenever the sidebar
    # mode didn't happen to match the open file's real state.
    # [FN] _kant_quick_action_has_structure — does the open file already have KANT nodes
    # [FN OPEN] _kant_quick_action_has_structure
    def _kant_quick_action_has_structure(self):
        tab = self.active_tab
        return tab is not None and any(isinstance(n, Node) for n in tab.tree.body)
    # [FN CLOSED] _kant_quick_action_has_structure

    # [FN] _kant_quick_action — the sparkle-slot button's click handler: AI-fill-blanks (scoped to
    # whatever's isolated in the coding board) if the open file already has KANT structure, plain
    # deterministic tagging (no AI, always whole-file) if it doesn't yet
    # [FN OPEN] _kant_quick_action
    def _kant_quick_action(self):
        if self._kant_quick_action_has_structure():
            self._ai_fill_kant_blanks()
        else:
            self._deterministic_tag_current_file()
    # [FN CLOSED] _kant_quick_action

    # [FN CATEGORY] _style_kant_quick_button — the sparkle-slot button reads as a completely
    # different action depending on whether the open file already has KANT structure, not just a
    # differently-themed icon: real structure already there means there's something to isolate/
    # inspect, so "ask the AI to fill in the blanks it finds" makes sense; no structure yet is the
    # state an untagged file opens into (see _open_file), where the same slot instead offers the
    # plain deterministic pass with no AI involved at all. Mirrors _kant_quick_action's own check
    # exactly, so the icon never promises one action while the click runs the other.
    # [FN] _style_kant_quick_button — sets the sparkle-slot button's icon/tooltip for the open file
    # [FN OPEN] _style_kant_quick_button
    def _style_kant_quick_button(self):
        btn = self.action_toolbar_buttons.get('sparkle')
        if btn is None:
            return
        if self._kant_quick_action_has_structure():
            btn.setIcon(draw_icon('sparkle', 18))
            btn.setToolTip(self._tr(
                "Chiedi all'AI (agente/modello/effort attualmente selezionati nella plancia AI) di "
                'compilare i campi CATEGORY e descrizione vuoti della convenzione KANT in quello che '
                "e' attualmente visualizzato nella plancia di coding (l'intero file, o solo "
                "l'elemento isolato — anche una foglia). Tag, nesting, marker OPEN/CLOSED e #id "
                "restano quelli già calcolati deterministicamente dall'IDE — l'AI scrive solo il testo."
            ))
        else:
            btn.setIcon(draw_icon('nest', 18))
            btn.setToolTip(self._tr(
                'Genera la struttura KANT (tag, nesting, #id) in modo deterministico, senza AI, '
                'per il file aperto nella plancia di coding.'
            ))
    # [FN CLOSED] _style_kant_quick_button

    def _style_action_toolbar(self):
        # transparent: this bar is spliced into TitleBar's own row now (embed_toolbar), so it
        # inherits TitleBar's background/border instead of painting its own
        self.action_toolbar.setStyleSheet('background:transparent; border:none;')
        style = theme.icon_button_style()
        for btn in self.action_toolbar_buttons.values():
            btn.setStyleSheet(style)
        self._action_toolbar_separator.setStyleSheet(f'color:{theme.BORDER};')
        self._action_toolbar_lead_separator.setStyleSheet(f'color:{theme.BORDER};')
        self.run_target_label.setStyleSheet(f'color:{theme.DIM}; font-size:{theme.CODING_FONT_PT - 1}pt; padding-left:2px;')

    # [FN CATEGORY] _build_welcome_page — previously had a "+" new-project button floating in its
    # own top_row inside the SAME centered outer layout as the card: since outer.setAlignment
    # (Qt.AlignCenter) centers that whole two-item stack as a block, the button ended up stranded
    # wherever the block happened to land vertically, nowhere near the window's actual top-right
    # corner (a real layout bug, not just a taste issue). Fixed by dropping the floating button
    # entirely and folding "new project" into the card itself as a peer action next to "Apri
    # cartella…" — same dashed-outline "create new" language already used by the KANT panel's own
    # "+ Nuovo file"/"+ Nuovo gruppo" buttons, so both places that mean "create" now look alike.
    # A soft radial gradient behind the card (page itself, not the card) keeps a large window from
    # reading as a mostly-empty void the way a single small centered card on a flat background did.
    # [FN] _build_welcome_page — the pre-project screen: open/create a project, recent folders
    # [FN OPEN] _build_welcome_page
    def _build_welcome_page(self):
        page = QWidget()
        page.setObjectName('welcomePage')
        page.setAttribute(Qt.WA_StyledBackground, True)
        self.welcome_page = page
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)

        card = QWidget()
        card.setObjectName('welcomeCard')
        card.setFixedWidth(640)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(48, 44, 48, 40)
        layout.setSpacing(22)
        layout.setAlignment(Qt.AlignHCenter)

        badge = QLabel()
        badge.setPixmap(make_app_pixmap(96))
        badge.setFixedSize(96, 96)
        badge.setAlignment(Qt.AlignCenter)
        badge_row = QHBoxLayout()
        badge_row.setAlignment(Qt.AlignCenter)
        badge_row.addWidget(badge)
        layout.addLayout(badge_row)

        self.welcome_title = QLabel('KANT Editor')
        title = self.welcome_title
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont('Consolas', 26, QFont.DemiBold))
        title.setStyleSheet(f'color:{theme.TEXT}; letter-spacing:3px; border:none;')
        layout.addWidget(title)

        self.welcome_desc = QLabel(
            'Apri la cartella di un progetto per esplorarlo: i file vengono etichettati secondo i '
            'marcatori KANT ([TAG OPEN] Nome / [TAG CLOSED] Nome) e mostrati suddivisi in sezioni '
            'pieghevoli secondo la gerarchia MOD > CLS > FN, ecc.'
        )
        desc = self.welcome_desc
        desc.setAlignment(Qt.AlignCenter)
        desc.setWordWrap(True)
        desc.setStyleSheet(f'color:{theme.DIM}; border:none;')
        desc.setFont(QFont('Consolas', 12))
        layout.addWidget(desc)

        self.welcome_open_btn = QPushButton('Apri cartella…')
        self.welcome_open_btn.setFont(QFont('Consolas', 15, QFont.DemiBold))
        self.welcome_open_btn.setCursor(Qt.PointingHandCursor)
        self.welcome_open_btn.setToolTip('Scegli una cartella di progetto da aprire nell\'IDE')
        self.welcome_open_btn.clicked.connect(self._open_folder)

        # peer action, not a stray corner icon: same "create new" dashed-outline language as the
        # KANT panel's "+ Nuovo file"/"+ Nuovo gruppo" (_build_main_page's add_row_style)
        self.welcome_new_project_btn = QPushButton('＋  Nuovo progetto')
        self.welcome_new_project_btn.setFont(QFont('Consolas', 15, QFont.DemiBold))
        self.welcome_new_project_btn.setCursor(Qt.PointingHandCursor)
        self.welcome_new_project_btn.setToolTip('Crea un progetto nuovo da zero')
        self.welcome_new_project_btn.clicked.connect(self._prompt_new_project)

        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignCenter)
        btn_row.setSpacing(12)
        btn_row.addWidget(self.welcome_open_btn)
        btn_row.addWidget(self.welcome_new_project_btn)
        layout.addLayout(btn_row)

        self.recent_title = QLabel('CARTELLE RECENTI')
        recent_title = self.recent_title
        recent_title.setFont(QFont('Consolas', 9, QFont.DemiBold))
        recent_title.setStyleSheet(f'color:{theme.DIM}; letter-spacing:2px; border:none;')
        layout.addWidget(recent_title)

        self.recent_wrap = QWidget()
        self.recent_layout = QVBoxLayout(self.recent_wrap)
        self.recent_layout.setContentsMargins(0, 0, 0, 0)
        self.recent_layout.setSpacing(6)
        layout.addWidget(self.recent_wrap)

        self.welcome_card = card
        center_row = QHBoxLayout()
        center_row.addStretch(1)
        center_row.addWidget(card)
        center_row.addStretch(1)
        outer.addLayout(center_row)
        outer.addStretch(1)

        # day/night toggle, bottom-right — the title bar's own Aspetto -> Notte/Giorno menu is
        # hidden on this screen (_set_project_chrome_visible(False), project chrome only), so
        # before opening a project there was previously no way to switch themes at all. Floats
        # directly on the page (not part of `outer`'s own layout — a widget added there would
        # repeat the exact bug the old top-right "+" button had, see this function's history
        # above: it floated wherever the centered block landed, not the window's real corner),
        # repositioned on resize the same way map_tab_btn/claude_tab_btn already are (see
        # MainWindow.resizeEvent/_position_welcome_theme_btn).
        self.welcome_theme_btn = QPushButton(page)
        self.welcome_theme_btn.setFixedSize(40, 40)
        self.welcome_theme_btn.setCursor(Qt.PointingHandCursor)
        self.welcome_theme_btn.clicked.connect(self._toggle_theme)

        self.welcome_language_btn = QPushButton(page)
        self.welcome_language_btn.setFixedSize(126, 40)
        self.welcome_language_btn.setIcon(draw_icon('globe', 18, theme.ACCENT))
        self.welcome_language_btn.setIconSize(QSize(18, 18))
        self.welcome_language_btn.setCursor(Qt.PointingHandCursor)
        language_menu = QMenu(self.welcome_language_btn)
        self.welcome_language_actions = {
            'en': language_menu.addAction('English'),
            'it': language_menu.addAction('Italiano'),
        }
        for code, action in self.welcome_language_actions.items():
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, value=code: self._set_language(value))
        self.welcome_language_btn.setMenu(language_menu)

        self._apply_welcome_language()
        QTimer.singleShot(0, self._position_welcome_theme_btn)  # page has no real layout yet here
        self._refresh_recent_folders()
        return page
    # [FN CLOSED] _build_welcome_page

    # [FN] _position_welcome_theme_btn — keeps the welcome page's day/night toggle pinned to the
    # page's actual bottom-right corner, called from resizeEvent the same way map_tab_btn/
    # claude_tab_btn already are
    # [FN OPEN] _position_welcome_theme_btn
    def _position_welcome_theme_btn(self):
        if not hasattr(self, 'welcome_theme_btn'):
            return
        margin = 18
        self.welcome_language_btn.move(
            margin,
            self.welcome_page.height() - self.welcome_language_btn.height() - margin,
        )
        self.welcome_theme_btn.move(
            self.welcome_page.width() - self.welcome_theme_btn.width() - margin,
            self.welcome_page.height() - self.welcome_theme_btn.height() - margin,
        )
        self.welcome_language_btn.raise_()
        self.welcome_theme_btn.raise_()
    # [FN CLOSED] _position_welcome_theme_btn

    def _set_language(self, code):
        code = 'it' if str(code).lower().startswith('it') else 'en'
        self.ui_language = code
        self.settings.setValue('language', code)
        self._apply_welcome_language()
        self._ui_language.set_language(code)
        self._ui_language.apply(self)
        if hasattr(self, 'claude_pane'):
            self.claude_pane.refresh_focus_label()
        if self.map_dialog is not None:
            self.map_dialog._refresh()

    def _tr(self, text):
        return translate_text(text, self.ui_language)

    def _apply_welcome_language(self):
        italian = str(self.settings.value('language', 'en')).lower().startswith('it')
        code = 'it' if italian else 'en'
        self.welcome_language_btn.setText('Italiano' if italian else 'English')
        self.welcome_language_btn.setToolTip('Cambia lingua' if italian else 'Change language')
        for action_code, action in self.welcome_language_actions.items():
            action.setChecked(action_code == code)
        self.welcome_desc.setText(
            'Apri la cartella di un progetto per esplorarlo: i file vengono etichettati secondo i '
            'marcatori KANT ([TAG OPEN] Nome / [TAG CLOSED] Nome) e mostrati suddivisi in sezioni '
            'pieghevoli secondo la gerarchia MOD > CLS > FN, ecc.' if italian else
            'Open a project folder to explore it: files are labeled using KANT markers '
            '([TAG OPEN] Name / [TAG CLOSED] Name) and shown as collapsible sections following '
            'the MOD > CLS > FN hierarchy.'
        )
        self.welcome_open_btn.setText('Apri cartella…' if italian else 'Open folder…')
        self.welcome_open_btn.setToolTip(
            "Scegli una cartella di progetto da aprire nell'IDE" if italian else
            'Choose a project folder to open in the IDE'
        )
        self.welcome_new_project_btn.setText('＋  Nuovo progetto' if italian else '＋  New project')
        self.welcome_new_project_btn.setToolTip(
            'Crea un progetto nuovo da zero' if italian else 'Create a new project from scratch'
        )
        self.recent_title.setText('CARTELLE RECENTI' if italian else 'RECENT FOLDERS')
        self._style_welcome_page()

    # [FN CATEGORY] _welcome_page_stylesheet / _style_welcome_page — split out of
    # _build_welcome_page so _apply_theme can re-run the exact same styling after a day/night
    # toggle instead of leaving the page's gradient background, card, and action buttons stuck
    # with whichever theme was active when the page was first built (the pre-existing gap this
    # closes: welcome_title/welcome_desc/recent_title were already refreshed on toggle, the rest
    # of the page never was).
    # [FN] _welcome_page_stylesheet — the welcome page's radial-gradient background CSS
    # [FN OPEN] _welcome_page_stylesheet
    def _welcome_page_stylesheet(self):
        return (
            f'#welcomePage {{ background: qradialgradient(cx:0.5, cy:0.4, radius:0.9, fx:0.5, fy:0.4, '
            f'stop:0 {theme.PANEL}, stop:1 {theme.BG}); }}'
        )
    # [FN CLOSED] _welcome_page_stylesheet

    # [FN] _style_welcome_page — re-applies theme colors to the welcome page and its card/buttons
    # [FN OPEN] _style_welcome_page
    def _style_welcome_page(self):
        if not hasattr(self, 'welcome_page'):
            return
        self.welcome_page.setStyleSheet(self._welcome_page_stylesheet())
        self.welcome_card.setStyleSheet(
            f'#welcomeCard {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; border-radius:{theme.RADIUS}px; }}'
        )
        self.welcome_open_btn.setStyleSheet(
            f'QPushButton {{ background:{theme.ACCENT}; color:{theme.BG if theme.NIGHT else "#111827"}; border:none; '
            f'border-radius:{theme.RADIUS}px; padding:14px 28px; }} '
            f'QPushButton:hover {{ background:{theme.ACCENT}; }}'
        )
        self.welcome_new_project_btn.setStyleSheet(
            # theme.BORDER (a subtle divider color, not meant to stand alone) was nearly invisible
            # as a dashed outline in day mode; theme.DIM reads clearly in both themes
            f'QPushButton {{ background:{theme.CODE_BG}; color:{theme.ACCENT}; '
            f'border:2px dashed {theme.DIM}; border-radius:{theme.RADIUS}px; padding:12px 26px; }} '
            f'QPushButton:hover {{ border-color:{theme.ACCENT}; background:{theme.PANEL}; }}'
        )
        # icon shows the mode a click will SWITCH TO (a sun while it's night, a moon while it's
        # day) — same "label is the destination, not the current state" convention theme_menu_action
        # already uses for the Aspetto menu's own Notte/Giorno text
        self.welcome_theme_btn.setIcon(draw_icon('sun' if self.night_mode else 'moon', 18, theme.ACCENT))
        self.welcome_theme_btn.setIconSize(QSize(18, 18))
        italian = str(self.settings.value('language', 'en')).lower().startswith('it')
        self.welcome_theme_btn.setToolTip(
            ('Passa al tema chiaro' if self.night_mode else 'Passa al tema scuro') if italian else
            ('Switch to light theme' if self.night_mode else 'Switch to dark theme')
        )
        self.welcome_theme_btn.setStyleSheet(
            f'QPushButton {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; '
            f'border-radius:20px; }} '
            f'QPushButton:hover {{ border-color:{theme.ACCENT}; background:{theme.CODE_BG}; }}'
        )
        self.welcome_language_btn.setIcon(draw_icon('globe', 18, theme.ACCENT))
        self.welcome_language_btn.setStyleSheet(
            f'QPushButton {{ background:{theme.PANEL}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:20px; padding:0 10px; }} '
            f'QPushButton:hover {{ border-color:{theme.ACCENT}; background:{theme.CODE_BG}; }}'
        )
    # [FN CLOSED] _style_welcome_page

    # [FN] _add_row_button_style — shared "+ Nuovo ..." dashed-outline style for add_file_btn/
    # add_grouping_btn, callable both at construction and from _apply_theme so a day/night toggle
    # after a project is already open actually restyles these two buttons instead of leaving them
    # stuck with whichever theme was active when _build_main_page first ran
    def _add_row_button_style(self):
        return (
            f'QPushButton {{ background:transparent; color:{theme.DIM}; border:none; '
            f'border-radius:{theme.RADIUS}px; padding:{theme.SPACE_1}px {theme.SPACE_2}px; font-weight:600; }} '
            f'QPushButton:hover {{ color:{theme.ACCENT}; background:{theme.PANEL2}; }}'
        )

    def _build_main_page(self):
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.tree = ProjectTree()
        self.tree.setHeaderHidden(True)
        self.tree.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        self.tree.setMinimumWidth(160)
        self.tree.setIndentation(14)
        self.tree.setUniformRowHeights(False)  # rows can grow taller once labels wrap
        self.tree.setStyleSheet(self._tree_stylesheet())
        self.tree.itemClicked.connect(self._on_tree_item_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        self._update_tree_drop_handler()
        self.setAcceptDrops(True)

        tree_panel = QWidget()
        tree_panel_layout = QVBoxLayout(tree_panel)
        tree_panel_layout.setContentsMargins(6, 6, 6, 6)
        tree_panel_layout.setSpacing(6)
        tree_panel_layout.addWidget(self._build_view_mode_bar())
        tree_panel_layout.addWidget(self.tree, 1)

        self.add_file_btn = QPushButton('+  Nuovo file')
        self.add_file_btn.setCursor(Qt.PointingHandCursor)
        self.add_file_btn.setToolTip('Crea un nuovo file nella cartella del progetto')
        self.add_file_btn.setStyleSheet(self._add_row_button_style())
        self.add_file_btn.clicked.connect(self._prompt_add_file)

        # a grouping bundles elements from anywhere in the project (any tag, any file, any parent)
        # under one name, independent of the source tree's own MOD/CLS/FN nesting — see
        # kant/groupings.py for the persistence format and _prompt_add_grouping for the picker
        self.add_grouping_btn = QPushButton('+  Nuovo gruppo')
        self.add_grouping_btn.setCursor(Qt.PointingHandCursor)
        self.add_grouping_btn.setToolTip('Raggruppa elementi da file diversi sotto un nome comune')
        self.add_grouping_btn.setStyleSheet(self._add_row_button_style())
        self.add_grouping_btn.clicked.connect(self._prompt_add_grouping)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        add_row.addWidget(self.add_file_btn)
        add_row.addWidget(self.add_grouping_btn)
        tree_panel_layout.addLayout(add_row)
        self.tree_panel = tree_panel

        # MAPPA opens from a small tab stuck to the bottom-center edge of the window (like a
        # drawer handle) rather than a button buried in the tree panel; clicking it again while
        # the map is open closes it back down. Parented directly to the shell (not the stack) so
        # it stays put and clickable regardless of which page/tab is showing underneath.
        self.map_tab_btn = QPushButton(' MAPPA', self.shell)
        self.map_tab_btn.setProperty('kantIcon', 'arrow-up')
        self.map_tab_btn.setIcon(draw_icon('arrow-up', 12))
        self.map_tab_btn.setIconSize(QSize(12, 12))
        self.map_tab_btn.setFixedSize(96, 22)
        self.map_tab_btn.setCursor(Qt.PointingHandCursor)
        self.map_tab_btn.setToolTip('Apri/chiudi la mappa grafica delle dipendenze (MAPPA) del progetto')
        self.map_tab_btn.clicked.connect(self._toggle_xref_window)
        self.map_tab_btn.hide()  # only relevant once a project is open

        # same tab-on-the-edge pattern as MAPPA's, but on the right edge for the AI terminal pane:
        # one button whose arrow flips between flattening the pane to zero width and restoring it.
        # Starts pointing right (arrow-right) to match the pane's default-expanded state.
        self._claude_pane_width = None
        self.claude_tab_btn = QPushButton('', self.shell)
        self.claude_tab_btn.setProperty('kantIcon', 'arrow-right')
        self.claude_tab_btn.setIcon(draw_icon('arrow-right', 12))
        self.claude_tab_btn.setIconSize(QSize(12, 12))
        self.claude_tab_btn.setFixedSize(16, 60)
        self.claude_tab_btn.setCursor(Qt.PointingHandCursor)
        self.claude_tab_btn.setToolTip('Comprimi/espandi il terminale AI')
        self.claude_tab_btn.clicked.connect(self._toggle_claude_pane)
        self.claude_tab_btn.hide()  # only relevant once a project is open

        # each open file is a tab (FileTab) with its own scroll area/view/dirty state; switching
        # tabs is just switching the QTabWidget's current index, nothing to rebuild
        self.tabs = QTabWidget()
        self.tabs.setTabBar(_KantTabBar())
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_active_tab_changed)
        self.tabs.tabBarClicked.connect(lambda index: self._set_ai_context_page(self.tabs.widget(index)))

        self.tree.itemDoubleClicked.connect(self._on_tree_item_double_clicked)
        # Each element view is another page in the same tab bar, backed by the file's one FileTab.
        self._element_pages = {}
        # File and element previews share one visible slot. These two references identify its
        # current type; when a child owns the slot its FileTab parent remains hidden as its model.
        self._preview_page = None
        self._preview_file_tab = None

        view_panel = QWidget()
        view_panel_layout = QVBoxLayout(view_panel)
        view_panel_layout.setContentsMargins(0, 0, 0, 0)
        view_panel_layout.setSpacing(0)
        view_panel_layout.addWidget(self._build_find_bar())
        view_panel_layout.addWidget(self._build_vim_command_bar())
        view_panel_layout.addWidget(self.tabs, 1)
        self.io_tabs = self._build_io_tabs()
        view_panel_layout.addWidget(self.io_tabs)
        self.view_panel = view_panel

        self.claude_pane = ClaudePane(os.getcwd())
        self.claude_pane.before_run = self._prepare_ai_snapshot
        self.claude_pane.context_hint = self._build_ai_context_hint
        self.claude_pane.focus_hint = self._build_ai_focus_summary
        self.claude_pane.refresh_focus_label()
        self.claude_pane.finished.connect(self._finish_ai_review)
        self.claude_pane.conversationChanged.connect(self._save_active_ai_conversation)
        self.claude_pane.pureAiToggled.connect(self._set_pure_ai_mode)
        self.conversation_sidebar = ConversationSidebar(self.shell)
        self.conversation_sidebar.conversationSelected.connect(self._activate_ai_conversation)
        self.conversation_sidebar.groupRequested.connect(self._group_ai_conversation)
        self.conversation_sidebar.newRequested.connect(self._new_ai_conversation)
        self.conversation_sidebar.hide()

        self.terminal_dock = self._build_terminal_dock()
        self._style_io_tabs()
        self._update_io_tabs(None)
        self.workspace_splitter = QSplitter(Qt.Horizontal)
        self.workspace_splitter.addWidget(tree_panel)
        self.workspace_splitter.addWidget(view_panel)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        saved_workspace_sizes = self.settings.value('workspaceSplitterSizes')
        if saved_workspace_sizes and len(saved_workspace_sizes) == 2:
            self.workspace_splitter.setSizes([int(x) for x in saved_workspace_sizes])
        else:
            self.workspace_splitter.setSizes([theme.TREE_MIN_WIDTH, 900])
        self.workspace_splitter.splitterMoved.connect(
            lambda *_: self._debounce_splitter_save('workspaceSplitterSizes', self.workspace_splitter)
        )

        self.main_splitter = QSplitter(Qt.Vertical)
        self.main_splitter.addWidget(self.workspace_splitter)
        self.main_splitter.addWidget(self.terminal_dock)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        saved_main_sizes = self.settings.value('mainVerticalSplitterSizes')
        # every session starts with the terminal dock genuinely visible — a saved size from a
        # previous session where it had been dragged down near/to 0 used to persist that collapsed
        # state on every subsequent launch, silently hiding the terminal until the user remembered
        # to drag it back open by hand. A minimum here doesn't discard the rest of the saved layout
        # (the workspace/terminal split ratio still restores), it just floors the terminal's share.
        MIN_TERMINAL_HEIGHT = 200
        if saved_main_sizes and len(saved_main_sizes) == 2:
            sizes = [int(x) for x in saved_main_sizes]
            if sizes[1] < MIN_TERMINAL_HEIGHT:
                sizes[0] -= (MIN_TERMINAL_HEIGHT - sizes[1])
                sizes[1] = MIN_TERMINAL_HEIGHT
            self.main_splitter.setSizes(sizes)
        else:
            self.main_splitter.setSizes([630, 200])
        self.main_splitter.splitterMoved.connect(
            lambda *_: self._debounce_splitter_save('mainVerticalSplitterSizes', self.main_splitter)
        )

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.main_splitter)
        self.splitter.addWidget(self.claude_pane)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        saved_sizes = self.settings.value('splitterSizes')
        if saved_sizes and len(saved_sizes) == 2:
            self.splitter.setSizes([int(x) for x in saved_sizes])
        else:
            self.splitter.setSizes([1320, 460])
        self.splitter.splitterMoved.connect(
            lambda *_: self._debounce_splitter_save('splitterSizes', self.splitter)
        )
        self.splitter.splitterMoved.connect(lambda *_: self._position_claude_tab())
        root_layout.addWidget(self.splitter, 1)
        self._build_status_bar()
        return central

    # [FN CATEGORY] _build_status_bar — cursor line/column, encoding and line-ending indicator for
    # the currently focused code block; QMainWindow's own status bar area, below the central widget
    # [FN] _build_status_bar — builds the bottom status bar
    # [FN OPEN] _build_status_bar
    def _build_status_bar(self):
        bar = self.statusBar()
        self.kant_map_label = QLabel('')
        self.kant_map_label.setFont(QFont('Consolas', theme.TREE_FONT_PT - 2))
        bar.addWidget(self.kant_map_label)  # addWidget (not addPermanentWidget): left-aligned
        # Git branch/status — collected here (left side, alongside file/KANT context) since it was
        # previously only shown inline per-row in the tree and via the Git menu, with no persistent
        # summary anywhere; the per-row tree markers stay too (position vs. summary, not a duplicate)
        self.git_status_label = QLabel('')
        self.git_status_label.setFont(QFont('Consolas', theme.TREE_FONT_PT - 2))
        bar.addWidget(self.git_status_label)
        self.cursor_pos_label = QLabel('')
        self.encoding_label = QLabel('')
        # shows the focused CodeEdit's vim mode (NORMAL/INSERT/VISUAL); empty whenever vim mode is
        # off or no code block has focus — see _on_focus_changed and _update_vim_mode_label
        self.vim_mode_label = QLabel('')
        self.vim_mode_label.setFont(QFont('Consolas', theme.TREE_FONT_PT, QFont.DemiBold))
        bar.addPermanentWidget(self.vim_mode_label)
        # LSP connection state for whichever server is (or was) active — no signal exists upstream
        # for the ready/not-ready transition, so lsp_status_timer polls the client's own existing
        # process/ready attributes (kant/lsp.py, untouched) rather than adding new LSP plumbing
        self.lsp_status_label = QLabel('')
        self.lsp_status_label.setFont(QFont('Consolas', theme.TREE_FONT_PT - 2))
        bar.addPermanentWidget(self.lsp_status_label)
        # a flat QPushButton, not a QLabel: it needs to be click-to-reopen-the-picker, and a
        # QPushButton gets that natively instead of needing the tree row labels' click-forwarding
        self.python_env_label = QPushButton('')
        self.python_env_label.setCursor(Qt.PointingHandCursor)
        self.python_env_label.setToolTip('Cambia interprete Python per questo progetto')
        self.python_env_label.clicked.connect(self._select_python_interpreter)
        self.python_env_label.setVisible(False)
        bar.addPermanentWidget(self.python_env_label)
        bar.addPermanentWidget(self.cursor_pos_label)
        bar.addPermanentWidget(self.encoding_label)
        self._style_status_bar()
        self.lsp_status_timer = QTimer(self)
        self.lsp_status_timer.timeout.connect(self._update_lsp_status_label)
        self.lsp_status_timer.start(2000)
        self._update_lsp_status_label()
    # [FN CLOSED] _build_status_bar

    def _style_status_bar(self):
        self.statusBar().setStyleSheet(
            f'QStatusBar {{ background:{theme.PANEL}; color:{theme.DIM}; border-top:1px solid {theme.BORDER}; }}'
        )
        self.vim_mode_label.setStyleSheet(f'color:{theme.ACCENT}; padding:0 8px;')
        self.git_status_label.setStyleSheet(f'color:{theme.DIM}; padding:0 8px;')
        self.lsp_status_label.setStyleSheet(f'color:{theme.DIM}; padding:0 8px;')
        self.python_env_label.setStyleSheet(
            f'QPushButton {{ border:none; background:transparent; color:{theme.DIM}; padding:0 8px; }} '
            f'QPushButton:hover {{ color:{theme.ACCENT}; }}'
        )
        self._update_git_status_label()
        self._update_lsp_status_label()

    # [FN CATEGORY] _update_git_status_label — reads self.git_root/self.git_status (set by
    # GitOpsMixin's refresh) plus one `git rev-parse --abbrev-ref HEAD` call for the branch name;
    # blank when the project isn't a git repo. The tree's own per-row markers stay untouched —
    # this is the one persistent summary, not a replacement for them.
    # [FN] _update_git_status_label — status-bar branch + dirty-file count
    # [FN OPEN] _update_git_status_label
    def _update_git_status_label(self):
        if not self.git_root:
            self.git_status_label.setText('')
            return
        result = self._run_git(['rev-parse', '--abbrev-ref', 'HEAD'])
        branch = result.stdout.strip() if result and result.returncode == 0 else '?'
        dirty = len(self.git_status or {})
        suffix = f' ({dirty})' if dirty else ''
        self.git_status_label.setText(f'  ⎇ {branch}{suffix}  ')
        self.git_status_label.setStyleSheet(f'color:{theme.WARN if dirty else theme.DIM}; padding:0 8px;')
    # [FN CLOSED] _update_git_status_label

    # [FN CATEGORY] _update_lsp_status_label — reads self.lsp_client.process/.ready directly
    # (kant/lsp.py, untouched); polled by lsp_status_timer since no ready-state signal exists
    # upstream to react to instead
    # [FN] _update_lsp_status_label — status-bar LSP connection state
    # [FN OPEN] _update_lsp_status_label
    def _update_lsp_status_label(self):
        client = getattr(self, 'lsp_client', None)
        if client is None or client.process is None:
            self.lsp_status_label.setText('  LSP: —  ')
            self.lsp_status_label.setStyleSheet(f'color:{theme.DIM}; padding:0 8px;')
            return
        ready = client.ready
        self.lsp_status_label.setText(f"  LSP: {client.server_name}{'' if ready else '…'}  ")
        self.lsp_status_label.setStyleSheet(f'color:{theme.OK if ready else theme.WARN}; padding:0 8px;')
    # [FN CLOSED] _update_lsp_status_label

    def _on_focus_changed(self, _old, new):
        if isinstance(new, CodeEdit):
            self._update_cursor_position_label(new)
            self._update_vim_mode_label(new)
            page = new
            while page is not None and not isinstance(page, FileTab) and not hasattr(page, '_element_key'):
                page = page.parentWidget()
            node = getattr(new, 'kant_node', None)
            if page is not None and node is not None:
                page._ai_focus_uid = node.uid
            self._set_ai_context_page(page)
            if not hasattr(new, '_status_bar_wired'):
                new._status_bar_wired = True
                new.cursorPositionChanged.connect(lambda e=new: self._update_cursor_position_label(e))
                new.vim_mode_changed.connect(lambda _mode, e=new: self._update_vim_mode_label(e))
        else:
            self.vim_mode_label.setText('')

    # [FN] _update_vim_mode_label — status-bar indicator for the focused CodeEdit's vim mode; blank
    # whenever vim mode is globally off, matching how the rest of the toggle stays invisible-by-off
    # [FN OPEN] _update_vim_mode_label
    def _update_vim_mode_label(self, edit):
        if not vim_mode_enabled():
            self.vim_mode_label.setText('')
            return
        labels = {
            'normal': '-- NORMAL --', 'insert': '-- INSERT --',
            'visual': '-- VISUAL --', 'visual_line': '-- VISUAL LINE --',
        }
        self.vim_mode_label.setText(labels.get(edit.vim_state, ''))
    # [FN CLOSED] _update_vim_mode_label

    def _update_cursor_position_label(self, edit):
        cursor = edit.textCursor()
        self.cursor_pos_label.setText(
            f'  Riga {edit.absolute_line_number(cursor.blockNumber())}, Col {cursor.columnNumber() + 1}  '
        )

    # [FN CATEGORY] _build_view_mode_bar — three mutually-exclusive source/grouping modes plus one
    # independent compact-render toggle for KANT; the compact renderer keeps the same tree/items.
    # [FN] _build_view_mode_bar — builds the KANT/File/Groups row and compact toggle
    # [FN OPEN] _build_view_mode_bar
    def _build_view_mode_bar(self):
        bar = QWidget()
        self.view_mode_bar = bar
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self.code_view_btn = QPushButton('KANT')
        self.code_view_btn.setCheckable(True)
        self.code_view_btn.setChecked(True)
        self.code_view_btn.setToolTip('Mostra la struttura concettuale del progetto come elementi KANT')
        self.code_view_btn.clicked.connect(lambda: self._set_view_mode('code'))

        self.file_view_btn = QPushButton('File')
        self.file_view_btn.setCheckable(True)
        self.file_view_btn.setToolTip('Mostra cartelle e file del progetto senza la struttura concettuale KANT')
        self.file_view_btn.clicked.connect(lambda: self._set_view_mode('file'))

        self.groups_view_btn = QPushButton('Gruppi')
        self.groups_view_btn.setCheckable(True)
        self.groups_view_btn.setToolTip(
            'Mostra i raggruppamenti del progetto (collezioni di elementi da file diversi) '
            'invece dell\'albero dei file'
        )
        self.groups_view_btn.clicked.connect(lambda: self._set_view_mode('groups'))

        self.view_mode_group = QButtonGroup(self)
        self.view_mode_group.setExclusive(True)
        self.view_mode_group.addButton(self.code_view_btn)
        self.view_mode_group.addButton(self.file_view_btn)
        self.view_mode_group.addButton(self.groups_view_btn)

        layout.addWidget(self.code_view_btn)
        layout.addWidget(self.file_view_btn)
        layout.addSpacing(14)
        layout.addWidget(self.groups_view_btn)
        layout.addStretch(1)
        self.compact_kant_btn = QToolButton()
        self.compact_kant_btn.setProperty('kantIcon', 'grid')
        self.compact_kant_btn.setIcon(draw_icon('grid', 16))
        self.compact_kant_btn.setIconSize(QSize(16, 16))
        self.compact_kant_btn.setFixedSize(32, 30)
        self.compact_kant_btn.setCheckable(True)
        self.compact_kant_btn.setChecked(self.compact_kant_view)
        self.compact_kant_btn.setCursor(Qt.PointingHandCursor)
        self.compact_kant_btn.toggled.connect(self._set_compact_kant_view)
        layout.addWidget(self.compact_kant_btn)
        self._style_view_mode_bar()
        self._update_action_buttons()
        return bar
    # [FN CLOSED] _build_view_mode_bar

    def _style_view_mode_bar(self):
        self.view_mode_bar.setStyleSheet(theme.panel_header_style())
        # active mode reads as a gold underline, not a filled accent surface — the left panel's
        # own version of the same tab_style used for the file-tab bar, so "selected" means the
        # same thing everywhere instead of switching to a full accent fill here specifically
        tab_style = theme.tab_style()
        for btn in (self.code_view_btn, self.file_view_btn, self.groups_view_btn):
            btn.setStyleSheet(tab_style)
        self.compact_kant_btn.setStyleSheet(theme.icon_button_style(selected=self.compact_kant_btn.isChecked()))
        self.compact_kant_btn.setEnabled(self.view_mode == 'code')
        self.compact_kant_btn.setToolTip(
            'Torna alla vista KANT a blocchi' if self.compact_kant_btn.isChecked()
            else 'Mostra la struttura KANT come albero compatto con menu espandibili'
        )
        # icon_button_style's "selected" look is a PANEL2 fill + accent border, not a full accent
        # fill, so the icon itself just switches to accent color when checked rather than needing
        # a light-on-dark-fill color
        self.compact_kant_btn.setIcon(
            draw_icon('grid', 16, theme.ACCENT if self.compact_kant_btn.isChecked() else None)
        )

    # [FN CATEGORY] _set_compact_kant_view — switches only the KANT tree renderer, preserving the
    # same navigation items and expansion behavior.
    # [FN] _set_compact_kant_view — toggles compact KANT rows and rebuilds the tree
    # [FN OPEN] _set_compact_kant_view
    def _set_compact_kant_view(self, compact):
        self.compact_kant_view = compact
        self._style_view_mode_bar()
        if self.view_mode == 'code':
            self.tree.setStyleSheet(self._tree_stylesheet())
            self._rebuild_tree()
    # [FN CLOSED] _set_compact_kant_view

    # [FN CATEGORY] _build_ai_context_hint — a hidden (never shown in the chat bubble) instruction
    # scoping the AI's changes to whatever the coding panel is currently showing: the isolated
    # element if one is filtered in, otherwise the whole open file. Delivered through the same
    # --append-system-prompt channel ClaudePane already uses for the KANT comment standard, so it
    # reaches the model without polluting the visible conversation. GLOBAL suppresses it entirely —
    # nothing extra is added, the prompt goes exactly as typed, project-wide.
    # [FN] _build_ai_context_hint — ClaudePane.context_hint callback
    # [FN OPEN] _build_ai_context_hint
    def _ai_context_target(self):
        page = getattr(self, '_ai_context_page', None) or self.active_page
        file_tab = getattr(page, '_file_tab', None)
        tab = file_tab or (page if isinstance(page, FileTab) else self.active_tab)
        if tab is None:
            return None, None
        default_uid = page._element_key[1] if file_tab is not None else tab.filter_uid
        return tab, getattr(page, '_ai_focus_uid', None) or self._visible_ai_context_uid(page) or default_uid

    def _visible_ai_context_uid(self, page):
        if not hasattr(page, 'findChildren') or not shiboken6.isValid(page):
            return None
        # a stray reference to an already-closed tab (_ai_context_page/active_page) reaching here
        # crashes on the FIRST Qt call it makes, not necessarily on `page` itself — e.g. its
        # scroll_area child can be gone even when `page` still passes isValid(); reproduced via a
        # real crash log (RuntimeError: ... QScrollArea already deleted) from a background git
        # status refresh landing after the tab it was still pointing at had closed
        if isinstance(page, FileTab):
            if not shiboken6.isValid(page.scroll_area):
                return None
            viewport = page.scroll_area.viewport()
        else:
            viewport = page.viewport()
        best_area, best_uid = -1, None
        for edit in page.findChildren(CodeEdit):
            node = getattr(edit, 'kant_node', None)
            if node is None:
                continue
            pos = edit.mapTo(viewport, QPoint(0, 0))
            width = max(0, min(pos.x() + edit.width(), viewport.width()) - max(pos.x(), 0))
            height = max(0, min(pos.y() + edit.height(), viewport.height()) - max(pos.y(), 0))
            area = width * height
            # >= (not >): findChildren() visits an outer element (e.g. a class) before the nested
            # ones inside it, so on an exact-area tie this keeps the LAST (most nested/specific)
            # widget — deterministic, and the deepest element is the more meaningful focus target
            # anyway. The old tiebreak on node.uid compared random hex strings, i.e. a coin flip.
            if area >= best_area:
                best_area, best_uid = area, node.uid
        return best_uid

    def _set_ai_context_page(self, page):
        if page is None:
            return
        self._ai_context_page = page
        if hasattr(self, 'claude_pane'):
            self.claude_pane.refresh_focus_label()

    def _build_ai_context_hint(self):
        tab, uid = self._ai_context_target()
        root = getattr(self, 'project_root_path', None)
        italian = str(self.settings.value('language', 'en')).lower().startswith('it')
        if getattr(self, 'pure_ai_mode', False):
            root_label = (root or '.').replace(os.sep, '/')
            return (
                f'Root: {root_label}. Hai accesso in lettura a tutto il progetto in questa cartella; '
                'la plancia KANT a destra è solo una vista e non definisce il focus della conversazione.'
                if italian else
                f'Root: {root_label}. You have read access to the whole project in this directory; '
                'the KANT board on the right is only a view and does not scope this conversation.'
            )
        target = None
        if tab is not None:
            node = self._find_node_by_uid(tab.tree, uid) if uid else None
            path = os.path.relpath(tab.path, root).replace(os.sep, '/') if root else tab.path
            symbol = node.name if node is not None else None
            if node is not None and node.tag in {'VAR', 'CST'}:
                names = re.findall(
                    r'(?m)^\s*(?:const\s+|let\s+|var\s+)?([A-Za-z_]\w*)\s*(?::[^=\n]+)?\s*=(?!=)',
                    serialize_kant(Node(tag='ROOT', name='', open_raw=None, body=[node])),
                )
                if names:
                    # ponytail: twelve names keep the hint bounded; the remainder count preserves scope.
                    symbol = ','.join(names[:12]) + (f',+{len(names) - 12}' if len(names) > 12 else '')
            target = f'{path}::{symbol}' if symbol else path
        if self.claude_pane.global_mode_btn.isChecked():
            root_label = (root or '.').replace(os.sep, '/')
            view = f' | {"Vista" if italian else "View"}: {target}' if target else ''
            return (
                f'Root: {root_label}{view}. Hai accesso in lettura a tutto il progetto in questa '
                f'cartella: esplora e leggi i file che ti servono direttamente dal filesystem invece '
                f'di chiedere all\'utente di incollarli. Usa tutta la root; la vista è solo il focus.' if italian else
                f'Root: {root_label}{view}. You have read access to the whole project in this '
                f'directory: explore and read whatever files you need directly from the filesystem '
                f'instead of asking the user to paste them. Use the whole root; the view is just the focus.'
            )
        if target is None:
            return None
        # imperative and explicit on purpose: a softer phrasing ("inspect it", "don't ask for
        # attachments") still leaves room for the model to default to asking the user to paste code
        # for a vague, non-editing question — stating outright that it already has read access and
        # naming the exact action to take (read this file yourself) closes that gap.
        return (
            f'Contesto implicito: {target}. Hai accesso in lettura al progetto in questa cartella — '
            f'se il messaggio non nomina esplicitamente un file diverso, leggi tu stesso {target} dal '
            f'filesystem per rispondere. Non chiedere all\'utente di incollare il codice.' if italian else
            f'Implicit context: {target}. You have read access to the project in this directory — '
            f'unless the message explicitly names a different file, read {target} yourself from the '
            f'filesystem to answer. Do not ask the user to paste the code.'
        )
    # [FN CLOSED] _build_ai_context_hint

    # [FN CATEGORY] _build_ai_focus_summary — the short, user-visible counterpart to
    # _build_ai_context_hint: same three cases (GLOBAL / isolated element / whole file), but a
    # one-line label for ClaudePane.focus_label instead of a full hidden instruction — mirrors that
    # function's logic on purpose so the two can never silently disagree about what's in scope.
    # [FN] _build_ai_focus_summary — ClaudePane.focus_hint callback
    # [FN OPEN] _build_ai_focus_summary
    def _build_ai_focus_summary(self):
        if self.claude_pane.global_mode_btn.isChecked():
            return 'intero progetto' if self.ui_language == 'it' else 'whole project'
        tab, uid = self._ai_context_target()
        if tab is None:
            return None
        rel_path = os.path.relpath(tab.path, self.project_root_path) if self.project_root_path else tab.path
        if uid:
            node = self._find_node_by_uid(tab.tree, uid)
            if node is not None:
                label = node.desc or node.name
                return f'[{node.tag}] "{label}" in {rel_path}'
        return rel_path
    # [FN CLOSED] _build_ai_focus_summary

    def _update_action_buttons(self):
        if not hasattr(self, 'title_bar') or not hasattr(self, 'tabs'):
            return
        has_tab = self.active_tab is not None
        has_git_file = bool(has_tab and self.git_root)
        self.title_bar.save_menu_action.setEnabled(has_tab)
        self.title_bar.undo_menu_action.setEnabled(bool(has_tab and self.active_tab.undo_stack))
        self.title_bar.redo_menu_action.setEnabled(bool(has_tab and self.active_tab.redo_stack))
        self.title_bar.validate_kant_menu_action.setEnabled(bool(self.project_root_path))
        self.title_bar.tag_current_file_menu_action.setEnabled(has_tab)
        self.title_bar.comment_full_file_menu_action.setEnabled(has_tab)
        self.title_bar.comment_project_menu_action.setEnabled(bool(self.project_root_path))
        self.title_bar.remove_kant_comments_menu_action.setEnabled(bool(self.project_root_path))
        self.title_bar.wipe_retag_menu_action.setEnabled(bool(self.project_root_path))
        self.title_bar.run_menu_action.setEnabled(has_tab)
        self.title_bar.find_menu_action.setEnabled(has_tab)
        self.action_toolbar_buttons['save'].setEnabled(has_tab)
        self.action_toolbar_buttons['undo'].setEnabled(bool(has_tab and self.active_tab.undo_stack))
        self.action_toolbar_buttons['redo'].setEnabled(bool(has_tab and self.active_tab.redo_stack))
        self.action_toolbar_buttons['find'].setEnabled(has_tab)
        self.action_toolbar_buttons['run'].setEnabled(has_tab)
        self.action_toolbar_buttons['debug'].setEnabled(has_tab)
        self.action_toolbar_buttons['sparkle'].setEnabled(has_tab)
        self._style_kant_quick_button()
        self.run_target_label.setText(self._run_target_text())
        self.title_bar.project_search_menu_action.setEnabled(bool(self.project_root_path))
        self.title_bar.project_replace_menu_action.setEnabled(bool(self.project_root_path))
        self.title_bar.git_refresh_menu_action.setEnabled(bool(self.git_root))
        self.title_bar.git_diff_menu_action.setEnabled(has_git_file)
        self.title_bar.git_stage_menu_action.setEnabled(has_git_file)
        self.title_bar.git_unstage_menu_action.setEnabled(has_git_file)
        self.title_bar.git_commit_menu_action.setEnabled(bool(self.git_root))
        self.title_bar.git_branch_menu_action.setEnabled(bool(self.git_root))
        self.title_bar.run_tests_menu_action.setEnabled(bool(self.project_root_path))
        for action in (
            self.title_bar.lsp_hover_menu_action,
            self.title_bar.lsp_definition_menu_action,
            self.title_bar.lsp_references_menu_action,
            self.title_bar.lsp_rename_menu_action,
            self.title_bar.lsp_format_menu_action,
            self.title_bar.lsp_format_external_menu_action,
        ):
            action.setEnabled(has_tab)
        if hasattr(self, 'claude_pane'):
            self.claude_pane.refresh_focus_label()

    # [FN] _build_element_page — builds an element view for the main coding tab bar
    def _build_element_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setAlignment(Qt.AlignTop)
        layout.setContentsMargins(4, 0, 4, 4)
        layout.setSpacing(4)
        scroll.setWidget(container)
        return scroll, layout

    # [FN CATEGORY] _build_find_bar — Ctrl+F search across every CodeEdit currently in the view (the
    # KANT view splits a file into many small editors, so search has to hop between them, not just
    # search one QTextDocument); hidden until invoked, closes on Escape
    # [FN] _build_find_bar — builds the hidden find bar shown above the code view
    # [FN OPEN] _build_find_bar
    def _build_find_bar(self):
        self._find_widget_index = 0
        bar = QWidget()
        self.find_bar = bar
        bar.setVisible(False)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)

        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText('Cerca nel file aperto…')
        self.find_input.returnPressed.connect(self._find_next)
        self.find_input.setFont(QFont('Consolas', theme.CODING_FONT_PT))
        layout.addWidget(self.find_input, 1)

        prev_btn = QPushButton('')
        prev_btn.setProperty('kantIcon', 'arrow-up')
        prev_btn.setIcon(draw_icon('arrow-up', 14))
        prev_btn.setIconSize(QSize(14, 14))
        prev_btn.setFixedWidth(32)
        prev_btn.setToolTip('Occorrenza precedente')
        prev_btn.clicked.connect(self._find_prev)
        layout.addWidget(prev_btn)

        next_btn = QPushButton('')
        next_btn.setProperty('kantIcon', 'arrow-down')
        next_btn.setIcon(draw_icon('arrow-down', 14))
        next_btn.setIconSize(QSize(14, 14))
        next_btn.setFixedWidth(32)
        next_btn.setToolTip('Occorrenza successiva (Invio)')
        next_btn.clicked.connect(self._find_next)
        layout.addWidget(next_btn)

        self.find_status = QLabel('')
        self.find_status.setStyleSheet(f'color:{theme.DIM};')
        layout.addWidget(self.find_status)

        close_btn = QPushButton('')
        close_btn.setProperty('kantIcon', 'close')
        close_btn.setIcon(draw_icon('close', 14))
        close_btn.setIconSize(QSize(14, 14))
        close_btn.setFixedWidth(32)
        close_btn.setToolTip('Chiudi la barra di ricerca (Esc)')
        close_btn.clicked.connect(self._hide_find_bar)
        layout.addWidget(close_btn)

        escape = QShortcut(QKeySequence(Qt.Key_Escape), self.find_input)
        escape.setContext(Qt.WidgetShortcut)
        escape.activated.connect(self._hide_find_bar)

        self._style_find_bar()
        return bar
    # [FN CLOSED] _build_find_bar

    def _style_find_bar(self):
        self.find_bar.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        self.find_input.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:{theme.RADIUS}px; padding:5px 8px;'
        )
        icon_button_style = theme.icon_button_style()
        for btn in self.find_bar.findChildren(QPushButton):
            btn.setStyleSheet(icon_button_style if not btn.text() else theme.BUTTON_STYLE)
            kind = btn.property('kantIcon')
            if kind:
                btn.setIcon(draw_icon(kind, 14))

    def _show_find_bar(self):
        self.find_bar.setVisible(True)
        self.find_input.setFocus()
        self.find_input.selectAll()

    def _hide_find_bar(self):
        self.find_bar.setVisible(False)

    # [FN CATEGORY] _build_vim_command_bar — the : command line vim mode opens (CodeEdit's ':' key,
    # routed through vim_action('open_command_bar')): a bare "line 1" of ex-mode, not a general
    # command interpreter — only the handful of commands worth having without a real command
    # language (:w save, :q close tab, :wq/:x save+close, :qa close all).
    # [FN] _build_vim_command_bar — builds the ":" command-line widget
    # [FN OPEN] _build_vim_command_bar
    def _build_vim_command_bar(self):
        bar = QWidget()
        self.vim_command_bar = bar
        bar.setVisible(False)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)

        prefix = QLabel(':')
        prefix.setFont(QFont('Consolas', theme.CODING_FONT_PT, QFont.DemiBold))
        layout.addWidget(prefix)

        self.vim_command_input = QLineEdit()
        self.vim_command_input.setPlaceholderText('w, q, wq, x, qa…')
        self.vim_command_input.setFont(QFont('Consolas', theme.CODING_FONT_PT))
        self.vim_command_input.returnPressed.connect(self._run_vim_command)
        layout.addWidget(self.vim_command_input, 1)

        escape = QShortcut(QKeySequence(Qt.Key_Escape), self.vim_command_input)
        escape.setContext(Qt.WidgetShortcut)
        escape.activated.connect(self._hide_vim_command_bar)

        self._style_vim_command_bar()
        return bar
    # [FN CLOSED] _build_vim_command_bar

    def _style_vim_command_bar(self):
        self.vim_command_bar.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        self.vim_command_input.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:{theme.RADIUS}px; padding:5px 8px;'
        )

    def _show_vim_command_bar(self):
        self.vim_command_bar.setVisible(True)
        self.vim_command_input.clear()
        self.vim_command_input.setFocus()

    def _hide_vim_command_bar(self):
        self.vim_command_bar.setVisible(False)
        page = self.active_page
        if page is not None:
            focus_target = page.findChild(CodeEdit) or page
            focus_target.setFocus()

    # [FN] _run_vim_command — parses and executes the : command line, then hides the bar; an
    # unrecognized command is silently ignored, same as real vim's "E492 Not an editor command"
    # being just a status-line message rather than anything disruptive
    # [FN OPEN] _run_vim_command
    def _run_vim_command(self):
        command = self.vim_command_input.text().strip()
        self._hide_vim_command_bar()
        if command in ('w', 'write'):
            self._save_file()
        elif command in ('q', 'quit'):
            self._close_active_tab()
        elif command in ('wq', 'x'):
            self._save_file()
            self._close_active_tab()
        elif command == 'qa':
            self._close_all_tabs()
    # [FN CLOSED] _run_vim_command

    # [FN CATEGORY] _find_in_view — searches every CodeEdit in the current center view in document
    # order, continuing from the currently focused widget's cursor and wrapping across widget
    # boundaries and back to the start; reuses QPlainTextEdit's own .find() rather than hand-rolling
    # text search
    # [FN] _find_in_view — moves to the next/previous match across all visible code blocks
    # [FN OPEN] _find_in_view
    def _find_in_view(self, forward=True):
        text = self.find_input.text()
        page = self.active_page
        if not text or page is None:
            return
        widgets = page.findChildren(CodeEdit)
        if not widgets:
            self.find_status.setText('Nessun risultato')
            return
        if self._find_widget_index >= len(widgets):
            self._find_widget_index = 0
        flags = QTextDocument.FindFlags()
        if not forward:
            flags |= QTextDocument.FindBackward
        for offset in range(len(widgets) + 1):
            idx = (self._find_widget_index + offset) % len(widgets)
            widget = widgets[idx]
            if offset > 0:
                cursor = widget.textCursor()
                cursor.movePosition(QTextCursor.Start if forward else QTextCursor.End)
                widget.setTextCursor(cursor)
            if widget.find(text, flags):
                self._find_widget_index = idx
                widget.setFocus()
                scroll_area = page.scroll_area if isinstance(page, FileTab) else page
                scroll_area.ensureWidgetVisible(widget, 50, 80)
                self.find_status.setText('')
                return
        self.find_status.setText('Nessun risultato')
    # [FN CLOSED] _find_in_view

    def _find_next(self):
        self._find_in_view(forward=True)

    def _find_prev(self):
        self._find_in_view(forward=False)

    # [FN CATEGORY] _show_command_palette — builds its entries by introspecting the title bar's own
    # menus (File/Cerca/Aspetto/LSP/Git) rather than a separately hand-maintained action list, so a
    # future menu action is automatically in the palette with no second registration to forget.
    # Disabled/separator actions are skipped; picking an entry just calls QAction.trigger(), reusing
    # every action's existing wiring untouched.
    # [FN] _show_command_palette — Ctrl+Shift+P: fuzzy-filtered list of every menu action
    # [FN OPEN] _show_command_palette
    def _show_command_palette(self):
        entries = []
        for prefix, menu_btn in (
            ('File', self.title_bar.file_menu_btn),
            ('Cerca', self.title_bar.search_menu_btn),
            ('Aspetto', self.title_bar.appearance_menu_btn),
            ('LSP', self.title_bar.lsp_menu_btn),
            ('Git', self.title_bar.git_menu_btn),
        ):
            for action in menu_btn.menu().actions():
                if action.isSeparator() or not action.isEnabled():
                    continue
                entries.append((f'{prefix}: {action.text()}', action))
        chosen = self._ide_command_palette(entries)
        if chosen is not None:
            chosen.trigger()
    # [FN CLOSED] _show_command_palette

    def _search_project(self):
        if not self.project_root_path:
            return
        needle, ok = self._ide_text('Cerca nel progetto', 'Testo da cercare:')
        if not ok or not needle:
            return
        project_root = self.project_root_path

        self._run_background(
            lambda: search_project(project_root, needle),
            lambda matches, error: self._show_search_results(needle, matches or [])
            if not error and self.project_root_path == project_root else None,
        )

    def _show_search_results(self, needle, matches):
        self.results_view.clear()
        title = f'Cerca {needle!r}: {len(matches)} risultato/i'
        if len(matches) >= 200:
            title += ' (primi 200)'
        root = QTreeWidgetItem(self.results_view, [title])
        for path, rel, lineno, line in matches:
            item = QTreeWidgetItem(root, [f'{rel}:{lineno}: {line}'])
            item.setData(0, ROLE_KIND, 'search-result')
            item.setData(0, ROLE_PATH, path)
            item.setData(0, ROLE_LINE, lineno)
            item.setData(0, ROLE_TEXT, line)
        root.setExpanded(True)
        self._toggle_info_popup(self.results_view, force_open=True)
        if not matches:
            QTreeWidgetItem(root, ['Nessun risultato'])

    def _open_result_item(self, item, _column):
        if item.data(0, ROLE_KIND) not in (
            'search-result', 'validation-result', 'lsp-result', 'diagnostic-result', 'test-result',
        ):
            return
        path = item.data(0, ROLE_PATH)
        line = item.data(0, ROLE_LINE) or 1
        if not path or not self._open_file(path):
            return
        tab = self.open_tabs.get(path)
        if not self._focus_visible_text(tab, item.data(0, ROLE_TEXT) or ''):
            self._goto_line(tab, int(line))

    # [FN CATEGORY] _show_kant_error_help — explanation fallback for KANT errors that require human
    # judgment. Resolvable rows bypass it in _on_kant_error_double_clicked and run their fix.
    # [FN] _show_kant_error_help — explains a KANT validation error, offers to fix it
    # [FN OPEN] _show_kant_error_help
    def _show_kant_error_help(self, item, _column):
        if item.data(0, ROLE_KIND) != 'validation-result':
            return
        message = item.data(0, ROLE_TEXT) or item.text(0)
        key, explanation, fix = _kant_error_lookup(message)
        count = self._kant_error_pattern_counts.get(key, 1) if key else 1
        fix_label = {'ai-fill': 'Compila con AI'}.get(fix)
        action = self._ide_kant_error_help(message, explanation, count, fix_label)
        if action == 'goto':
            self._open_result_item(item, 0)
        elif action == 'fix':
            self._open_result_item(item, 0)
            if fix == 'ai-fill':
                self._ai_fill_kant_blanks()
    # [FN CLOSED] _show_kant_error_help

    def _on_kant_error_double_clicked(self, item, _column):
        if item.data(0, ROLE_KIND) != 'validation-result':
            return
        message = item.data(0, ROLE_TEXT) or item.text(0)
        _key, _explanation, fix = _kant_error_lookup(message)
        if fix == 'ai-fill':
            self._open_result_item(item, 0)
            self._ai_fill_kant_blanks()
        elif fix == 'repair-marker':
            self._repair_kant_error_item(item)
        elif fix == 'sync-map':
            self._sync_kant_map()
            self.statusBar().showMessage('Rigenerazione della mappa KANT avviata', 4000)
        else:
            self._show_kant_error_help(item, 0)

    def _repair_kant_error_item(self, item):
        path = item.data(0, ROLE_PATH)
        line = int(item.data(0, ROLE_LINE) or 1)
        message = item.data(0, ROLE_TEXT) or item.text(0)
        if not path:
            return
        tab = self._tab_for_path(path)
        try:
            if tab is not None:
                text = serialize_kant(tab.tree)
            else:
                with open(path, encoding='utf-8', newline='') as source:
                    text = source.read()
            repaired = repair_kant_error(text, line, message)
            if repaired is None:
                self._show_kant_error_help(item, 0)
                return
            try:
                new_tree = parse_kant(repaired)
            except KantParseError:
                # repair_kant_error only permits this when another independent error occurs later;
                # keep the corrected text editable and let the next validation expose that error.
                new_tree = Node(tag='ROOT', name='', open_raw=None, body=[Run(lines=repaired.split('\n'))])
            if tab is None:
                write_file_atomic(path, repaired)
                self._open_file(path)
            else:
                tab.remember_undo_state()
                tab.tree = new_tree
                tab.mark_dirty()
                self._render_view(tab, tab.filter_uid)
                if not tab.save():
                    return
                self._update_tab_title(tab)
            self.statusBar().showMessage('Errore KANT risolto', 4000)
            self._refresh_after_fs_change()
            self._run_kant_validation_background()
        except (OSError, UnicodeDecodeError, KantParseError) as error:
            self._ide_message('Fix KANT', f'Impossibile applicare il fix: {error}')

    def _focus_visible_text(self, tab, text):
        if tab is None or not text:
            return False
        for edit in tab.view_container.findChildren(CodeEdit):
            cursor = edit.textCursor()
            cursor.movePosition(QTextCursor.Start)
            edit.setTextCursor(cursor)
            if edit.find(text):
                edit.setFocus()
                tab.scroll_area.ensureWidgetVisible(edit, 50, 80)
                return True
        return False

    def _goto_line(self, tab, line):
        if tab is None:
            return
        line = max(1, line)
        for edit in tab.view_container.findChildren(CodeEdit):
            first = edit.absolute_line_number(0)
            last = edit.absolute_line_number(max(0, edit.blockCount() - 1))
            if not first <= line <= last:
                continue
            cursor = edit.textCursor()
            cursor.movePosition(QTextCursor.Start)
            for _ in range(line - first):
                cursor.movePosition(QTextCursor.Down)
            edit.setTextCursor(cursor)
            edit.setFocus()
            tab.scroll_area.ensureWidgetVisible(edit, 50, 80)
            return

    def _replace_project(self):
        if not self.project_root_path:
            return
        needle, ok = self._ide_text('Sostituisci nel progetto', 'Testo da sostituire:')
        if not ok or not needle:
            return
        replacement, ok = self._ide_text('Sostituisci nel progetto', 'Nuovo testo:')
        if not ok:
            return
        if not self._flush_all_tabs():
            return
        project_root = self.project_root_path

        self._run_background(
            lambda: scan_project_replace(project_root, needle, replacement),
            lambda changes, error: self._finish_project_replace(needle, replacement, changes or [])
            if not error and self.project_root_path == project_root else None,
        )

    def _finish_project_replace(self, needle, replacement, changes):
        if not self._flush_all_tabs():
            return
        if any(file_fingerprint(path) != fingerprint for path, _text, _count, fingerprint in changes):
            self._ide_message('Sostituisci nel progetto', 'Alcuni file sono cambiati durante la scansione. Ripeti la sostituzione.')
            return
        total = sum(count for _path, _text, count, _fingerprint in changes)
        if not total:
            self.terminal.write_info(f'\n# Sostituisci progetto: {needle!r}\nNessuna occorrenza\n')
            return
        if not self._ide_yes_no(
            'Sostituisci nel progetto',
            f'Sostituire {total} occorrenze in {len(changes)} file?',
        ):
            return
        changed_paths = set()
        for path, text, _count, _fingerprint in changes:
            write_file_atomic(path, text)
            changed_paths.add(os.path.abspath(path))
        for path, tab in list(self.open_tabs.items()):
            if os.path.abspath(path) not in changed_paths:
                continue
            try:
                with open(path, 'r', encoding='utf-8', newline='') as f:
                    tab.tree = parse_kant(f.read())
            except OSError:
                continue
            except KantParseError as e:
                self.terminal.write_info(f'\n# ATTENZIONE: {os.path.basename(path)} ha marcatori KANT non validi dopo la sostituzione: {e}\n')
                continue
            tab.dirty = False
            tab.disk_fingerprint = file_fingerprint(path)
            self._render_view(tab)
            self._update_tab_title(tab)
        self._refresh_after_fs_change()
        self.terminal.write_info(f'\n# Sostituisci progetto: {needle!r} -> {replacement!r}\n{total} occorrenze in {len(changes)} file\n')

    # [FN CATEGORY] _set_view_mode — switches how the LEFT project tree is built: "code" labels every
    # file/section by its KANT tag (the convention-driven view), "file" is a classic PyCharm-style
    # plain file/folder browser by real name. The center code view is unaffected either way — it
    # always shows the currently open file's KANT structure.
    # [FN] _set_view_mode — changes the project tree's display mode and rebuilds it
    # [FN OPEN] _set_view_mode
    def _set_view_mode(self, mode):
        if mode == self.view_mode:
            return
        self.view_mode = mode
        self._style_view_mode_bar()
        self.tree.setStyleSheet(self._tree_stylesheet())
        self._rebuild_tree()
        self._update_tree_drop_handler()
    # [FN CLOSED] _set_view_mode

    # [FN CATEGORY] _update_tree_drop_handler — ProjectTree only shows a "droppable" cursor and
    # accepts an OS drag when file_drop_handler is not None, so this has to actually toggle it (not
    # just have the handler itself no-op) — otherwise File mode's drop target would keep the
    # droppable cursor in KANT/Gruppi mode too, promising a drop that then silently did nothing.
    # [FN] _update_tree_drop_handler — enables tree file-drop only while in File view mode
    # [FN OPEN] _update_tree_drop_handler
    def _update_tree_drop_handler(self):
        file_mode = self.view_mode == 'file'
        self.tree.file_drop_handler = self._handle_tree_file_drop if file_mode else None
        # KANT elements are only reorderable while the tree is actually showing KANT structure —
        # File/Gruppi mode rows aren't KANT elements at all (plain filenames / grouping members)
        code_mode = self.view_mode == 'code'
        self.tree.setDragEnabled(code_mode)
        self.tree.setDragDropMode(
            QAbstractItemView.InternalMove if code_mode else
            QAbstractItemView.DropOnly if file_mode else QAbstractItemView.NoDragDrop
        )
        self.tree.reorder_allowed = self._kant_reorder_allowed if code_mode else None
        self.tree.reorder_handler = self._kant_reorder_apply if code_mode else None
    # [FN CLOSED] _update_tree_drop_handler

    # [FN] _kant_reorder_allowed — a KANT element may only be dragged onto/beside another element
    # sharing its exact parent (same file, same nesting level) — reparenting or reordering across
    # files/levels via drag is out of scope, this only reshuffles siblings that are already siblings
    # [FN OPEN] _kant_reorder_allowed
    def _kant_reorder_allowed(self, dragged, target):
        if dragged.data(0, ROLE_KIND) != 'section':
            return False
        if target is None or target.data(0, ROLE_KIND) != 'section':
            return False
        return target.parent() is dragged.parent()
    # [FN CLOSED] _kant_reorder_allowed

    # [FN CATEGORY] _kant_reorder_apply — ProjectTree has already moved the tree ITEMS by the time
    # this runs; this reads the resulting sibling
    # order and reorders the matching Node entries in the real parsed tree to match, then saves.
    # Only the Node entries move — any Run (plain-text/comment) siblings interleaved between them
    # stay in their exact original slot, so a reorder's diff is just the moved marker blocks, not a
    # wholesale rewrite of everything between them.
    # [FN] _kant_reorder_apply — reorders the underlying source to match a tree drag-reorder, saves
    # [FN OPEN] _kant_reorder_apply
    def _kant_reorder_apply(self, parent_item, ordered_items):
        if parent_item is None:
            self._rebuild_tree()  # top-level items are files, not reorderable — undo Qt's own move
            return
        path = parent_item.data(0, ROLE_PATH)
        if not path:
            return
        tab = self.open_tabs.get(path)
        if tab is None:
            if not self._open_file(path):
                self._rebuild_tree()
                return
            tab = self.open_tabs.get(path)
        if parent_item.data(0, ROLE_KIND) == 'file':
            parent_node = next((n for n in tab.tree.body if isinstance(n, Node)), None)
        else:
            parent_node = self._find_node_by_uid(tab.tree, parent_item.data(0, ROLE_UID))
        if parent_node is None:
            self._rebuild_tree()
            return
        wanted_uids = [it.data(0, ROLE_UID) for it in ordered_items if it.data(0, ROLE_KIND) == 'section']
        node_by_uid = {n.uid: n for n in parent_node.body if isinstance(n, Node)}
        if set(node_by_uid) != set(wanted_uids):
            # the tree the drag saw doesn't match what's actually in the parsed source anymore
            # (e.g. an external edit landed in between) — refuse to reorder against stale
            # assumptions rather than silently scrambling something the drag didn't actually see
            self._rebuild_tree()
            return
        new_nodes = iter(node_by_uid[uid] for uid in wanted_uids)
        parent_node.body = [item if not isinstance(item, Node) else next(new_nodes) for item in parent_node.body]
        tab.remember_undo_state()
        tab.mark_dirty()
        tab.save()
        self._render_view(tab, tab.filter_uid)
        self._rebuild_tree()
    # [FN CLOSED] _kant_reorder_apply

    # [FN CATEGORY] _handle_tree_file_drop — copies each dropped OS path into the project (a
    # directory drop target if the drop landed on a folder row, its parent directory if it landed
    # on a file row, the project root otherwise). Copies, never moves — dragging in from Explorer
    # shouldn't remove the source file the user still has selected there. Existing-name collisions
    # are skipped with a message rather than silently overwritten.
    # [FN] _handle_tree_file_drop — copies dropped files/folders into the project tree
    # [FN OPEN] _handle_tree_file_drop
    def _handle_tree_file_drop(self, paths, item):
        if not self.project_root_path:
            return
        target_dir = self.project_root_path
        if item is not None:
            kind = item.data(0, ROLE_KIND)
            item_path = item.data(0, ROLE_PATH)
            if kind == 'dir':
                target_dir = item_path
            elif item_path:
                target_dir = os.path.dirname(item_path)
        skipped = []
        copied = 0
        for src in paths:
            name = os.path.basename(src.rstrip('/\\'))
            dest = os.path.join(target_dir, name)
            if os.path.exists(dest):
                skipped.append(name)
                continue
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dest)
                else:
                    shutil.copy2(src, dest)
                copied += 1
            except OSError as e:
                skipped.append(f'{name} ({e})')
        if copied:
            self._rebuild_tree()
        if skipped:
            self._ide_message(
                'Copia file', 'Non copiati (nome già esistente nella cartella di destinazione):\n' + '\n'.join(skipped),
            )
    # [FN CLOSED] _handle_tree_file_drop

    # [FN CATEGORY] _rebuild_tree — rebuilds the left project tree for the current folder using
    # whichever builder matches self.view_mode; shared by folder-open, mode-switch and theme-switch
    # [FN] _rebuild_tree — rebuilds the project tree according to the current view mode
    # [FN OPEN] _rebuild_tree
    def _rebuild_tree(self, refresh_git=True):
        self.tree.clear()
        if not self.project_root_path:
            return
        if refresh_git:
            self._refresh_git_status()
        if self.view_mode == 'code':
            self._build_project_tree(self.tree.invisibleRootItem(), self.project_root_path)
        elif self.view_mode == 'groups':
            self._build_groupings_tree(self.tree.invisibleRootItem(), self.project_root_path)
        else:
            self._build_plain_project_tree(self.tree.invisibleRootItem(), self.project_root_path)
        self.tree._rewrap_labels()
    # [FN CLOSED] _rebuild_tree

    def _build_io_tabs(self):
        # INCOMING lists who references the selected element (what comes IN, and from where);
        # OUTGOING lists what the selected element references (what goes OUT, and to where). Both
        # are lists, not text, so a row can be double-clicked to jump straight to that element.
        self.incoming_view = QListWidget()
        self.incoming_view.itemActivated.connect(self._open_xref_item)
        self.incoming_view.itemDoubleClicked.connect(self._open_xref_item)

        self.outgoing_view = QListWidget()
        self.outgoing_view.itemActivated.connect(self._open_xref_item)
        self.outgoing_view.itemDoubleClicked.connect(self._open_xref_item)

        self.results_view = QTreeWidget()
        self.results_view.setHeaderLabels(['Risultati'])
        self.results_view.header().setStretchLastSection(True)
        self.results_view.header().setMinimumSectionSize(80)
        self.results_view.itemDoubleClicked.connect(self._open_result_item)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # a thin top row for just the close button, so it sits at the same height as whichever
        # view's own header is showing (results_view's "Risultati") instead of down with the
        # INCOMING/OUTGOING row at the bottom — hidden/shown together with info_popup itself, since
        # there's nothing to close while the panel is collapsed
        self.info_popup_top_bar = QWidget()
        top_bar_layout = QHBoxLayout(self.info_popup_top_bar)
        top_bar_layout.setContentsMargins(6, 2, 6, 2)
        top_bar_layout.addStretch(1)
        self.info_popup_close_btn = QPushButton('')
        self.info_popup_close_btn.setIcon(draw_icon('close', 14))
        self.info_popup_close_btn.setIconSize(QSize(14, 14))
        self.info_popup_close_btn.setCursor(Qt.PointingHandCursor)
        self.info_popup_close_btn.setToolTip('Chiudi questo pannello')
        self.info_popup_close_btn.clicked.connect(self._close_info_popup)
        top_bar_layout.addWidget(self.info_popup_close_btn)
        self.info_popup_top_bar.setFixedHeight(26)
        self.info_popup_top_bar.setVisible(False)
        layout.addWidget(self.info_popup_top_bar)

        self.info_popup = QStackedWidget()
        self.info_popup.addWidget(self.incoming_view)
        self.info_popup.addWidget(self.outgoing_view)
        self.info_popup.addWidget(self.results_view)
        self.info_popup.setVisible(False)
        layout.addWidget(self.info_popup, 1)

        label_bar = QWidget()
        label_layout = QHBoxLayout(label_bar)
        label_layout.setContentsMargins(10, 4, 10, 4)
        label_layout.setSpacing(16)
        self.incoming_label_btn = QPushButton('INCOMING')
        self.incoming_label_btn.setCheckable(True)
        self.incoming_label_btn.setToolTip("Elenca chi fa riferimento all'elemento selezionato, e da dove")
        self.incoming_label_btn.clicked.connect(lambda: self._toggle_info_popup(self.incoming_view))
        self.outgoing_label_btn = QPushButton('OUTGOING')
        self.outgoing_label_btn.setCheckable(True)
        self.outgoing_label_btn.setToolTip("Elenca a cosa fa riferimento l'elemento selezionato, e dove")
        self.outgoing_label_btn.clicked.connect(lambda: self._toggle_info_popup(self.outgoing_view))
        for btn in (self.incoming_label_btn, self.outgoing_label_btn):
            label_layout.addWidget(btn)
        # MAPPA sits right after INCOMING/OUTGOING, styled the same way — clicking it doesn't
        # open the map directly, it expands two lateral options (LOCAL/GLOBAL) inline in this
        # same bar; picking one opens the map in that scope and collapses the options back down
        self.mappa_label_btn = QPushButton('MAPPA')
        self.mappa_label_btn.setCheckable(True)
        self.mappa_label_btn.setToolTip('Apri la mappa grafica delle dipendenze del progetto')
        self.mappa_label_btn.clicked.connect(self._toggle_mappa_options)
        label_layout.addWidget(self.mappa_label_btn)

        self.mappa_options_row = QWidget()
        mappa_options_layout = QHBoxLayout(self.mappa_options_row)
        mappa_options_layout.setContentsMargins(0, 0, 0, 0)
        mappa_options_layout.setSpacing(4)
        self.mappa_local_btn = QPushButton('LOCAL')
        self.mappa_local_btn.setToolTip("Mappa limitata all'elemento (o modulo) attualmente aperto")
        self.mappa_local_btn.clicked.connect(lambda: self._open_mappa(True))
        self.mappa_global_btn = QPushButton('GLOBAL')
        self.mappa_global_btn.setToolTip('Mappa completa del progetto')
        self.mappa_global_btn.clicked.connect(lambda: self._open_mappa(False))
        mappa_options_layout.addWidget(self.mappa_local_btn)
        mappa_options_layout.addWidget(self.mappa_global_btn)
        self.mappa_options_row.setVisible(False)
        label_layout.addWidget(self.mappa_options_row)
        label_layout.addStretch(1)
        # a quick, one-file-scoped action, distinct from /kant-code-map's whole-project sweep —
        # moved here, right beside the label that names what's actually open, since it acts on
        # exactly that scope (whatever's currently isolated in the coding board, or the whole file
        # when nothing is) and used to sit disconnected from it in the top toolbar. Icon/tooltip/
        # behavior swap between the AI-fill-blanks action and the plain deterministic-tagging
        # action — see _style_kant_quick_button/_kant_quick_action.
        ai_fill_btn = QToolButton()
        ai_fill_btn.setText('AI KANT COMMENT')
        ai_fill_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        ai_fill_btn.setIconSize(QSize(16, 16))
        ai_fill_btn.setFixedHeight(theme.CONTROL_H)
        ai_fill_btn.setFont(QFont('Consolas', theme.TREE_FONT_PT - 2))
        ai_fill_btn.setCursor(Qt.PointingHandCursor)
        ai_fill_btn.clicked.connect(self._kant_quick_action)
        label_layout.addWidget(ai_fill_btn)
        self.action_toolbar_buttons['sparkle'] = ai_fill_btn
        # the filename itself lives here, not in the title bar — that slot shows the KANT identity
        # of whatever's isolated instead (see _update_filename_label)
        self.file_path_label = QLabel('')
        self.file_path_label.setStyleSheet(f'color:{theme.DIM}; font-weight:700;')
        self.file_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.file_path_label.setCursor(Qt.IBeamCursor)
        label_layout.addWidget(self.file_path_label)
        layout.addWidget(label_bar)

        panel.setFixedHeight(42)
        self._style_kant_quick_button()
        return panel

    def _style_io_tabs(self):
        list_style = (
            f'QListWidget {{ background:{theme.CODE_BG}; color:{theme.TEXT}; border:none; padding:4px; '
            f'font-family:Consolas; }} '
            f'QListWidget::item {{ padding:5px 8px; border-radius:{theme.RADIUS}px; }} '
            f'QListWidget::item:hover {{ background:{theme.PANEL2}; }} '
            f'QListWidget::item:selected {{ background:{theme.PANEL2}; color:{theme.ACCENT}; }}'
        )
        for view in (self.incoming_view, self.outgoing_view):
            view.setStyleSheet(list_style)
        self.results_view.setStyleSheet(
            f'QTreeWidget {{ background:{theme.CODE_BG}; color:{theme.TEXT}; border:none; padding:6px; }} '
            f'QHeaderView::section {{ background:{theme.PANEL}; color:{theme.TEXT}; border:none; '
            f'border-bottom:1px solid {theme.BORDER}; padding:4px 8px; font-weight:700; }}'
        )
        self.info_popup.setStyleSheet(f'background:{theme.CODE_BG}; border-top:1px solid {theme.BORDER}; border-bottom:1px solid {theme.BORDER};')
        self.info_popup_top_bar.setStyleSheet(f'background:{theme.CODE_BG};')
        self.action_toolbar_buttons['sparkle'].setStyleSheet(
            f'QToolButton {{ background:transparent; color:{theme.DIM}; '
            f'border:1px solid transparent; border-radius:{theme.RADIUS}px; '
            f'padding:3px 9px; font-weight:700; }} '
            f'QToolButton:hover {{ color:{theme.ACCENT}; border-color:{theme.ACCENT}; }} '
            f'QToolButton:pressed {{ background:{theme.ACCENT}; color:#ffffff; }} '
            f'QToolButton:disabled {{ background:transparent; color:{theme.TEXT_DISABLED}; '
            f'border-color:{theme.BORDER_WEAK}; }}'
        )
        self.file_path_label.setStyleSheet(f'color:{theme.DIM}; font-weight:700;')
        self.io_tabs.setStyleSheet(f'background:{theme.PANEL}; border-top:1px solid {theme.BORDER};')
        # underline-tab style, consistent with the file-tab bar and left-panel view-mode tabs —
        # active state is the gold underline, not a filled pill
        for btn in (self.incoming_label_btn, self.outgoing_label_btn, self.mappa_label_btn,
                    self.mappa_local_btn, self.mappa_global_btn):
            btn.setStyleSheet(theme.tab_style())
        self.info_popup_close_btn.setStyleSheet(
            f'QPushButton {{ border:none; background:transparent; color:{theme.DIM}; '
            f'font-size:16px; font-weight:700; padding:0px 4px; }} '
            f'QPushButton:hover {{ color:{theme.TAG_COLORS["TST"]}; }}'
        )
        # map_tab_btn's own corner rounding flips with which edge it's on (bottom of the shell vs
        # top of the map dialog) — handled by _style_map_tab_button, called from _position_map_tab
        # since it already knows which edge, and runs whenever the tab is (re)shown or moved
        self._position_map_tab()
        sizes = self.splitter.sizes() if hasattr(self, 'splitter') else []
        self._style_claude_tab_button(len(sizes) > 1 and sizes[1] == 0)
        self.terminal_sidebar.setStyleSheet(theme.panel_header_style())
        tab_style = theme.tab_style()
        for btn in self.terminal_sidebar_group.buttons():
            btn.setStyleSheet(tab_style)
        self.errors_view.setStyleSheet(f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:none; padding:6px;')
        self.kant_errors_view.setStyleSheet(f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:none; padding:6px;')
        if self.map_dialog is not None:
            self.map_dialog.apply_style()

    # [FN CATEGORY] _build_terminal_dock — a compact horizontal tab header (Terminale/Python/
    # Problemi/KANT, exclusive) switches a QStackedWidget between the real shell (self.terminal), a
    # second TerminalPane running an interactive Python REPL, and a live list of the active file's
    # diagnostics — so the bottom panel isn't only ever the shell. The Python REPL process starts
    # lazily on first switch to that tab, not at construction, since most sessions never open it.
    # [FN] _build_terminal_dock — header tabs + stacked terminal/REPL/errors panel
    # [FN OPEN] _build_terminal_dock
    def _build_terminal_dock(self):
        self.terminal = TerminalPane(os.getcwd())
        self.python_terminal = TerminalPane(os.getcwd())
        self.errors_view = QTreeWidget()
        self.errors_view.setHeaderHidden(True)
        self.errors_view.itemClicked.connect(self._open_result_item)
        self.kant_errors_view = QTreeWidget()
        self.kant_errors_view.setHeaderHidden(True)
        self.kant_errors_view.itemClicked.connect(self._open_result_item)
        self.kant_errors_view.itemDoubleClicked.connect(self._on_kant_error_double_clicked)

        self.terminal_stack = QStackedWidget()
        self.terminal_stack.addWidget(self.terminal)
        self.terminal_stack.addWidget(self.python_terminal)
        self.terminal_stack.addWidget(self.errors_view)
        self.terminal_stack.addWidget(self.kant_errors_view)

        # a compact horizontal tab header, not a vertical icon rail — a conventional docked-panel
        # look (Terminale/Python/Problemi/KANT switch terminal_stack's page) so the active pane
        # reads clearly. Mappa's entry point lives in the INCOMING/OUTGOING bar instead (see
        # _build_io_tabs), not here.
        header = QWidget()
        self.terminal_sidebar = header
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(theme.SPACE_1, 0, theme.SPACE_1, 0)
        header_layout.setSpacing(2)
        self.terminal_sidebar_group = QButtonGroup(header)
        self.terminal_sidebar_group.setExclusive(True)
        for index, (icon_name, label, tooltip) in enumerate((
            ('terminal', 'Terminale', 'Terminale'),
            ('repl', 'Python', 'Terminale Python'),
            ('warning', 'Problemi', 'Errori nel file aperto'),
            ('kant', 'KANT', 'Errori convenzione KANT'),
        )):
            btn = QPushButton(f'  {label}')
            btn.setCheckable(True)
            btn.setProperty('kantIcon', icon_name)
            btn.setIcon(draw_icon(icon_name, 14))
            btn.setIconSize(QSize(14, 14))
            btn.setFixedHeight(theme.CONTEXTBAR_H)
            btn.setToolTip(tooltip)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, i=index: self._switch_terminal_tab(i))
            self.terminal_sidebar_group.addButton(btn, index)
            header_layout.addWidget(btn)
        header_layout.addStretch(1)
        self.terminal_sidebar_group.button(0).setChecked(True)

        dock = QWidget()
        dock_layout = QVBoxLayout(dock)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        dock_layout.setSpacing(0)
        dock_layout.addWidget(header)
        dock_layout.addWidget(self.terminal_stack, 1)
        return dock
    # [FN CLOSED] _build_terminal_dock

    def _switch_terminal_tab(self, index):
        self.terminal_stack.setCurrentIndex(index)
        if index == 1 and self.python_terminal.process is None:
            self.python_terminal.run_python_repl(self._active_python())
        elif index == 3:
            self._run_kant_validation_background()

    def _toggle_info_popup(self, widget, force_open=False):
        if self.info_popup.currentWidget() is widget and self.info_popup.isVisible() and not force_open:
            self._close_info_popup()
            return
        self.info_popup.setCurrentWidget(widget)
        self.info_popup.setVisible(True)
        self.info_popup_top_bar.setVisible(True)
        self.io_tabs.setFixedHeight(200)
        self.incoming_label_btn.setChecked(widget is self.incoming_view)
        self.outgoing_label_btn.setChecked(widget is self.outgoing_view)

    def _close_info_popup(self):
        self.info_popup.setVisible(False)
        self.info_popup_top_bar.setVisible(False)
        self.io_tabs.setFixedHeight(42)
        self.incoming_label_btn.setChecked(False)
        self.outgoing_label_btn.setChecked(False)

    def _toggle_mappa_options(self):
        self.mappa_options_row.setVisible(not self.mappa_options_row.isVisible())
        self.mappa_label_btn.setChecked(self.mappa_options_row.isVisible())

    def _open_mappa(self, local):
        self.mappa_options_row.setVisible(False)
        self.mappa_label_btn.setChecked(False)
        self._open_xref_window(local=local)

    # [FN] _current_map_local_key — the xref key LOCAL scope should drill into: the element
    # currently isolated in the coding board, or its nearest container if that element is itself
    # a leaf with no children of its own (a lone FN has nothing to show "inside" it — its parent
    # does). Falls back to the file's own top-level node when nothing is isolated, same lookup
    # _update_filename_label already uses for "what am I looking at right now".
    def _current_map_local_key(self):
        tab = self.active_tab
        if tab is None or not self.project_root_path:
            return None
        uid = self._active_filter_uid()
        node = self._find_node_by_uid(tab.tree, uid) if uid else None
        if node is None:
            node = next((item for item in tab.tree.body if isinstance(item, Node)), None)
        if node is None:
            return None
        rel = os.path.relpath(tab.path, self.project_root_path).replace(os.sep, '/')
        key = f'{rel}::{node.uid}'
        xref = self._get_xref()
        element = xref.get(key)
        if element is None:
            return None
        if not any(e.parent == key for e in xref.values()) and element.parent:
            return element.parent
        return key

    # [FN CATEGORY] _open_xref_window — MAPPA opens the cross-reference graph in a dialog internal to
    # the IDE (parented to the main window, floating over the editor — not a strip in the coding pane
    # nor a separate OS window), kept as a single reused instance and raised if already open. Rebuilds
    # the graph from the (cache-backed) xref on every open so it reflects the current code, and wires
    # double-click-a-node back to _navigate_to_element so the map doubles as a jump-to launcher. The
    # Its toolbar exit button routes through _toggle_xref_window so closing still navigates to the
    # selected element.
    # [FN] _open_xref_window — opens/raises the internal cross-reference map dialog. local=True
    # drills straight into the current element's containing parent (see _current_map_local_key);
    # local=False (GLOBAL) is the classic full-project view, explicitly exiting drill mode so a
    # later GLOBAL open never leaks a LOCAL drill left over from an earlier session with the dialog.
    # [FN OPEN] _open_xref_window
    def _open_xref_window(self, local=False):
        if self.map_dialog is None:
            self.map_dialog = XrefMapDialog(self)
            self.map_dialog.nodeActivated.connect(self._navigate_to_element)
            self.map_dialog.closeRequested.connect(self._toggle_xref_window)
        self.map_dialog.apply_style()
        project_name = os.path.basename(self.project_root_path) if self.project_root_path else ''
        # the very first open of a project whose xref graph hasn't finished its background build
        # yet (_get_xref returns {} and schedules the build) shows a spinner instead of an
        # unexplained empty graph; _schedule_xref_build's own apply() callback re-calls set_graph
        # with the real data (and no loading flag) once the build actually completes
        still_building = self._xref_cache is None
        self.map_dialog.set_graph(self._get_xref(), project_name, self.project_root_path or '', loading=still_building)
        drill_key = self._current_map_local_key() if local else None
        if drill_key:
            self.map_dialog._enter_drill_mode(drill_key)
        else:
            self.map_dialog._exit_drill_mode()
        self.map_dialog.show()
        self._position_map_dialog()
        self.map_dialog.raise_()
        self.map_dialog.activateWindow()
        self.map_tab_btn.hide()
    # [FN CLOSED] _open_xref_window

    def _toggle_xref_window(self):
        if self.map_dialog is not None and self.map_dialog.isVisible():
            key = self.map_dialog.selected_key()
            self.map_dialog.hide()
            # back on the shell, but hidden — Mappa's entry point is the MAPPA button in the
            # INCOMING/OUTGOING bar (see _build_io_tabs); the map's own toolbar closes it.
            self.map_tab_btn.setParent(self.shell)
            self.map_tab_btn.setText(' MAPPA')
            self.map_tab_btn.setProperty('kantIcon', 'arrow-up')
            self.map_tab_btn.setIcon(draw_icon('arrow-up', 12))
            self.map_tab_btn.hide()
            self._position_map_tab()
            if key:
                self._navigate_to_element(key)
            return
        self._open_xref_window()

    # [FN CATEGORY] _start_import_edit — "Modifica import"'s entry point (CodeEdit.contextMenuEvent,
    # right-click on an import line). Python-only: resolving a library's real on-disk source needs an
    # interpreter that actually has it installed, and the project's active interpreter (_active_python,
    # already used for run/debug/REPL) is the only one this app already knows how to ask. A local copy
    # is made once per module (kant_local_imports/<flat_name>.py, flattened since only the one resolved
    # file is copied — not the whole package) and reused on a later "Modifica import" of the same
    # module, so re-editing it never clobbers earlier customization.
    # [FN] _start_import_edit — resolves the module's source, makes/reuses the local copy, opens the editor
    # [FN OPEN] _start_import_edit
    def _start_import_edit(self, edit, module, line_text):
        if not self.project_root_path:
            self._ide_message('Modifica import', 'Apri un progetto prima di modificare un import.')
            return
        tab = getattr(edit, 'kant_tab', None)
        if tab is None or Path(tab.path).suffix.lower() != '.py':
            self._ide_message(
                'Modifica import',
                'La modifica degli import è disponibile solo per file Python al momento.',
            )
            return
        python = self._active_python()
        try:
            result = subprocess.run(
                [python, '-c', (
                    'import importlib.util\n'
                    f'spec = importlib.util.find_spec({module!r})\n'
                    'print(spec.origin or "" if spec else "")'
                )],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self._ide_message('Modifica import', f'Impossibile risolvere "{module}": {e}')
            return
        source_path = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ''
        if result.returncode != 0 or not source_path or not os.path.isfile(source_path):
            self._ide_message(
                'Modifica import',
                f'Impossibile trovare il file sorgente di "{module}" — modulo builtin, estensione C, '
                "o non installato nell'interprete attivo del progetto.",
            )
            return
        flat_name = module.replace('.', '_')
        dest_dir = os.path.join(self.project_root_path, 'kant_local_imports')
        dest_path = os.path.join(dest_dir, f'{flat_name}.py')
        if not os.path.exists(dest_path):
            try:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copy2(source_path, dest_path)
            except OSError as e:
                self._ide_message('Modifica import', f'Impossibile creare la copia locale: {e}')
                return
        from_match = re.match(r'^\s*from\s+[\w.]+\s+import\s+(\w+)', line_text)
        if from_match:
            symbol, is_from = from_match.group(1), True
        else:
            symbol, is_from = module.split('.')[0], False
        self._open_import_edit_dialog(module, symbol, is_from, dest_path)
    # [FN CLOSED] _start_import_edit

    # [FN] _open_import_edit_dialog — coding board + a ClaudePane scoped to just the local copy
    # [FN OPEN] _open_import_edit_dialog
    def _open_import_edit_dialog(self, module, symbol, is_from, dest_path):
        try:
            with open(dest_path, 'r', encoding='utf-8') as f:
                text = f.read()
        except OSError as e:
            self._ide_message('Modifica import', f'Impossibile aprire la copia locale: {e}')
            return
        dialog, outer, body = self._internal_window(f'MODIFICA IMPORT — {module}', 900, 'Chiudi senza continuare')
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        editor_container = QWidget()
        editor_layout = QVBoxLayout(editor_container)
        editor_layout.setContentsMargins(10, 10, 10, 10)
        hint = QLabel(f"Copia locale di \"{module}\" — {dest_path}\nL'import originale non viene mai toccato.")
        hint.setStyleSheet(f'color:{theme.DIM};')
        hint.setWordWrap(True)
        editor_layout.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f'border:none; background:{theme.CODE_BG};')
        local_edit = CodeEdit(text)
        scroll.setWidget(local_edit)
        editor_layout.addWidget(scroll, 1)
        splitter.addWidget(editor_container)

        pane = ClaudePane(os.path.dirname(dest_path))
        pane.context_hint = lambda: (
            f'Stai modificando la copia locale personalizzata dell\'import "{module}", salvata in '
            f'{dest_path}. Modifica solo questo file per soddisfare le richieste dell\'utente.'
        )
        pane.focus_hint = lambda: f'import locale: {module}'
        pane.apply_style()
        splitter.addWidget(pane)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        body.addWidget(splitter)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(14, 10, 14, 14)
        buttons.addStretch(1)
        confirm = QPushButton('Conferma e continua')
        confirm.setToolTip('Salva la copia locale e passa alla scelta di dove usarla')
        confirm.setStyleSheet(theme.BUTTON_STYLE)
        buttons.addWidget(confirm)
        body.addLayout(buttons)
        outer.addLayout(body)
        dialog.setMinimumHeight(560)

        def on_confirm():
            try:
                write_file_atomic(dest_path, local_edit.toPlainText())
            except OSError as e:
                self._ide_message('Modifica import', f'Salvataggio della copia locale fallito: {e}')
                return
            dialog.accept()

        confirm.clicked.connect(on_confirm)
        if dialog.exec() == QDialog.Accepted:
            self._open_import_occurrence_picker(module, symbol, is_from, dest_path)
    # [FN CLOSED] _open_import_edit_dialog

    # [FN CATEGORY] _open_import_occurrence_picker — reuses reference_locations (the same text-scan
    # find-references already backing the no-LSP command palette) to find every file that mentions the
    # imported symbol, then keeps only those that have their OWN import line for this module — that's
    # what "affiancare" (add alongside) needs: a known line to insert right after. A file only ever
    # gets ONE new shadow-import line; Python name resolution takes care of every call site in that
    # file automatically (the shadow import runs after the original, so the local copy wins for
    # anything below it) — there's no such thing as redirecting one call site but not its neighbour
    # in the same file, so the picker operates at file granularity, not line granularity.
    # [FN] _open_import_occurrence_picker — pick which files get the new shadow import
    # [FN OPEN] _open_import_occurrence_picker
    def _open_import_occurrence_picker(self, module, symbol, is_from, dest_path):
        matches = reference_locations(self.project_root_path, symbol, limit=500)
        import_line_re = re.compile(
            rf'^\s*(?:from\s+{re.escape(module)}\s+import\s+|import\s+{re.escape(module)}\b)'
        )
        dest_abs = os.path.abspath(dest_path)
        candidates = {}  # abs path -> (rel_path, import_line_no)
        for path, rel, _lineno, _line in matches:
            if os.path.abspath(path) == dest_abs or path in candidates:
                continue
            tab = self.open_tabs.get(path)
            if tab is not None:
                file_lines = serialize_kant(tab.tree).split('\n')
            else:
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        file_lines = f.read().split('\n')
                except OSError:
                    continue
            import_line_no = next((i for i, l in enumerate(file_lines) if import_line_re.match(l)), None)
            if import_line_no is not None:
                candidates[path] = (rel, import_line_no)
        if not candidates:
            self._ide_message(
                'Modifica import',
                f'Copia locale salvata in {dest_path}.\n'
                f'Nessun altro file ha un proprio import di "{module}" da poter affiancare.',
            )
            return

        dialog, outer, body = self._internal_window('DOVE USARE LA COPIA LOCALE', 560, 'Chiudi senza applicare')
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(10)
        info = QLabel(
            f'Scegli in quali file affiancare un nuovo import verso la copia locale di "{module}" '
            "(quello originale non viene mai modificato)."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f'color:{theme.TEXT};')
        body.addWidget(info)

        list_widget = QTreeWidget()
        list_widget.setHeaderHidden(True)
        list_widget.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER};'
        )
        active_path = getattr(self.active_tab, 'path', None)
        checks = {}
        for path, (rel, import_line_no) in sorted(candidates.items(), key=lambda kv: kv[1][0]):
            item = QTreeWidgetItem(list_widget, [f'{rel}  (riga {import_line_no + 1})'])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if path == active_path else Qt.Unchecked)
            checks[item] = (path, import_line_no)
        body.addWidget(list_widget)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton('Annulla')
        cancel.setStyleSheet(theme.BUTTON_STYLE)
        cancel.clicked.connect(dialog.reject)
        apply_btn = QPushButton('Sostituisci selezionati')
        apply_btn.setStyleSheet(theme.BUTTON_STYLE)
        apply_btn.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply_btn)
        body.addLayout(buttons)
        outer.addLayout(body)

        if dialog.exec() != QDialog.Accepted:
            return
        selected = [checks[item] for item in checks if item.checkState(0) == Qt.Checked]
        if not selected:
            return
        flat_name = os.path.splitext(os.path.basename(dest_path))[0]
        local_module = f'kant_local_imports.{flat_name}'
        new_line = f'from {local_module} import {symbol}' if is_from else f'import {local_module} as {symbol}'
        failures = []
        for path, import_line_no in selected:
            if not self._apply_local_import_shadow(path, import_line_no, new_line):
                failures.append(os.path.relpath(path, self.project_root_path))
        if failures:
            self._ide_message('Modifica import', "Scrittura fallita per:\n" + '\n'.join(failures))
        else:
            self._ide_message('Modifica import', f'Import locale aggiunto in {len(selected)} file.')
    # [FN CLOSED] _open_import_occurrence_picker

    # [FN] _apply_local_import_shadow — inserts one new import line right after an existing one,
    # through the same open-tab-aware save path _kant_reorder_apply already established (in-memory
    # tree when the file's open, so unsaved edits aren't lost; a fresh KantParseError falls back to
    # the plain-text ROOT wrapper _open_file itself uses for a file with no valid KANT markers)
    # [FN OPEN] _apply_local_import_shadow
    def _apply_local_import_shadow(self, path, import_line_no, new_line):
        tab = self.open_tabs.get(path)
        if tab is not None:
            lines = serialize_kant(tab.tree).split('\n')
            lines.insert(import_line_no + 1, new_line)
            new_text = '\n'.join(lines)
            try:
                tab.tree = parse_kant(new_text)
            except KantParseError:
                tab.tree = Node(tag='ROOT', name='', open_raw=None, body=[Run(lines=new_text.split('\n'))])
            tab.mark_dirty()
            ok = tab.save()
            self._render_view(tab, tab.filter_uid)
            return ok
        try:
            with open(path, 'r', encoding='utf-8', newline='') as f:
                lines = f.read().split('\n')
        except OSError:
            return False
        lines.insert(import_line_no + 1, new_line)
        try:
            write_file_atomic(path, '\n'.join(lines))
        except OSError:
            return False
        return True
    # [FN CLOSED] _apply_local_import_shadow

    # [FN CATEGORY] _toggle_claude_pane — flattens the AI terminal pane to zero width via the outer
    # splitter (not hiding the widget) so its running process/state is untouched, and remembers the
    # width it had so restoring gives back the same size instead of an arbitrary default.
    # [FN] _toggle_claude_pane — collapses/restores the AI terminal pane
    # [FN OPEN] _toggle_claude_pane
    def _toggle_claude_pane(self):
        if self.pure_ai_mode:
            return
        sizes = self.splitter.sizes()
        if len(sizes) < 2:
            return
        if sizes[1] > 0:
            self._claude_pane_width = sizes[1]
            self.splitter.setSizes([sizes[0] + sizes[1], 0])
            self.claude_tab_btn.setIcon(draw_icon('arrow-left', 12))
            self.claude_tab_btn.setProperty('kantIcon', 'arrow-left')
            self._style_claude_tab_button(True)
        else:
            restore = self._claude_pane_width or 360
            total = sum(sizes)
            self.splitter.setSizes([max(200, total - restore), restore])
            self.claude_tab_btn.setIcon(draw_icon('arrow-right', 12))
            self.claude_tab_btn.setProperty('kantIcon', 'arrow-right')
            self._style_claude_tab_button(False)
        self._position_claude_tab()
    # [FN CLOSED] _toggle_claude_pane

    def _set_pure_ai_mode(self, enabled):
        enabled = bool(enabled)
        if enabled == self.pure_ai_mode:
            return
        self.pure_ai_mode = enabled
        # PURE AI is project-wide and independent from whichever KANT node is selected in the
        # right board; its hidden prompt must not inherit that board's file/element focus.
        self.claude_pane.global_mode_btn.setVisible(not enabled)
        self.claude_pane.focus_label.setVisible(not enabled)
        if enabled:
            self._normal_splitter_sizes = self.splitter.sizes()
            self._normal_workspace_sizes = self.workspace_splitter.sizes()
            self.workspace_splitter.setOrientation(Qt.Vertical)
            self.workspace_splitter.setSizes([300, 620])
            self.splitter.insertWidget(0, self.conversation_sidebar)
            self.splitter.insertWidget(1, self.claude_pane)
            self.conversation_sidebar.show()
            self.splitter.setCollapsible(1, False)
            self.splitter.setStretchFactor(0, 0)
            self.splitter.setStretchFactor(1, 1)
            self.splitter.setStretchFactor(2, 1)
            self.splitter.setSizes([240, 680, 520])
            self.claude_tab_btn.hide()
            self._refresh_ai_conversation_sidebar()
        else:
            self.conversation_sidebar.hide()
            self.conversation_sidebar.setParent(self.shell)
            self.splitter.insertWidget(0, self.main_splitter)
            self.splitter.insertWidget(1, self.claude_pane)
            self.splitter.setCollapsible(1, True)
            self.workspace_splitter.setOrientation(Qt.Horizontal)
            self.workspace_splitter.setSizes(self._normal_workspace_sizes or [theme.TREE_MIN_WIDTH, 900])
            self.splitter.setStretchFactor(0, 1)
            self.splitter.setStretchFactor(1, 0)
            self.splitter.setSizes(self._normal_splitter_sizes or [1320, 460])
            self.claude_tab_btn.setVisible(self.project_root_path is not None and self.stack.currentIndex() == 1)
            self._position_claude_tab()

    # [FN CATEGORY] _navigate_to_element — resolves an xref key with legacy order fallback, then
    # routes through the same element-preview slot used by direct tree clicks.
    # [FN] _navigate_to_element — opens an xref element in the editor preview
    # [FN OPEN] _navigate_to_element
    def _navigate_to_element(self, key):
        if not key or '::' not in key or not self.project_root_path:
            return
        rel, uid = key.rsplit('::', 1)
        # resolve the target's document order BEFORE opening the file: opening it reparses and (for
        # a legacy file with no #id yet) mints a fresh uid, so the key's uid can't be matched. The
        # order index survives reparse, so it's the reliable fallback; a #id'd file matches by uid.
        element = self._get_xref().get(key)
        target_order = element.order if element is not None else None
        path = os.path.join(self.project_root_path, rel.replace('/', os.sep))
        if not os.path.isfile(path) or not self._open_file(path):
            return
        tab = self.open_tabs.get(path)
        if tab is None:
            return
        node = self._find_node_by_uid(tab.tree, uid)
        if node is None and target_order is not None:
            nodes = self._nodes_in_order(tab.tree)
            if target_order < len(nodes):
                node = nodes[target_order]
        if node is None:
            return
        self._show_element_tab(tab, node.uid)
        self._select_tree_section(path, node.uid)
    # [FN CLOSED] _navigate_to_element

    def _nodes_in_order(self, root):
        # pre-order over tagged nodes — matches kant.xref's own walk, so element.order lines up
        result = []

        def walk(node):
            for item in node.body:
                if isinstance(item, Node):
                    result.append(item)
                    walk(item)

        walk(root)
        return result

    def _select_tree_section(self, path, uid):
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            if item.data(0, ROLE_KIND) == 'section' and item.data(0, ROLE_PATH) == path \
                    and item.data(0, ROLE_UID) == uid:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                self._update_io_tabs(uid)
                return
            it += 1

    def _open_xref_item(self, item):
        self._navigate_to_element(item.data(ROLE_KEY))

    # [FN CATEGORY] _get_xref — lazy cached build of the project cross-reference graph: parses every
    # KANT-tagged file once (same walk _sync_kant_map does) and hands the trees to build_xref. Cache
    # is dropped by _invalidate_xref on save, filesystem change, and project switch — never served
    # stale across an edit, never rebuilt while nothing changed.
    # ponytail: full reparse remains deterministic and now runs off the UI thread.
    # [FN] _get_xref — returns the (cached) project cross-reference graph
    # [FN OPEN] _get_xref
    def _get_xref(self):
        if self._xref_cache is None:
            self._schedule_xref_build()
        return self._xref_cache or {}
    # [FN CLOSED] _get_xref

    def _schedule_xref_build(self):
        if not self.project_root_path or self._xref_pending_generation == self._xref_generation:
            return
        project_root = self.project_root_path
        generation = self._xref_generation
        self._xref_pending_generation = generation
        open_texts = {}
        for path, tab in self.open_tabs.items():
            rel = os.path.relpath(path, project_root).replace(os.sep, '/')
            if not rel.startswith('..'):
                open_texts[rel] = serialize_kant(tab.tree)

        def build():
            trees = {}
            for file_path in iter_kant_tagged_files(project_root):
                rel = os.path.relpath(file_path, project_root).replace(os.sep, '/')
                if rel in open_texts:
                    continue
                label = read_top_level_label(file_path)
                if label is not None:
                    trees[rel] = label[2]
            for rel, text in open_texts.items():
                trees[rel] = parse_kant(text)
            return build_xref(trees)

        def apply(graph, error):
            if self._xref_pending_generation == generation:
                self._xref_pending_generation = None
            if error:
                if generation == self._xref_generation and project_root == self.project_root_path:
                    if self.map_dialog is not None and self.map_dialog.isVisible():
                        self.map_dialog.set_graph({}, os.path.basename(project_root), project_root)
                    self.statusBar().showMessage(f'Impossibile costruire MAPPA: {error}', 5000)
                return
            if generation != self._xref_generation or project_root != self.project_root_path:
                return
            self._xref_cache = graph
            if self.map_dialog is not None and self.map_dialog.isVisible():
                self.map_dialog.set_graph(graph, os.path.basename(project_root), project_root)
            if self.active_tab is not None:
                self._update_io_tabs(self._last_io_uid)

        self._run_background(build, apply)

    def _invalidate_xref(self):
        self._xref_cache = None
        self._xref_generation += 1
        if self.map_dialog is not None and self.map_dialog.isVisible():
            self._schedule_xref_build()

    # [FN CATEGORY] _update_io_tabs — maps the selected node to kant/xref.py, then shows the
    # selected subtree's boundary: INCOMING sources and OUTGOING targets outside that subtree.
    # Internal child-to-child edges are excluded. Each row stores an xref key for navigation.
    # The whole-file view uses its first top-level KANT node, so modules aggregate child edges too.
    # [FN] _update_io_tabs — fills navigable Incoming/Outgoing boundary lists
    # [FN OPEN] _update_io_tabs
    def _update_io_tabs(self, uid):
        self._last_io_uid = uid
        tab = self.active_tab
        if tab is None:
            node = None
        elif uid is not None:
            node = self._find_node_by_uid(tab.tree, uid)
        else:
            # uid is None for the whole-file view (file tree item, or a tab with no section
            # filter) — that view's own element is the file's top-level tagged node, not "nothing
            # selected", so it should aggregate incoming/outgoing the same as any other module.
            node = next((item for item in tab.tree.body if isinstance(item, Node)), None)
        self.incoming_view.clear()
        self.outgoing_view.clear()
        if node is None or not self.project_root_path:
            return
        xref = self._get_xref()
        rel = os.path.relpath(tab.path, self.project_root_path).replace(os.sep, '/')
        element = xref.get(f'{rel}::{node.uid}')
        if element is None:
            return
        subtree_keys = {element.key} | {f'{rel}::{child.uid}' for child in _walk_nodes(node)}
        incoming = {k for key in subtree_keys if key in xref for k in xref[key].incoming} - subtree_keys
        outgoing = {k for key in subtree_keys if key in xref for k in xref[key].outgoing} - subtree_keys

        def fill(view, keys, arrow):
            if not keys:
                placeholder = QListWidgetItem('—')
                placeholder.setFlags(Qt.NoItemFlags)
                view.addItem(placeholder)
                return
            # group by file so "from where" / "to where" reads at a glance; the shown name is the
            # short description (what the left tree shows), and hovering gives the category text
            for target_key in sorted(keys, key=lambda k: (xref[k].file, xref[k].desc)):
                el = xref[target_key]
                # no text passed to the item itself: it's just a data/selection carrier here, the
                # QLabel set below via setItemWidget is the only thing actually drawn — giving the
                # item its own text too used to paint both, since setItemWidget doesn't clear an
                # item's own text/foreground painting underneath the widget it hosts
                item = QListWidgetItem()
                item.setData(ROLE_KEY, target_key)
                item.setToolTip(el.category_desc or 'Doppio clic per aprire')
                view.addItem(item)
                color = theme.TAG_COLORS.get(el.tag, theme.TEXT)
                label = QLabel(
                    f'<span style="color:{theme.DIM}">{arrow}</span> '
                    f'{_tag_header_html(el.tag, el.name, el.desc, bold_name=True)} '
                    f'<span style="color:{theme.DIM}">· {html_escape(el.file)}</span>'
                )
                label.setFont(QFont('Consolas', theme.TREE_FONT_PT))
                label.setStyleSheet(
                    f'color:{theme.TEXT}; background:transparent; padding:4px 8px; '
                    f'border-bottom:2px solid {color};'
                )
                label.setAttribute(Qt.WA_TransparentForMouseEvents)
                # label.sizeHint() alone under-reports the row's real needed height: it's computed
                # on an unparented, unconstrained-width label (rich-text metrics aren't final until
                # actually laid out), and it has no idea about QListWidget::item's OWN padding (5px
                # top+bottom, set in _style_io_tabs) — that padding was eating into, not adding to,
                # too-tight a box, squashing every row's icon/text/underline together
                hint = label.sizeHint()
                hint.setHeight(max(hint.height(), 22) + 10)
                item.setSizeHint(hint)
                view.setItemWidget(item, label)

        fill(self.incoming_view, incoming, '←')   # ← comes from
        fill(self.outgoing_view, outgoing, '→')   # → goes to
    # [FN CLOSED] _update_io_tabs


    # ---- Python interpreter/venv (AI-NAV: kant/pyenv.py owns detection/config, this owns UI) ---

    def _active_python(self):
        """The configured interpreter for the open project, or sys.executable as a fallback —
        the single call every run/debug/test/format/REPL site routes through."""
        if self.project_root_path:
            configured = load_interpreter(self.project_root_path)
            if configured:
                return configured
        return sys.executable

    def _refresh_python_env_label(self):
        if not self.project_root_path:
            self.python_env_label.setVisible(False)
            return
        python_path = self._active_python()
        version = interpreter_version(python_path)
        label = interpreter_label(python_path) if python_path != sys.executable else 'sistema'
        text = f'Python: {label}' + (f' ({version})' if version else '')
        self.python_env_label.setText(text)
        self.python_env_label.setVisible(True)

    # [FN CATEGORY] _auto_select_interpreter — called once per project open: silently picks the
    # first detected venv when nothing is configured yet, no prompt — Python-mode activating with
    # no manual setup is the point. A user who wants a different interpreter still has the status
    # bar button (_select_python_interpreter) to change it any time.
    # [FN] _auto_select_interpreter — auto-picks a detected venv if the project has none configured
    # [FN OPEN] _auto_select_interpreter
    def _auto_select_interpreter(self):
        if not self.project_root_path:
            return
        if not load_interpreter(self.project_root_path):
            candidates = detect_venvs(self.project_root_path)
            if candidates:
                save_interpreter(self.project_root_path, candidates[0])
        self._refresh_python_env_label()
    # [FN CLOSED] _auto_select_interpreter

    def _select_python_interpreter(self):
        if not self.project_root_path:
            self._ide_message('Python', 'Apri prima una cartella di progetto.')
            return
        candidates = detect_venvs(self.project_root_path)
        current = load_interpreter(self.project_root_path)
        chosen = self._ide_python_interpreter_form(candidates, current)
        if not chosen:
            return
        save_interpreter(self.project_root_path, chosen)
        self._refresh_python_env_label()
        self.terminal.write_info(f'\n# Interprete Python: {chosen}\n')

    # [FN CATEGORY] _install_dependencies — requirements.txt gets `pip install -r`; pyproject.toml
    # gets `pip install -e .` (an editable install resolves the project's own declared deps without
    # this needing to parse the TOML itself) — both run through the terminal pane (not
    # _run_background) so the install's own output streams live instead of appearing only once
    # finished.
    # [FN] _install_dependencies — installs requirements.txt/pyproject.toml deps on the active interpreter
    # [FN OPEN] _install_dependencies
    def _install_dependencies(self):
        if not self.project_root_path:
            return
        dep_file = dependency_file(self.project_root_path)
        if not dep_file:
            self._ide_message('Dipendenze', 'Nessun requirements.txt o pyproject.toml trovato in questo progetto.')
            return
        python_path = self._active_python()
        args = (
            [python_path, '-m', 'pip', 'install', '-r', dep_file] if dep_file == 'requirements.txt'
            else [python_path, '-m', 'pip', 'install', '-e', '.']
        )
        command = ' '.join(_quote_arg(a) for a in args)
        self.terminal.run_command(command, self.project_root_path)
    # [FN CLOSED] _install_dependencies

    # [FN CATEGORY] _run_lint_check — ruff first, flake8 as a fallback (same shape as
    # _format_with_external_tool's black/ruff choice), both checked via has_module against the
    # SELECTED interpreter rather than a bare PATH lookup, so this lints with whatever the
    # project's own venv actually has installed, not whatever happens to be on the system PATH.
    # [FN] _run_lint_check — runs ruff check (or flake8) and surfaces results as a clickable list
    # [FN OPEN] _run_lint_check
    def _run_lint_check(self):
        project_root = self.project_root_path
        if not project_root:
            return
        python_path = self._active_python()

        def run():
            if has_module(python_path, 'ruff'):
                return subprocess.run(
                    [python_path, '-m', 'ruff', 'check', '--output-format=concise', '.'],
                    cwd=project_root, capture_output=True, text=True, timeout=60,
                )
            if has_module(python_path, 'flake8'):
                return subprocess.run(
                    [python_path, '-m', 'flake8', '.'],
                    cwd=project_root, capture_output=True, text=True, timeout=60,
                )
            return None

        self.terminal.write_info('\n# Lint: ruff check (o flake8)\n')
        self._run_background(run, lambda result, error: self._finish_lint_check(project_root, result, error))
    # [FN CLOSED] _run_lint_check

    def _finish_lint_check(self, project_root, result, error):
        if error or result is None:
            self.terminal.write_info(
                '\n# Lint: ne ruff ne flake8 sono installati per questo interprete '
                '(pip install ruff, oppure pip install flake8)\n'
            )
            return
        output = (result.stdout or '') + (result.stderr or '')
        self.terminal.write_info(f'\n{output}\n')
        self._show_lint_results(project_root, output)

    # [FN CATEGORY] _show_lint_results — parses ruff's --output-format=concise and flake8's default
    # output, both "path:line:col: message" (flake8) or "path:line:col: CODE message" (ruff) —
    # the trailing column is optional in the regex since it's not needed for navigation, just line.
    # [FN] _show_lint_results — populates results_view with clickable ruff/flake8 findings
    # [FN OPEN] _show_lint_results
    def _show_lint_results(self, project_root, output):
        entries = []
        for match in re.finditer(r'^(?P<path>[^\s:][^:]*):(?P<line>\d+):(?:\d+:)?\s*(?P<message>.+)$', output, re.MULTILINE):
            entries.append((match.group('path'), int(match.group('line')), match.group('message')))
        self.results_view.clear()
        summary = f'Lint: {len(entries)} problema/i' if entries else 'Lint: nessun problema'
        root_item = QTreeWidgetItem(self.results_view, [summary])
        for rel_path, line, message in entries[:200]:
            abs_path = rel_path if os.path.isabs(rel_path) else os.path.join(project_root, rel_path)
            item = QTreeWidgetItem(root_item, [f'{rel_path}:{line}: {message}'])
            item.setData(0, ROLE_KIND, 'diagnostic-result')
            item.setData(0, ROLE_PATH, abs_path)
            item.setData(0, ROLE_LINE, line)
        root_item.setExpanded(True)
        self._toggle_info_popup(self.results_view, force_open=True)
    # [FN CLOSED] _show_lint_results

    def _run_current_file(self):
        tab = self.active_tab
        if tab is None:
            return
        if tab.dirty:
            if not tab.save():
                return
            self._update_tab_title(tab)
        command = run_command_for_path(tab.path, self._active_python())
        if command is None:
            self._ide_message('Run', 'Nessun comando run configurato per questo tipo di file.')
            return
        # the run output always goes to the shell TerminalPane, but nothing forced the dock to
        # actually show it — pressing Run while looking at the Python REPL or Errori tab silently
        # started the command out of view, reading as "Run doesn't do anything"
        self._switch_terminal_tab(0)
        self.terminal.run_command(command, os.path.dirname(tab.path) or None)

    # [FN CATEGORY] _debug_current_file — F5: Python only (the only language this IDE can start a
    # real debugger for without bundling one per language). Gutter breakpoints are stored per
    # CodeEdit as block numbers relative to that block's own Run text, so they're converted to
    # absolute file line numbers via the same _line_count_before_run used for LSP cursor mapping,
    # then handed to the terminal as `break file:line` pdb commands issued before `continue`.
    # [FN] _debug_current_file — launches the active Python file under pdb with its breakpoints set
    # [FN OPEN] _debug_current_file
    def _debug_current_file(self):
        tab = self.active_tab
        if tab is None:
            return
        if Path(tab.path).suffix.lower() != '.py':
            self._ide_message('Debug', 'Il debug e disponibile solo per file Python in questa versione.')
            return
        if tab.dirty:
            if not tab.save():
                return
            self._update_tab_title(tab)
        lines = []
        for edit in self.active_page.findChildren(CodeEdit):
            item = getattr(edit, 'kant_item', None)
            if not edit.breakpoints or item is None:
                continue
            offset = self._line_count_before_run(tab.tree, item) or 0
            lines.extend(offset + number + 1 for number in edit.breakpoints)
        # same fix as _run_current_file: pdb's output lands in the shell TerminalPane regardless
        # of which terminal-dock tab is currently showing, so force it into view here too — this
        # is very likely why debug "seemed to not work": F5 fired and pdb genuinely started, just
        # out of sight on the REPL/Errori tab.
        self._switch_terminal_tab(0)
        self.terminal.run_debug_python(tab.path, lines, os.path.dirname(tab.path) or None, self._active_python())
    # [FN CLOSED] _debug_current_file

    # [FN CATEGORY] _apply_skeleton_to_tab — runs the deterministic skeleton pass on a tab's current
    # content and, if it added anything, replaces the tab's tree with the marked-up one as an
    # ordinary undoable edit — the exact same in-place-update sequence _ai_fill_kant_blanks and
    # _deterministic_tag_current_file both need, factored out once so they can't drift apart.
    # [FN] _apply_skeleton_to_tab — inserts a deterministic KANT skeleton into a tab; None if no-op
    # [FN OPEN] _apply_skeleton_to_tab
    def _apply_skeleton_to_tab(self, tab):
        text = serialize_kant(tab.tree)
        result = skeleton.apply_skeleton(text, tab.path, self.project_root_path)
        if result is None:
            return None
        new_text, _inserted = result
        try:
            new_tree = parse_kant(new_text)
        except KantParseError as e:
            self._ide_message('KANT', f'Impossibile inserire lo scheletro dei marker: {e}')
            return None
        tab.remember_undo_state()
        tab.tree = new_tree
        tab.mark_dirty()
        self._render_view(tab, tab.filter_uid)
        self._update_tab_title(tab)
        return new_text
    # [FN CLOSED] _apply_skeleton_to_tab

    # [FN] _kant_node_span — (tag, name, start_line, closed_line) for a uid in tab's CURRENT tree,
    # or None; a leaf's own span works exactly the same way a parent's does, no special-casing.
    # start_line is the earliest of category_line/tagline_line/open_line, not open_line alone —
    # audit_kant_headers reports a blank/missing CATEGORY or tagline at ITS OWN line, which sits
    # one or two lines ABOVE open_line, so anchoring the scope on open_line alone would silently
    # exclude exactly the warnings this scoping exists to catch.
    # [FN OPEN] _kant_node_span
    def _kant_node_span(self, tab, uid):
        if uid is None:
            return None
        node = self._find_node_by_uid(tab.tree, uid)
        if node is None or node.open_line is None or node.closed_line is None:
            return None
        start_line = min(l for l in (node.category_line, node.tagline_line, node.open_line) if l is not None)
        return node.tag, node.name, start_line, node.closed_line
    # [FN CLOSED] _kant_node_span

    # [FN CATEGORY] _deterministic_tag_current_file — the File-view-mode counterpart to
    # _ai_fill_kant_blanks: no AI involved at all, just the deterministic skeleton pass on whatever
    # file is open in the coding board. This is what the action-toolbar's sparkle-slot button runs
    # while the left tree is in File mode (an untagged/lightly-tagged file, where "ask the AI to
    # fill blanks" doesn't make sense yet — there's no structure for it to find blanks in).
    # [FN] _deterministic_tag_current_file — tags the open file deterministically, no AI
    # [FN OPEN] _deterministic_tag_current_file
    def _deterministic_tag_current_file(self):
        tab = self.active_tab
        if tab is None:
            return
        if self._apply_skeleton_to_tab(tab) is None:
            self._ide_message(
                'KANT', 'Nessun elemento da taggare in questo file (già completo, o linguaggio non supportato).',
            )
            return
        self._ide_message('KANT', 'Struttura KANT generata deterministicamente per questo file.')
    # [FN CLOSED] _deterministic_tag_current_file

    # [FN CATEGORY] _comment_kant_project — project-wide KANT comment action: flushes open edits,
    # scans every supported UTF-8 source recursively without the search-size cap, inserts all
    # mechanically knowable structure first, then asks the selected AI only for blank prose.
    # [FN] _comment_kant_project — deterministically tags and comments the complete project
    # [FN OPEN] _comment_kant_project
    def _comment_kant_project(self):
        if not self.project_root_path or not self._flush_all_tabs():
            return
        changed, skipped = skeleton.apply_skeleton_to_project(self.project_root_path)
        for rel, _count in changed:
            tab = self._tab_for_path(os.path.join(self.project_root_path, rel))
            if tab is not None:
                self._reload_tab_from_disk(tab)
        self._refresh_after_fs_change()
        effort = self.claude_pane.effort_select.currentText().strip()
        if effort == MODEL_DEFAULT:
            effort = None
        launched = self._launch_kant_fill_blanks(None, effort=effort)
        if launched is None:
            summary = 'Tutti i sorgenti supportati sono già completi: nessun commento KANT da generare.'
            if skipped:
                summary += '\nFile con marker non validi o non scrivibili: ' + ', '.join(skipped)
            self._ide_message('AI KANT Comment (intero progetto)', summary)
        elif not launched:
            self._ide_message('AI KANT Comment (intero progetto)', 'Impossibile avviare il processo AI in questo momento.')
        elif skipped:
            self.statusBar().showMessage(
                f'Commento progetto avviato; {len(skipped)} file non validi/non scrivibili esclusi', 7000,
            )
    # [FN CLOSED] _comment_kant_project

    def _remove_all_kant_comments(self):
        if not self.project_root_path:
            return
        if not self._ide_yes_no(
            'Rimuovi tutti i commenti KANT',
            'Questo elimina CATEGORY, righe descrittive, OPEN/CLOSED e i vecchi INCOMING/OUTGOING '
            'da tutti i file del progetto. Il codice e i commenti normali restano invariati. Continuare?',
            danger=True,
        ):
            return
        if not self._flush_all_tabs():
            return
        changed, skipped = skeleton.strip_kant_project(self.project_root_path)
        for rel in changed:
            tab = self._tab_for_path(os.path.join(self.project_root_path, rel))
            if tab is not None:
                self._reload_tab_from_disk(tab)
        self._refresh_after_fs_change()
        summary = f'Commenti KANT rimossi da {len(changed)} file.'
        if skipped:
            summary += f'\n{len(skipped)} file non scrivibili: ' + ', '.join(skipped)
        self._ide_message('Rimuovi tutti i commenti KANT', summary)

    # [FN CATEGORY] _wipe_and_retag_project — strips every KANT marker (including hand-written
    # CATEGORY/tagline text — that's genuinely discarded, not preserved) from the whole project,
    # then re-tags every file from scratch as if it had never been marked at all. Destructive
    # enough to need its own explicit, danger-styled confirmation rather than the plain yes/no
    # every other project-level action here uses. Any currently open tab for an affected file is
    # reloaded from disk afterward — the wipe writes straight to disk, so an open tab's still-in-
    # memory pre-wipe tree would otherwise silently overwrite the fresh result on its next save.
    # [FN] _wipe_and_retag_project — removes and deterministically regenerates the whole project's
    # KANT structure
    # [FN OPEN] _wipe_and_retag_project
    def _wipe_and_retag_project(self):
        if not self.project_root_path:
            return
        if not self._ide_yes_no(
            'Rimuovi e rigenera struttura KANT',
            'Questo rimuove OGNI marker KANT (comprese le descrizioni CATEGORY/riga breve scritte '
            'a mano) da tutti i file del progetto e ricrea la struttura da zero in modo '
            'deterministico (tag/nesting/#id). Le descrizioni andranno riscritte da capo. '
            "Non può essere annullata con Ctrl+Z. Continuare?",
            danger=True,
        ):
            return
        changed, skipped = skeleton.wipe_and_reskeleton_project(self.project_root_path)
        for rel, _count in changed:
            tab = self._tab_for_path(os.path.join(self.project_root_path, rel))
            if tab is not None:
                self._reload_tab_from_disk(tab)
        self._refresh_after_fs_change()
        total = sum(count for _rel, count in changed)
        summary = f'{len(changed)} file rigenerati, {total} elementi taggati.'
        if skipped:
            summary += f'\n{len(skipped)} file saltati (marker non validi): ' + ', '.join(skipped)
        self._ide_message('Rimuovi e rigenera struttura KANT', summary)
    # [FN CLOSED] _wipe_and_retag_project

    # [FN CATEGORY] _ai_fill_kant_blanks — the deterministic/AI split for KANT comments, applied to
    # exactly what's currently isolated in the coding board — a single leaf, a parent and its
    # descendants, or (uid is None) the whole file. Tag, name, nesting, OPEN/CLOSED placement, and
    # #id are never left to the AI to decide — kant/skeleton.py works those out from the code's own
    # structure and inserts them first, as an ordinary undoable edit. audit_kant_headers then lists
    # every element whose CATEGORY/tagline is still blank, filtered to the visible node's own line
    # span when one is isolated (the uid is captured BEFORE the skeleton insertion, since that can
    # shift line numbers — the span is always re-resolved from the tab's post-insertion tree, never
    # reused from before it). Refuses to proceed if the file's existing markers don't even parse.
    # [FN] _ai_fill_kant_blanks — inserts a deterministic KANT skeleton, then asks the AI to fill
    # the visible scope, or the whole file when explicitly requested by the KANT menu
    # [FN OPEN] _ai_fill_kant_blanks
    def _ai_fill_kant_blanks(self, whole_file=False):
        tab = self.active_tab
        if tab is None:
            return
        scope_uid = None if whole_file else self._active_filter_uid()
        new_text = self._apply_skeleton_to_tab(tab)
        text = new_text if new_text is not None else serialize_kant(tab.tree)
        scope = self._kant_node_span(tab, scope_uid)
        # an element IS isolated in the coding board (scope_uid set) but its span couldn't be
        # resolved — surfacing this as an error instead of silently falling back to the whole
        # file, which would run the AI over far more than what's actually shown
        if scope_uid is not None and scope is None:
            self._ide_message(
                'KANT', "Impossibile individuare l'elemento isolato nella plancia di coding — riprova dopo un salvataggio.",
            )
            return

        audit = audit_kant_headers(text)
        if audit['errors']:
            self._ide_message(
                'KANT', 'Questo file ha marker KANT non validi: esegui prima "Verifica KANT" dal menu File.',
            )
            return
        blanks = [
            w for w in audit['warnings']
            if w['message'] in ('CATEGORY mancante', 'CATEGORY vuota', 'tagline mancante', 'tagline vuota')
        ]
        if scope is not None:
            _tag, _name, open_line, closed_line = scope
            blanks = [w for w in blanks if open_line <= w['line'] <= closed_line]
        if not blanks:
            where = f' in [{scope[0]}] {scope[1]}' if scope is not None else ''
            self._ide_message('KANT', f'Nessun campo KANT vuoto da compilare{where}.')
            return

        listing = '\n'.join(f'- riga {b["line"]}: [{b["tag"]}] {b["name"]} — {b["message"]}' for b in blanks)
        if scope is not None:
            intro = (
                f'Nel file {tab.path}, dentro [{scope[0]}] {scope[1]} (righe {scope[2]}-{scope[3]}), '
                'questi elementi KANT hanno CATEGORY e/o la riga descrittiva ancora vuoti:'
            )
        else:
            intro = f'Nel file {tab.path} questi elementi KANT hanno CATEGORY e/o la riga descrittiva ancora vuoti:'
        md_path = _write_kant_fill_markdown(intro, listing)
        prompt = (
            f'Leggi {md_path}: elenca gli elementi KANT con descrizione mancante e le istruzioni '
            'per compilarla. Applica esattamente quelle istruzioni.'
        )
        effort = self.claude_pane.effort_select.currentText().strip()
        if effort == MODEL_DEFAULT:
            effort = None
        self.claude_pane.run_prompt(prompt, effort=effort)
    # [FN CLOSED] _ai_fill_kant_blanks

    # [FN] _project_kant_blanks — every element with a blank CATEGORY/tagline across the whole
    # project (same audit_kant_headers check _ai_fill_kant_blanks uses per file), plus any file
    # whose existing markers don't parse — the caller decides whether/how to surface those
    # [FN OPEN] _project_kant_blanks
    def _project_kant_blanks(self, root):
        lines, broken = [], []
        for path, text in iter_project_text_files(root, max_bytes=None):
            rel = os.path.relpath(path, root)
            audit = audit_kant_headers(text)
            if audit['errors']:
                broken.append(rel)
                continue
            for w in audit['warnings']:
                if w['message'] in ('CATEGORY mancante', 'CATEGORY vuota', 'tagline mancante', 'tagline vuota'):
                    lines.append(f'- {rel}:{w["line"]}: [{w["tag"]}] {w["name"]} — {w["message"]}')
        return lines, broken
    # [FN CLOSED] _project_kant_blanks

    # [FN CATEGORY] _launch_kant_fill_blanks — the project-wide sibling of _ai_fill_kant_blanks,
    # used right after a fresh deterministic skeleton pass on project open: the skeleton tool has
    # already decided every tag/name/nesting/#id, so this asks the AI for the one thing left —
    # filling in the descriptions — instead of the old /kant-code-map prompt that asked it to
    # figure out structure too.
    # [FN] _launch_kant_fill_blanks — asks Claude/Codex to fill in every blank KANT description
    # [FN OPEN] _launch_kant_fill_blanks
    def _launch_kant_fill_blanks(self, agent, model=None, effort=None):
        if model:
            self.claude_pane.set_agent(agent)
            self.claude_pane.model_select.setCurrentText(model)
        lines, broken = self._project_kant_blanks(self.project_root_path)
        if not lines:
            return None
        listing = '\n'.join(lines)
        if broken:
            listing += (
                '\n\nFile esclusi perché hanno marker KANT non validi (non modificarli):\n- '
                + '\n- '.join(broken)
            )
        md_path = _write_kant_fill_markdown(
            'In questo progetto questi elementi KANT hanno CATEGORY e/o la riga descrittiva ancora vuoti:',
            listing,
        )
        prompt = (
            f'Leggi {md_path}: elenca gli elementi KANT con descrizione mancante in questo progetto '
            'e le istruzioni per compilarla. Applica esattamente quelle istruzioni.'
        )
        return self.claude_pane.run_prompt(prompt, agent=agent, auto_permissions_once=True, effort=effort)
    # [FN CLOSED] _launch_kant_fill_blanks

    # [FN CATEGORY] _vim_dispatch — the single callback every CodeEdit's vim engine (kant/widgets.py)
    # routes structural/cross-widget actions through: moving to an adjacent element, jumping to the
    # file's first/last element, folding, search, undo/redo, and the : command bar. Kept as one
    # dispatcher (not one callback per action) so CodeEdit only needs a single attribute for
    # everything vim needs from the rest of the app.
    # [FN] _vim_dispatch — CodeEdit.vim_action callback
    # [FN OPEN] _vim_dispatch
    def _vim_dispatch(self, edit, name, **_kwargs):
        if name == 'next_element':
            self._vim_move_to_element(edit, 1)
        elif name == 'prev_element':
            self._vim_move_to_element(edit, -1)
        elif name == 'first_element':
            self._vim_move_to_edge(True)
        elif name == 'last_element':
            self._vim_move_to_edge(False)
        elif name == 'toggle_fold':
            self._vim_toggle_fold(edit)
        elif name == 'search':
            self._show_find_bar()
        elif name == 'find_next':
            self._find_next()
        elif name == 'find_prev':
            self._find_prev()
        elif name == 'undo':
            self._undo_file()
        elif name == 'redo':
            self._redo_file()
        elif name == 'open_command_bar':
            self._show_vim_command_bar()
    # [FN CLOSED] _vim_dispatch

    # [FN] _vim_move_to_element — j/k at a block boundary: focuses the next/previous CodeEdit in
    # the active page (same findChildren(CodeEdit) scope _debug_current_file already uses for
    # breakpoints), cursor landing at its start (moving down) or end (moving up) — matching the
    # feel of a continuous buffer where element boundaries are just more lines.
    # [FN OPEN] _vim_move_to_element
    def _vim_move_to_element(self, edit, direction):
        page = self.active_page
        if page is None:
            return
        edits = page.findChildren(CodeEdit)
        if edit not in edits:
            return
        target_index = edits.index(edit) + direction
        if not 0 <= target_index < len(edits):
            return
        target = edits[target_index]
        cursor = target.textCursor()
        cursor.movePosition(QTextCursor.Start if direction > 0 else QTextCursor.End)
        target.setTextCursor(cursor)
        target.setFocus()
    # [FN CLOSED] _vim_move_to_element

    def _vim_move_to_edge(self, first):
        page = self.active_page
        if page is None:
            return
        edits = page.findChildren(CodeEdit)
        if not edits:
            return
        target = edits[0] if first else edits[-1]
        cursor = target.textCursor()
        cursor.movePosition(QTextCursor.Start if first else QTextCursor.End)
        target.setTextCursor(cursor)
        target.setFocus()

    # [FN] _vim_toggle_fold — za: walks up from the edit to its enclosing CollapsibleSection (a
    # LeafSection, the other section widget, has no fold state to toggle) and clicks its existing
    # fold button, reusing the same toggle path a mouse click already goes through.
    # [FN OPEN] _vim_toggle_fold
    def _vim_toggle_fold(self, edit):
        widget = edit.parent()
        while widget is not None and not isinstance(widget, CollapsibleSection):
            widget = widget.parent()
        if widget is not None and widget.toggle_btn is not None:
            widget.toggle_btn.click()
    # [FN CLOSED] _vim_toggle_fold

    # ---- section view (AI-NAV: Node/Run tree -> editable Qt widgets) -----

    def _render_view(self, tab, only_uid=None):
        tab.filter_uid = only_uid
        self._update_tab_title(tab)
        self._update_filename_label()
        if hasattr(self, 'claude_pane'):
            self.claude_pane.refresh_focus_label()
        while tab.view_layout.count():
            item = tab.view_layout.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()
        tab.section_widgets.clear()
        tab.collapsibles.clear()
        if only_uid is None:
            self._build_node_widgets(tab, tab.tree, tab.view_layout, 0)
            self._ensure_empty_file_is_editable(tab)
            self._add_add_element_block(tab)
        else:
            node = self._find_node_by_uid(tab.tree, only_uid)
            if node is None:
                self._build_node_widgets(tab, tab.tree, tab.view_layout, 0)
                self._ensure_empty_file_is_editable(tab)
                self._add_add_element_block(tab)
            else:
                wrapper = Node(tag='ROOT', name='', open_raw=None, body=[node])
                self._build_node_widgets(tab, wrapper, tab.view_layout, 0)
        if getattr(tab, '_ai_review_lines', None) is not None:
            self._apply_ai_review_editor_markers(tab)

    # [FN CATEGORY] _enter_ai_review_mode — replaces the old separate review window: every non-
    # binary changed file gets opened and re-rendered as ONE merged, read-only block showing both
    # kept and deleted lines together (green-underlined additions, red-underlined+struck-through
    # deletions), while the project tree colors each affected file's own row the same way (see
    # _ai_review_label_style) — a live diff you scroll through in place, not a separate window.
    # Called from workspace._finish_ai_review right after build_ai_review succeeds; a deleted file
    # has nothing left on disk to open, so it only gets the tree's strikethrough treatment.
    # [FN] _enter_ai_review_mode — opens every changed file in its merged diff view, colors the tree
    # [FN OPEN] _enter_ai_review_mode
    def _enter_ai_review_mode(self, review):
        self._ai_review_status = {}
        for item in review:
            rel = item['path'].replace(os.sep, '/')
            kind = 'created' if item['status'] == 'creato' else (
                'deleted' if item['status'] == 'eliminato' else 'modified'
            )
            self._ai_review_status[rel] = {
                'kind': kind, 'additions': item.get('additions', 0), 'deletions': item.get('deletions', 0),
            }
            if item['binary'] or kind == 'deleted':
                continue
            target = os.path.join(self.project_root_path, item['path'])
            if not self._open_file(target):
                continue
            tab = self.open_tabs.get(target)
            if tab is not None:
                # _open_file reuses one "preview" tab slot for whatever was opened most recently
                # (VS Code-style) — opening several review files back to back would otherwise evict
                # each one's tab the moment the next opens. _pin_file_tab is the real mechanism (it
                # also detaches the tab from _preview_file_tab so _release_preview has nothing left
                # to evict) — just setting tab._pinned doesn't do that on its own.
                self._pin_file_tab(tab)
                self._show_ai_review_diff(tab, item)
        self._rebuild_tree()

    def _exit_ai_review_mode(self):
        self._ai_review_status = {}
    # [FN CLOSED] _enter_ai_review_mode

    # [FN] _show_ai_review_diff — swaps one tab's content for its merged old+new diff view
    # [FN OPEN] _show_ai_review_diff
    def _show_ai_review_diff(self, tab, item):
        old_lines = [line.rstrip('\n') for line in item.get('old_lines', [])]
        new_lines = [line.rstrip('\n') for line in item.get('new_lines', [])]
        merged, added, deleted = [], set(), set()
        for tag, i1, i2, j1, j2 in item['opcodes']:
            if tag == 'equal':
                merged.extend(old_lines[i1:i2])
                continue
            if tag in ('delete', 'replace'):
                start = len(merged)
                merged.extend(old_lines[i1:i2])
                deleted.update(range(start, len(merged)))
            if tag in ('insert', 'replace'):
                start = len(merged)
                merged.extend(new_lines[j1:j2])
                added.update(range(start, len(merged)))
        tab.tree = Node(tag='ROOT', name='', open_raw=None, body=[Run(lines=merged)])
        tab._ai_review_lines = (added, deleted)
        self._render_view(tab, None)
    # [FN CLOSED] _show_ai_review_diff

    # [FN] _apply_ai_review_editor_markers — green/red underline extra-selections over the merged
    # view's added/deleted lines, deleted lines also struck through; the whole block goes read-only
    # for the duration of the review — "disattiva dal codice vivo, lascia solo in visualizzazione"
    # [FN OPEN] _apply_ai_review_editor_markers
    def _apply_ai_review_editor_markers(self, tab):
        added, deleted = getattr(tab, '_ai_review_lines', (set(), set()))
        # the merged diff tree is one Run under ROOT, so _build_node_widgets puts exactly one
        # CodeEdit straight into tab.view_layout — read it from there, not from
        # tab.view_container.findChildren(CodeEdit): the PREVIOUS render's widgets were just
        # takeAt()'d out of the layout by _render_view's cleanup but are only deleteLater()'d, not
        # yet actually gone, so findChildren would still see them until Qt processes the deferred
        # delete and could grab a stale widget instead of the new one.
        if not tab.view_layout.count():
            return
        edit = tab.view_layout.itemAt(0).widget()
        if not isinstance(edit, CodeEdit):
            return
        edit.setReadOnly(True)
        doc = edit.document()
        selections = []
        for line_no in sorted(added | deleted):
            block = doc.findBlockByNumber(line_no)
            if not block.isValid():
                continue
            color = QColor(theme.DANGER if line_no in deleted else theme.OK)
            fmt = QTextCharFormat()
            fmt.setUnderlineStyle(QTextCharFormat.SingleUnderline)
            fmt.setUnderlineColor(color)
            background = QColor(color)
            background.setAlpha(28)
            fmt.setBackground(background)
            if line_no in deleted:
                fmt.setFontStrikeOut(True)
            fmt.setProperty(QTextFormat.FullWidthSelection, True)
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.LineUnderCursor)
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format = fmt
            selections.append(selection)
        edit.setExtraSelections(selections)
    # [FN CLOSED] _apply_ai_review_editor_markers

    def _ensure_empty_file_is_editable(self, tab):
        if tab.view_container.findChildren(CodeEdit):
            return
        run = next((item for item in tab.tree.body if isinstance(item, Run)), None)
        if run is None:
            run = Run(lines=[''])
            tab.tree.body.append(run)
        edit = CodeEdit('\n'.join(run.lines), self._run_line_offsets(tab.tree).get(id(run), 0))
        edit.kant_item = run
        edit.kant_tab = tab
        edit.textChanged.connect(lambda e=edit, it=run, t=tab: self._on_code_changed(t, e, it))
        edit.completion_provider = self._request_completion
        edit.hover_provider = self._request_hover
        edit.definition_provider = lambda _edit: self._lsp_command('definition')
        edit.rename_provider = lambda _edit: self._lsp_command('rename')
        edit.vim_action = self._vim_dispatch
        edit.import_edit_provider = self._start_import_edit
        tab.view_layout.addWidget(edit)

    # [FN CATEGORY] _add_add_element_block — the "+" card at the bottom of the whole-file KANT
    # outline (scroll all the way down to find it). Deliberately a QPushButton, not a hand-rolled
    # clickable QFrame — free keyboard focus/Enter-activation and hover state instead of
    # reimplementing them.
    # [FN] _add_add_element_block — appends the "add a new element" card to a file's outline
    # [FN OPEN] _add_add_element_block
    def _add_add_element_block(self, tab):
        # a plain top margin used to be the only thing separating this from the last element's own
        # widget — when that element was a leaf (no border/background of its own around it, e.g. a
        # bare FN/CST at the top level), the card read as if it belonged to that leaf instead of to
        # the file as a whole. A full-width divider line makes the file-level scope unambiguous
        # regardless of what precedes it.
        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet(f'background:{theme.BORDER}; max-height:1px; border:none; margin-top:10px; margin-bottom:4px;')
        tab.view_layout.addWidget(divider)
        block = QPushButton('+  Aggiungi un elemento')
        block.setCursor(Qt.PointingHandCursor)
        block.setToolTip('Crea un nuovo modulo, classe, funzione o altro elemento in questo file')
        block.setFixedHeight(36)
        block.setStyleSheet(
            f'QPushButton {{ background:{theme.CODE_BG}; color:{theme.DIM}; border:1px dashed {theme.DIM}; '
            f'border-radius:{theme.RADIUS}px; font-size:{theme.CODING_FONT_PT}pt; font-weight:600; }} '
            f'QPushButton:hover {{ color:{theme.ACCENT}; border-color:{theme.ACCENT}; background:{theme.PANEL}; }}'
        )
        block.clicked.connect(lambda: self._prompt_add_element(tab))
        tab.view_layout.addWidget(block)
    # [FN CLOSED] _add_add_element_block

    def _default_element_language(self, tab):
        ext = os.path.splitext(tab.path)[1].lower()
        for language, info in ELEMENT_LANGUAGES.items():
            if info['ext'] == ext:
                return language
        return 'Python'

    # [FN CATEGORY] _prompt_add_element — opens _ide_new_element_form, and on confirmation appends
    # the resulting Node straight to the tab's own tree — the same in-memory model every edit
    # already mutates, so save/undo/xref all pick it up exactly like a hand-typed element would,
    # nothing new to wire for those.
    # [FN] _prompt_add_element — the "+" card's click handler
    # [FN OPEN] _prompt_add_element
    def _prompt_add_element(self, tab):
        result = self._ide_new_element_form(default_tag='FN', default_language=self._default_element_language(tab))
        if result is None:
            return
        tag, name, desc, language = result
        tab.remember_undo_state()
        node = build_new_element_node(tag, name, desc, language)
        # a file already wrapped in exactly one top-level MOD/CFG element gets the new element
        # appended INSIDE that wrapper's own body, not as a second sibling at tab.tree's root —
        # appending to the root placed it structurally outside the module (after its own [[TAG]
        # CLOSED] line), which the coding board rendered anyway (it walks tab.tree.body flatly,
        # no notion of "the" file wrapper) but which _build_project_tree's left-tree builder never
        # sees, since it only ever walks the first top-level node's own children. Multiple existing
        # top-level nodes (a flat file with no single wrapper) or zero (nothing tagged yet) both
        # keep the previous root-level append, unambiguous either way.
        top_level_nodes = [item for item in tab.tree.body if isinstance(item, Node)]
        if len(top_level_nodes) == 1:
            top_level_nodes[0].body.append(node)
        else:
            tab.tree.body.append(node)
        tab.mark_dirty()
        # save now instead of waiting on the 2s autosave timer — the left "Codice" tree only
        # reflects what's on disk (_rebuild_tree re-reads/re-parses files, it never looks at
        # in-memory tab state), so without this the new element wouldn't show up there for however
        # long autosave takes to catch up, even though the coding board already shows it. tab.save()
        # does arm _on_tab_saved's 400ms debounced _refresh_after_fs_change on its own, but that's
        # an async race this doesn't need to take: rebuild the tree synchronously right here instead
        # of waiting on it, so the new element is guaranteed visible the instant this returns.
        tab.save()
        self._invalidate_xref()
        self._rebuild_tree()
        self._render_view(tab, tab.filter_uid)
        self._update_tab_title(tab)
        new_widget = tab.section_widgets.get(node.uid)
        if new_widget is not None:
            QTimer.singleShot(0, lambda: tab.scroll_area.ensureWidgetVisible(new_widget, 50, 80))
            first_edit = new_widget.findChild(CodeEdit)
            if first_edit is not None:
                first_edit.setFocus()
    # [FN CLOSED] _prompt_add_element

    # [FN CATEGORY] _prompt_add_grouping — the "+ Nuovo gruppo" button's handler: offers every
    # element currently in the project's cross-reference graph (_get_xref, the same data the
    # Incoming/Outgoing panel already builds) as candidates, then saves the chosen subset under the
    # given name via kant/groupings.py. Switches to "Gruppi" view so the new group is immediately
    # visible instead of landing in whichever view mode was active before.
    # [FN] _prompt_add_grouping — the "+ Nuovo gruppo" button's click handler
    # [FN OPEN] _prompt_add_grouping
    def _prompt_add_grouping(self, preselected_key=None):
        if not self.project_root_path:
            self._ide_message('Nuovo gruppo', 'Apri prima una cartella di progetto.')
            return
        xref = self._get_xref()
        elements = sorted(
            ((key, el.tag, el.desc or el.name, el.file) for key, el in xref.items()),
            key=lambda row: (row[3], row[2]),
        )
        preselected = (preselected_key,) if preselected_key else ()
        result = self._ide_new_grouping_form(elements, preselected=preselected)
        if result is None:
            return
        name, member_keys = result
        if not name:
            return
        grouping = new_grouping(name)
        grouping.members = member_keys
        grouping.member_hints = {key: member_hint(xref.get(key)) for key in member_keys if key in xref}
        groupings = load_reconciled_groupings(self.project_root_path, xref)
        groupings.append(grouping)
        save_groupings(self.project_root_path, groupings)
        if self.view_mode != 'groups':
            self.groups_view_btn.setChecked(True)
            self._set_view_mode('groups')
        else:
            self._rebuild_tree()
    # [FN CLOSED] _prompt_add_grouping

    # [FN CATEGORY] _xref_key_for_tree_item — resolves a 'file' or 'section' KANT-tree row to its
    # xref key (kant/xref.py's '<rel_path>::<uid>' format), the same lookup _on_tree_item_clicked
    # already does for navigation — reused here so the right-click "add to group" action targets a
    # real xref element instead of inventing a second identifier scheme just for groupings.
    # [FN] _xref_key_for_tree_item — the xref key for a 'file'/'section' tree item, or None
    # [FN OPEN] _xref_key_for_tree_item
    def _xref_key_for_tree_item(self, item):
        if not self.project_root_path or item is None:
            return None
        kind = item.data(0, ROLE_KIND)
        path = item.data(0, ROLE_PATH)
        if kind not in ('file', 'section') or not path:
            return None
        rel = os.path.relpath(path, self.project_root_path).replace(os.sep, '/')
        xref = self._get_xref()
        uid = item.data(0, ROLE_UID)
        key = f'{rel}::{uid}'
        if key in xref:
            return key
        # a legacy (no #id) file mints a fresh uid on every reparse, the same mismatch
        # _on_tree_item_clicked already works around via document order — a 'file' row is always
        # document order 0 (the file's own top-level element, per _build_project_tree's comment),
        # a 'section' row carries its own ROLE_ORDER (children start at 1)
        order = 0 if kind == 'file' else item.data(0, ROLE_ORDER)
        if order is None:
            return None
        candidates = sorted((el for el in xref.values() if el.file == rel), key=lambda el: el.order)
        return candidates[order].key if order < len(candidates) else None
    # [FN CLOSED] _xref_key_for_tree_item

    def _find_node_by_uid(self, node, uid):
        for item in node.body:
            if not isinstance(item, Node):
                continue
            if item.uid == uid:
                return item
            found = self._find_node_by_uid(item, uid)
            if found is not None:
                return found
        return None

    # [FN CATEGORY] _find_node_containing_line — the innermost element whose marker span
    # ([open_line, closed_line], from parse_kant) contains a given source line number. Used to
    # resolve a validation error's raw line number (which counts every line in the file, markers
    # included) to the actual KANT element it belongs to, since _goto_line's block-counting walk
    # only sees each CodeEdit's own Run body — it has no idea the CATEGORY/tagline/OPEN/CLOSED lines
    # between elements exist, so it drifts off the real position for anything past the first element.
    # [FN] _find_node_containing_line — innermost Node whose open/closed span contains `line`
    # [FN OPEN] _find_node_containing_line
    def _find_node_containing_line(self, node, line):
        for item in node.body:
            if not isinstance(item, Node) or item.open_line is None or item.closed_line is None:
                continue
            if item.open_line <= line <= item.closed_line:
                return self._find_node_containing_line(item, line) or item
        return None
    # [FN CLOSED] _find_node_containing_line

    def _build_node_widgets(self, tab, node, layout, depth, line_offsets=None):
        if line_offsets is None:
            line_offsets = self._run_line_offsets(tab.tree)
        i = 0
        while i < len(node.body):
            item = node.body[i]
            if isinstance(item, Run):
                text = '\n'.join(item.lines)
                if not text.strip():
                    i += 1
                    continue
                edit = CodeEdit(text, line_offsets.get(id(item), 0))
                edit.kant_item = item
                edit.kant_tab = tab
                edit.kant_node = node if node.tag != 'ROOT' else None
                edit.textChanged.connect(lambda e=edit, it=item, t=tab: self._on_code_changed(t, e, it))
                edit.completion_provider = self._request_completion
                edit.hover_provider = self._request_hover
                edit.definition_provider = lambda _edit: self._lsp_command('definition')
                edit.rename_provider = lambda _edit: self._lsp_command('rename')
                edit.vim_action = self._vim_dispatch
                layout.addWidget(edit)
                i += 1
            else:
                if self._is_coordinated_leaf(item):
                    group, next_i = self._collect_coordinated_leaf_group(node.body, i)
                    if len(group) > 1:
                        self._render_coordinated_leaf_group(tab, group, layout, line_offsets)
                        i = next_i
                        continue
                has_children = any(isinstance(c, Node) for c in item.body)
                # depth 0 is always the outermost element of an isolated/whole-file view — its
                # identity is already shown in the tab label and title bar, so its own
                # "[TAG] name" title here would be pure redundancy
                if has_children:
                    section = CollapsibleSection(item, show_header=depth > 0)
                    section.editMetadata.connect(lambda node, t=tab: self._edit_kant_metadata(t, node))
                    section.set_expanded(depth < 1)
                    layout.addWidget(section)
                    tab.collapsibles.append(section)
                    tab.section_widgets[item.uid] = section
                    self._build_node_widgets(tab, item, section.content_layout, depth + 1, line_offsets)
                else:
                    leaf = LeafSection(item, show_header=depth > 0)
                    leaf.editMetadata.connect(lambda node, t=tab: self._edit_kant_metadata(t, node))
                    layout.addWidget(leaf)
                    tab.section_widgets[item.uid] = leaf
                    self._build_node_widgets(tab, item, leaf.content_layout, depth + 1, line_offsets)
                i += 1

    def _is_coordinated_leaf(self, item):
        return (
            isinstance(item, Node)
            and item.tag in theme.COORDINATED_LEAF_TAGS
            and not any(isinstance(c, Node) for c in item.body)
        )

    def _collect_coordinated_leaf_group(self, body, start):
        group = [body[start]]
        i = start + 1
        while i < len(body):
            j = i
            while j < len(body) and isinstance(body[j], Run) and not '\n'.join(body[j].lines).strip():
                j += 1
            if j < len(body) and self._is_coordinated_leaf(body[j]):
                group.append(body[j])
                i = j + 1
                continue
            break
        return group, i if len(group) > 1 else start + 1

    def _render_coordinated_leaf_group(self, tab, nodes, layout, line_offsets):
        panel = QWidget()
        panel.setObjectName('coordinatedConstants')
        # same flat-block language as CollapsibleSection (thin top separator + tag-colored left
        # gutter, no card fill/radius) — one continuous gutter spans the whole cluster, reading as
        # "these leaves are one coordinated group" without a bordered box around them
        gutter = theme.TAG_COLORS.get(nodes[0].tag, theme.BORDER) if nodes else theme.BORDER
        panel.setStyleSheet(
            f'#coordinatedConstants {{ background:transparent; border:none; '
            f'border-top:1px solid {theme.BORDER_WEAK}; border-left:3px solid {gutter}; padding:0; }}'
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(theme.SPACE_1, 3, 5, 3)
        panel_layout.setSpacing(1)
        for node in nodes:
            leaf = LeafSection(node, compact=True)
            panel_layout.addWidget(leaf)
            tab.section_widgets[node.uid] = leaf
            self._build_node_widgets(tab, node, leaf.content_layout, 0, line_offsets)
        layout.addWidget(panel)

    def _on_code_changed(self, tab, edit, item):
        tab.remember_undo_state(coalesce=True)
        item.lines = edit.toPlainText().split('\n')
        self._refresh_code_line_offsets(tab)
        tab.mark_dirty()

    def _rewrite_marker_line(self, raw, marker, payload):
        if not raw:
            return raw
        start = raw.find('[')
        if start == -1:
            return raw
        suffix = ''
        stripped = raw.rstrip()
        if stripped.endswith('*/'):
            suffix = ' */'
        elif stripped.endswith('-->'):
            suffix = ' -->'
        return f'{raw[:start]}{marker} {payload}{suffix}'

    def _edit_kant_metadata(self, tab, node):
        result = self._ide_metadata_form(node.tag, node.name, node.desc or node.name)
        if result is None:
            return
        tag, name, desc = result
        tag = tag.strip().upper()
        name = name.strip()
        if not tag or not name:
            return
        desc = desc.strip() or name
        tab.remember_undo_state()
        node.tag = tag
        node.name = name
        node.desc = desc
        if node.tag_raw:
            node.tag_raw = self._rewrite_marker_line(node.tag_raw, f'[{tag}]', f'{name} — {desc}')
        else:
            node.tag_raw = f'# [{tag}] {name} — {desc}'
        node.open_raw = self._rewrite_marker_line(node.open_raw, f'[{tag} OPEN #{node.uid}]', name)
        node.closed_raw = self._rewrite_marker_line(node.closed_raw, f'[{tag} CLOSED #{node.uid}]', name)
        if node.incoming_raw:
            node.incoming_raw = self._rewrite_marker_line(node.incoming_raw, f'[{tag} INCOMING]', f'{name} — {node.incoming or ""}')
        if node.outgoing_raw:
            node.outgoing_raw = self._rewrite_marker_line(node.outgoing_raw, f'[{tag} OUTGOING]', f'{name} — {node.outgoing or ""}')
        tab.mark_dirty()
        tab.save()  # same reasoning as _prompt_add_element — the left tree reads disk, not tab.tree
        self._render_view(tab, tab.filter_uid)
        self._update_tab_title(tab)

    def _reveal_section(self, tab, uid):
        widget = tab.section_widgets.get(uid)
        if widget is None:
            return
        p = widget.parent()
        while p is not None:
            if isinstance(p, CollapsibleSection):
                p.set_expanded(True)
            p = p.parent()
        # expanding ancestors invalidates layout geometry; defer the scroll until Qt has relaid out
        QTimer.singleShot(0, lambda: tab.scroll_area.ensureWidgetVisible(widget, 50, 80))

    # ---- workspace mutations (AI-NAV: UI delegates safety to WorkspaceMixin)

    # [FN] _reveal_in_explorer — opens Windows Explorer with the given path selected/highlighted
    # [FN OPEN] _reveal_in_explorer
    def _reveal_in_explorer(self, path):
        if not path or not os.path.exists(path):
            return
        # '/select,' (no space before the comma) is explorer.exe's own documented switch to open
        # the CONTAINING folder with this item highlighted, for both files and directories —
        # matches how "Reveal in File Explorer" reads in other Windows dev tools
        subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
    # [FN CLOSED] _reveal_in_explorer

    def _dir_for_context(self, item, kind):
        if item is None:
            return self.project_root_path
        if kind == 'dir':
            return item.data(0, ROLE_PATH)
        if kind in ('file', 'plainfile', 'invalidfile', 'section'):
            return os.path.dirname(item.data(0, ROLE_PATH))
        return self.project_root_path

    # [FN CATEGORY] _show_tree_context_menu — right-click menu on the project tree: new file/folder
    # always offered (targeting the right-clicked folder, its containing folder if a file, or the
    # project root on empty space); files/folders can be renamed or trashed, while nested KANT
    # elements can be removed from their source (with descendants) through their own explicit action
    # [FN] _show_tree_context_menu — builds and shows the tree's right-click menu
    # [FN OPEN] _show_tree_context_menu
    def _show_tree_context_menu(self, pos):
        if not self.project_root_path:
            return
        item = self.tree.itemAt(pos)
        kind = item.data(0, ROLE_KIND) if item else None
        target_dir = self._dir_for_context(item, kind)

        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        new_file_action = menu.addAction('Nuovo file…')
        new_file_action.setToolTip('Crea un nuovo file vuoto nella cartella scelta')
        new_folder_action = menu.addAction('Nuova cartella…')
        new_folder_action.setToolTip('Crea una nuova sottocartella vuota')
        restore_action = menu.addAction('Ripristina dal cestino...') if self._restore_candidates() else None
        if restore_action is not None:
            restore_action.setToolTip("Ripristina un file/cartella eliminato di recente dal cestino dell'IDE")
        rename_action = delete_action = delete_kant_action = None
        git_diff_action = git_stage_action = git_unstage_action = None
        run_test_action = reveal_action = None
        test_name = None
        if item is not None and kind in ('file', 'plainfile', 'invalidfile', 'dir'):
            menu.addSeparator()
            rename_action = menu.addAction('Rinomina…')
            rename_action.setToolTip('Rinomina questo file o cartella')
            delete_action = menu.addAction('Elimina')
            delete_action.setToolTip('Sposta questo file o cartella nel cestino')
            reveal_action = menu.addAction('Visualizza in Esplora risorse')
            reveal_action.setToolTip('Apre Esplora risorse con questo file o cartella selezionato')
        elif item is not None and kind == 'section':
            menu.addSeparator()
            delete_kant_action = menu.addAction('Elimina elemento KANT')
            delete_kant_action.setToolTip('Elimina questo elemento KANT e tutto il codice contenuto')
        if item is not None and kind in ('file', 'plainfile') and self.git_root:
            menu.addSeparator()
            git_diff_action = menu.addAction('Git diff')
            git_diff_action.setToolTip('Mostra le differenze non salvate di questo file rispetto a Git')
            git_stage_action = menu.addAction('Git stage')
            git_stage_action.setToolTip('Aggiunge questo file alla staging area (git add)')
            git_unstage_action = menu.addAction('Git unstage')
            git_unstage_action.setToolTip('Rimuove questo file dalla staging area (git reset)')
        if item is not None and kind == 'section' and Path(item.data(0, ROLE_PATH)).suffix.lower() == '.py':
            test_name = self._section_test_name(item)
            if test_name:
                menu.addSeparator()
                run_test_action = menu.addAction(f'Esegui questo test ({test_name})')
                run_test_action.setToolTip(f'Esegue solo pytest {test_name}, non l\'intera suite')

        # KANT-element-only actions: 'file'/'section' rows are actual tagged elements (resolvable
        # in the xref graph), unlike a 'plainfile'/'dir' row — groupings in particular only ever
        # bundle real KANT elements, on request, so the "add to group" action simply never appears
        # on anything else. If no group exists yet, the single action creates the first one with
        # this element already checked in _ide_new_grouping_form, instead of a dead end that would
        # send the user hunting for "+ Nuovo gruppo" themselves right after they asked to add one.
        element_key = self._xref_key_for_tree_item(item) if item is not None else None
        copy_name_action = copy_path_action = add_to_new_group_action = None
        # (action, grouping) pairs captured while the submenu is still fresh — checked against
        # `chosen` below instead of re-querying the submenu itself after exec() returns, since a
        # transient addMenu() popup is not guaranteed to still be alive once its parent menu closes
        group_actions = []
        if element_key is not None:
            menu.addSeparator()
            copy_name_action = menu.addAction('Copia nome elemento')
            copy_path_action = menu.addAction('Copia percorso file')
            menu.addSeparator()
            groupings = load_reconciled_groupings(self.project_root_path, self._get_xref())
            if groupings:
                add_to_group_menu = menu.addMenu('Aggiungi a un gruppo')
                add_to_group_menu.setToolTipsVisible(True)
                for grouping in groupings:
                    already_in = element_key in grouping.members
                    group_action = add_to_group_menu.addAction(grouping.name)
                    group_action.setEnabled(not already_in)
                    group_action.setToolTip('Già in questo gruppo' if already_in else f'Aggiungi a "{grouping.name}"')
                    group_actions.append((group_action, grouping))
            else:
                add_to_new_group_action = menu.addAction('Aggiungi a un nuovo gruppo…')
                add_to_new_group_action.setToolTip('Crea il primo gruppo del progetto con questo elemento già incluso')

        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is new_file_action:
            self._create_new_file(target_dir)
        elif chosen is new_folder_action:
            self._create_new_folder(target_dir)
        elif restore_action is not None and chosen is restore_action:
            self._restore_from_trash()
        elif rename_action is not None and chosen is rename_action:
            self._rename_tree_item(item, kind)
        elif delete_action is not None and chosen is delete_action:
            self._delete_tree_item(item, kind)
        elif delete_kant_action is not None and chosen is delete_kant_action:
            self._delete_kant_element(item)
        elif reveal_action is not None and chosen is reveal_action:
            self._reveal_in_explorer(item.data(0, ROLE_PATH))
        elif git_diff_action is not None and chosen is git_diff_action:
            self._git_diff_file(item.data(0, ROLE_PATH))
        elif git_stage_action is not None and chosen is git_stage_action:
            self._git_stage_file(item.data(0, ROLE_PATH), staged=True)
        elif git_unstage_action is not None and chosen is git_unstage_action:
            self._git_stage_file(item.data(0, ROLE_PATH), staged=False)
        elif run_test_action is not None and chosen is run_test_action:
            self._run_single_test(item.data(0, ROLE_PATH), test_name)
        elif copy_name_action is not None and chosen is copy_name_action:
            el = self._get_xref().get(element_key)
            QApplication.clipboard().setText(el.desc or el.name if el is not None else '')
        elif copy_path_action is not None and chosen is copy_path_action:
            QApplication.clipboard().setText(item.data(0, ROLE_PATH))
        elif add_to_new_group_action is not None and chosen is add_to_new_group_action:
            self._prompt_add_grouping(preselected_key=element_key)
        else:
            matched_group = next((g for a, g in group_actions if a is chosen), None)
            if matched_group is not None:
                add_member(
                    self.project_root_path, matched_group.id, element_key,
                    member_hint(self._get_xref().get(element_key)),
                )
                if self.view_mode == 'groups':
                    self._rebuild_tree()
    # [FN CLOSED] _show_tree_context_menu

    # [FN CATEGORY] _delete_kant_element — removes one nested node from the shared in-memory file
    # model, so the existing atomic save and file-level undo cover the operation exactly like an edit
    # [FN] _delete_kant_element — confirms and deletes a KANT subtree selected in the project tree
    # [FN OPEN] _delete_kant_element
    def _delete_kant_element(self, item):
        if item is None or item.data(0, ROLE_KIND) != 'section':
            return
        path = item.data(0, ROLE_PATH)
        order = item.data(0, ROLE_ORDER)
        if not path or not self._open_file(path):
            return
        tab = self.open_tabs.get(path)
        if tab is None:
            return

        # Old files without persisted #ids are reparsed when opened, so retain the same stable
        # document-order fallback used by navigation and the context-menu test runner.
        node = self._find_node_by_uid(tab.tree, item.data(0, ROLE_UID))
        if node is None and order is not None:
            nodes = self._nodes_in_order(tab.tree)
            if order < len(nodes):
                node = nodes[order]
        if node is None:
            self._ide_message(
                'Elimina elemento KANT',
                self._tr("L'elemento non e piu presente nel file."),
            )
            return

        label = node.desc or node.name or f'[{node.tag}]'
        prompt = self._tr(
            'Eliminare "{name}" e tutto il codice contenuto, inclusi gli elementi figli?\n\n'
            'Puoi annullare con Ctrl+Z.'
        ).format(name=label)
        if not self._ide_yes_no('Elimina elemento KANT', prompt, danger=True):
            return

        deleted_uids = {node.uid} | {child.uid for child in _walk_nodes(node)}
        before = serialize_kant(tab.tree)
        was_dirty = tab.dirty
        tab.remember_undo_state()

        def remove_from(parent):
            for index, child in enumerate(parent.body):
                if child is node:
                    del parent.body[index]
                    return True
                if isinstance(child, Node) and remove_from(child):
                    return True
            return False

        if not remove_from(tab.tree):
            return
        tab.mark_dirty()
        if not tab.save():
            # Atomic saving leaves the disk untouched on failure; restore the matching live model too.
            tab.tree = parse_kant(before)
            tab.dirty = was_dirty
            if was_dirty:
                tab.autosave_timer.start(2000)
            else:
                tab.autosave_timer.stop()
            tab.dirtyChanged.emit()
            self._render_view(tab, tab.filter_uid)
            self._ide_message(
                'Elimina elemento KANT',
                self._tr("Impossibile salvare la modifica: l'elemento non e stato eliminato."),
            )
            return

        active_page = self.active_page
        deleted_pages = [
            page for (page_path, uid), page in list(self._element_pages.items())
            if page_path == path and uid in deleted_uids
        ]
        deleted_preview_was_active = active_page in deleted_pages and active_page is self._preview_page
        for page in deleted_pages:
            self._close_element_tab(page, cleanup_backing=False)

        if tab.filter_uid in deleted_uids:
            tab.filter_uid = None
        self._render_view(tab, tab.filter_uid)
        for page in list(self._element_pages.values()):
            if page._file_tab is tab:
                self._render_element_page(page)
                self._update_element_tab_title(page)

        # A normal unpinned element preview hides its backing file. If that is the element just
        # removed, surface the now-updated whole file in the same preview slot.
        if deleted_preview_was_active:
            index = self.tabs.indexOf(tab)
            if index != -1:
                self.tabs.setTabVisible(index, True)
                self.tabs.setCurrentWidget(tab)
                self._set_preview_file_tab(tab)

        self._invalidate_xref()
        self._rebuild_tree(refresh_git=False)
        if self.active_tab is tab:
            self._update_io_tabs(self._active_filter_uid())
    # [FN CLOSED] _delete_kant_element

    # [FN CATEGORY] _section_test_name — resolves a tree item's KANT node the same way
    # _on_tree_item_clicked does (uid first, document-order fallback for legacy no-#id files) and
    # returns its name only when it looks like a pytest test (name starts with 'test_') — this is
    # the one check that decides whether "Esegui questo test" shows up in the context menu at all.
    # [FN] _section_test_name — the pytest test name for a 'section' tree item, or None
    # [FN OPEN] _section_test_name
    def _section_test_name(self, item):
        path = item.data(0, ROLE_PATH)
        tab = self.open_tabs.get(path)
        tree = tab.tree if tab is not None else None
        if tree is None:
            try:
                with open(path, encoding='utf-8') as f:
                    tree = parse_kant(f.read())
            except (OSError, KantParseError):
                return None
        uid = item.data(0, ROLE_UID)
        node = self._find_node_by_uid(tree, uid)
        if node is None:
            order = item.data(0, ROLE_ORDER)
            if order is None:
                return None
            nodes = self._nodes_in_order(tree)
            if order >= len(nodes):
                return None
            node = nodes[order]
        return node.name if node.name.startswith('test_') else None
    # [FN CLOSED] _section_test_name

    # [FN CATEGORY] _run_single_test — targets one test with pytest's `path::name` node-id syntax
    # instead of the whole-project run _run_tests does; reuses _finish_test_run's own output/summary
    # parsing since a one-test run produces the exact same FAILED-line / summary-line shape.
    # [FN] _run_single_test — runs one pytest test by name and surfaces pass/fail
    # [FN OPEN] _run_single_test
    def _run_single_test(self, path, test_name):
        project_root = self.project_root_path or self.git_root
        if not project_root or self._test_run_pending:
            return
        if not self._flush_all_tabs():
            return
        self._test_run_pending = True
        python_exe = self._active_python()
        node_id = f'{path}::{test_name}'

        def run():
            return subprocess.run(
                [python_exe, '-m', 'pytest', '--tb=short', '-q', '--color=no', node_id],
                cwd=project_root, capture_output=True, text=True, timeout=120,
            )

        self.terminal.write_info(f'\n# Esegui test: python -m pytest {test_name}\n')
        self._run_background(run, lambda result, error: self._finish_test_run(project_root, result, error))
    # [FN CLOSED] _run_single_test

    # [FN CATEGORY] _run_tests — runs the whole project through `python -m pytest` (auto-discovers
    # test_*.py, matching this project's own test_kant_smoke.py convention) via _run_background so
    # the UI doesn't block, then parses FAILED lines out of the captured output into results_view —
    # the same clickable-results tree _show_search_results already uses — instead of a bespoke panel.
    # [FN] _run_tests — runs pytest and surfaces pass/fail without an external terminal
    # [FN OPEN] _run_tests
    def _run_tests(self):
        project_root = self.project_root_path or self.git_root
        if not project_root or self._test_run_pending:
            return
        if not self._flush_all_tabs():
            return
        self._test_run_pending = True
        python_exe = self._active_python()

        def run():
            return subprocess.run(
                [python_exe, '-m', 'pytest', '--tb=short', '-q', '--color=no'],
                cwd=project_root, capture_output=True, text=True, timeout=300,
            )

        self.terminal.write_info('\n# Esegui test: python -m pytest --tb=short -q\n')
        self._run_background(run, lambda result, error: self._finish_test_run(project_root, result, error))
    # [FN CLOSED] _run_tests

    def _finish_test_run(self, project_root, result, error):
        self._test_run_pending = False
        if error or result is None:
            message = str(error) if error else 'Impossibile avviare pytest (installato?)'
            self.terminal.write_info(f'\n# Test: errore\n{message}\n')
            return
        output = (result.stdout or '') + (result.stderr or '')
        self.terminal.write_info(f'\n{output}\n')
        # the final summary line ("1 failed, 1 passed in 0.74s") is printed bare, not wrapped in the
        # "=== ... ===" banners pytest uses for FAILURES / short test summary info above it
        summary_match = re.search(
            r'^[^\r\n]*(?:failed|passed|error|skipped|xfailed|xpassed)[^\r\n]*\bin [\d.]+s[^\r\n]*$',
            output, re.MULTILINE | re.IGNORECASE,
        )
        summary = summary_match.group(0).strip('= ') if summary_match else f'exit code {result.returncode}'
        failures = []
        for match in re.finditer(r'^FAILED (\S+)', output, re.MULTILINE):
            rel_path, _, rest = match.group(1).partition('::')
            test_name = rest.rsplit('::', 1)[-1].split('[', 1)[0] if rest else ''
            failures.append((os.path.join(project_root, rel_path), rel_path, test_name))
        self._show_test_results(summary, failures)

    def _show_test_results(self, summary, failures):
        self.results_view.clear()
        root = QTreeWidgetItem(self.results_view, [f'Test: {summary}'])
        for path, rel, test_name in failures:
            label = f'{rel}::{test_name}' if test_name else rel
            item = QTreeWidgetItem(root, [label])
            item.setData(0, ROLE_KIND, 'test-result')
            item.setData(0, ROLE_PATH, path)
            item.setData(0, ROLE_TEXT, f'def {test_name}' if test_name else '')
        root.setExpanded(True)
        self._toggle_info_popup(self.results_view, force_open=True)
# [FN CLOSED] MainWindow
