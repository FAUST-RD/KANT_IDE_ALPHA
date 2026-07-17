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
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QPoint, QPointF, Qt, QSettings, QSize, Signal, QTimer
from PySide6.QtGui import (
    QColor, QFont, QKeySequence, QMouseEvent, QShortcut, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QPushButton, QScrollArea,
    QSizeGrip, QSplitter, QStackedWidget, QTabBar, QTabWidget, QToolButton,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from kant import theme
from kant.theme import set_theme
from kant.icons import draw_icon
from kant.model import (
    Run, Node, parse_kant, serialize_kant, read_top_level_label, read_top_level_label_result,
    KantParseError, ELEMENT_LANGUAGES, build_new_element_node, build_new_file_content,
)
from kant.fileio import file_fingerprint, write_file_atomic, detect_line_ending, is_safe_child_name
from kant.syntax import check_file_syntax, run_command_for_path, _quote_arg
from kant.xref import build_xref, _walk_nodes
from kant.groupings import load_groupings, save_groupings, new_grouping, add_member
from kant.lsp import LSP_SERVERS_BY_EXT, file_uri, path_from_file_uri, lsp_server_for_path, LspClient
from kant.dialogs import IdeDialogsMixin
from kant.gitops import GitOpsMixin
from kant.projectops import (
    build_kant_map, definition_locations, has_any_kant_tags, iter_kant_tagged_files,
    reference_locations, scan_project_replace, search_project, validate_kant_project,
)
from kant.pyenv import (
    dependency_file, detect_venvs, has_module, interpreter_label, interpreter_version,
    is_python_majority_project, load_interpreter, save_interpreter,
)
from kant.workspace import ROLE_PATH, WorkspaceMixin, discard_snapshot, rollback_snapshot
from kant.widgets import (
    CodeEdit, TerminalPane, ClaudePane, CollapsibleSection, LeafSection,
    ProjectTree, make_app_icon, make_app_pixmap, RecentFolderCard, TitleBar, FileTab,
    MODEL_DEFAULT, CLAUDE_MODELS, CODEX_MODELS, _tag_header_html, _markdown_to_html,
    set_vim_mode, vim_mode_enabled, show_code_hover_popup, hide_code_hover_popup,
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


# [FN CATEGORY] _TreeItemLabel — tree rows use setItemWidget with a rich-HTML QLabel instead of plain
# item text (for colored tag badges); WA_TransparentForMouseEvents alone was unreliable for getting
# clicks through to the QTreeWidget's own itemClicked/itemDoubleClicked, so this label forwards
# clicks to its own item directly instead of depending on hit-test pass-through.
# [FN] _TreeItemLabel — a tree-row label that forwards its own clicks to the owning QTreeWidgetItem
# [FN OPEN] _TreeItemLabel
class _TreeItemLabel(QLabel):
    def __init__(self, tree, item, html):
        super().__init__(html)
        self._tree = tree
        self._item = item

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._tree.setCurrentItem(self._item)
            self._tree.itemClicked.emit(self._item, 0)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._tree.itemDoubleClicked.emit(self._item, 0)
        event.accept()
# [FN CLOSED] _TreeItemLabel


# [FN CATEGORY] _TabLabel — QTabBar.setTabButton() lets a rich-HTML QLabel replace a tab's plain
# text, giving the tab strip the same colored/bold "[TAG] name" convention already used in the
# tree and the coding panel. It selects its own tab on click, then forwards the same press (and
# any move/release) on to the QTabBar itself — the label now covers the tab's whole visible area,
# so without forwarding, QTabBar's own mouse handling never sees the press and its built-in
# drag-to-reorder (tabs are setMovable(True)) stops working from the labeled region entirely.
# [FN] _TabLabel — a tab-strip label that forwards its own clicks to select its tab
# [FN OPEN] _TabLabel
class _TabLabel(QLabel):
    def __init__(self, tabs, tab):
        super().__init__()
        self.setTextFormat(Qt.RichText)
        self._tabs = tabs
        self._tab = tab

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._tabs.setCurrentWidget(self._tab)
        self._forward_to_tab_bar(event)

    def mouseMoveEvent(self, event):
        self._forward_to_tab_bar(event)

    def mouseReleaseEvent(self, event):
        self._forward_to_tab_bar(event)

    def _forward_to_tab_bar(self, event):
        bar = self._tabs.tabBar()
        pos = self.mapTo(bar, event.position().toPoint())
        forwarded = QMouseEvent(
            event.type(), QPointF(pos), event.globalPosition(),
            event.button(), event.buttons(), event.modifiers(),
        )
        QApplication.sendEvent(bar, forwarded)
        event.accept()
# [FN CLOSED] _TabLabel


# [FN CATEGORY] _KantTabBar — QTabBar's own tabSizeHint only accounts for a tab's text/icon, not a
# button widget set via setTabButton — with the plain text cleared in favor of _TabLabel, tabs
# were sizing themselves as if empty and clipping the rich label down to a sliver. This widens the
# hint to fit the label's own sizeHint plus room for the close button.
# [FN] _KantTabBar — a QTabBar that sizes tabs to fit their LeftSide button widget
# [FN OPEN] _KantTabBar
class _KantTabBar(QTabBar):
    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        label = self.tabButton(index, QTabBar.LeftSide)
        if label is not None:
            needed = label.sizeHint().width() + 40  # + close button and padding
            size.setWidth(max(size.width(), needed))
        return size
# [FN CLOSED] _KantTabBar


# [FN CATEGORY] MainWindow — wires the project tree, the section view and the toolbar together;
# owns the currently-open file's parsed tree and dirty state
# [FN] MainWindow — the KANT Editor application window
# [FN OPEN] MainWindow
class MainWindow(IdeDialogsMixin, WorkspaceMixin, GitOpsMixin, QMainWindow):
    backgroundFinished = Signal(object, object, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('KANT Editor')
        self.setWindowIcon(make_app_icon())
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.resize(1500, 950)
        self.setFont(QFont('Consolas', 10))

        self.open_tabs = {}  # path -> FileTab, every currently open file
        self._ai_context_page = None  # last coding tab selected by the user
        self.settings = QSettings('KANT', 'KANT Editor')  # persists the dragged column width
        self.night_mode = self.settings.value('nightMode', False, type=bool)
        set_theme(self.night_mode)
        self.project_root_path = None
        self.git_root = None
        self.git_status = {}
        self.view_mode = 'code'  # left project tree: 'code' = KANT-labeled, 'file' = plain filenames
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
        self._map_sync_generation = 0
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
        self.lsp_client.serverError.connect(
            lambda message: self.terminal.write_info(f'\n# LSP: {message}\n') if hasattr(self, 'terminal') else None
        )
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
        # the titlebar owns these widgets (they sit to the right of the "KANT IDE" wordmark now);
        # keep short aliases so the rest of MainWindow doesn't need to know that
        self.filename_label = self.title_bar.filename_label
        self.syntax_label = self.title_bar.syntax_label
        # its own row, not squeezed into the title bar — that row's essential window chrome
        # (minimize/maximize/close) was getting crowded out once these were added there
        shell_layout.addWidget(self._build_action_toolbar())

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

        self.setStyleSheet(theme.APP_STYLE)
        saved_geometry = self.settings.value('windowGeometry')
        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)
        self._setup_shortcuts()
        self._check_crash_recovery()
        self._check_pending_ai_snapshot()

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
        return self._ide_yes_no('Chiudi KANT IDE', 'Sei sicuro di voler chiudere KANT IDE?', accent=True)

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
        self._background.shutdown(wait=False, cancel_futures=True)
        for proc in (self.terminal.process, self.claude_pane.process):
            if proc is not None:
                proc.kill()
                proc.waitForFinished(1000)
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
        self._position_map_tab()
        self._position_claude_tab()
        self._position_map_dialog()

    def _position_claude_tab(self):
        if not hasattr(self, 'claude_tab_btn'):
            return
        # sits at the AI pane's own inner-left edge (splitter.widget(0)'s width), not a fixed
        # shell coordinate — collapsed, that edge coincides with the shell's right edge (same spot
        # as before); expanded, it tracks wherever the splitter divider actually is instead of
        # floating past it, out over the pane's own content
        x = self.splitter.widget(0).width() - self.claude_tab_btn.width()
        self.claude_tab_btn.move(x, (self.shell.height() - self.claude_tab_btn.height()) // 2)
        self.claude_tab_btn.raise_()

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
    # [FN] _style_map_tab_button — rounds whichever corners face away from the edge it's stuck to
    # [FN OPEN] _style_map_tab_button
    def _style_map_tab_button(self, at_top):
        if at_top:
            self.map_tab_btn.setStyleSheet(
                f'QPushButton {{ background:{theme.PANEL}; color:{theme.TEXT}; '
                f'border:1px solid {theme.BORDER}; border-top:none; '
                f'border-bottom-left-radius:8px; border-bottom-right-radius:8px; font-weight:700; }} '
                f'QPushButton:hover {{ color:{theme.ACCENT}; border-color:{theme.ACCENT}; }}'
            )
        else:
            self.map_tab_btn.setStyleSheet(
                f'QPushButton {{ background:{theme.PANEL}; color:{theme.TEXT}; '
                f'border:1px solid {theme.BORDER}; border-bottom:none; '
                f'border-top-left-radius:8px; border-top-right-radius:8px; font-weight:700; }} '
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

    # [FN CATEGORY] _tree_stylesheet — the ONE place the KANT tree's own QSS is built, called from
    # both initial construction and _apply_theme's refresh pass. Those two used to duplicate this
    # string independently and had drifted apart (padding:6px 4px boxed-look vs. a later padding:
    # 14px 10px flat-look; a hardcoded #eef4ff selection color vs. a night-aware one) — real dead
    # space and a wrong-color selection highlight that only showed up after a theme toggle. One
    # shared builder means the two call sites can't diverge like that again.
    # [FN] _tree_stylesheet — boxed KANT tree QSS with a theme-aware selection color
    # [FN OPEN] _tree_stylesheet
    def _tree_stylesheet(self):
        selected_bg = '#1e293b' if self.night_mode else '#fdf3d8'
        return (
            f'QTreeWidget {{ background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:8px; padding:6px 4px; }} '
            f'QTreeWidget::item {{ padding:0px; }} '
            f'QTreeWidget::item:selected {{ background:{selected_bg}; color:{theme.ACCENT}; border-radius:4px; }}'
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
            self.terminal.setStyleSheet(
                f'background:{theme.CODE_BG}; color:{theme.TEXT}; border-top:1px solid {theme.BORDER}; padding:12px;'
            )
            self.claude_pane.apply_style()
            self._style_io_tabs()
            self._style_view_mode_bar()
            self._style_action_toolbar()
            self._style_find_bar()
            self._style_status_bar()
            self._update_kant_map_label()
            for tab in self.open_tabs.values():
                tab.apply_style()
                self._render_view(tab, tab.filter_uid)
            if hasattr(self.active_page, '_element_key'):
                self._render_element_page(self.active_page)
            if self.active_tab is not None:
                self._update_io_tabs(self._active_filter_uid())
        self._refresh_recent_folders()

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
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(4)
        self.action_toolbar_buttons = {}
        for key, tooltip, callback in (
            ('save', 'Salva (Ctrl+S)', self._save_file),
            ('undo', 'Annulla file (Ctrl+Z)', self._undo_file),
            ('redo', 'Ripeti file (Ctrl+Y)', self._redo_file),
            ('find', 'Trova nel file (Ctrl+F)', self._show_find_bar),
        ):
            btn = QToolButton()
            btn.setIcon(draw_icon(key, 18))
            btn.setIconSize(QSize(18, 18))
            btn.setToolTip(tooltip)
            btn.setFixedSize(32, 28)
            btn.clicked.connect(callback)
            layout.addWidget(btn)
            self.action_toolbar_buttons[key] = btn
        layout.addStretch(1)
        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setFixedHeight(22)
        self._action_toolbar_separator = separator
        layout.addWidget(separator)
        # Run/Debug visibly bigger than the rest, at the trailing edge of this same row (moved from
        # the leading edge on request — same toolbar, opposite side)
        for key, tooltip, callback in (
            ('run', 'Esegui (Ctrl+R)', self._run_current_file),
            ('debug', 'Debug (F5)', self._debug_current_file),
        ):
            btn = QToolButton()
            btn.setIcon(draw_icon(key, 26))
            btn.setIconSize(QSize(26, 26))
            btn.setToolTip(tooltip)
            btn.setFixedSize(40, 36)
            btn.clicked.connect(callback)
            layout.addWidget(btn)
            self.action_toolbar_buttons[key] = btn
        self.action_toolbar = bar
        self._style_action_toolbar()
        return bar
    # [FN CLOSED] _build_action_toolbar

    def _style_action_toolbar(self):
        self.action_toolbar.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        style = theme.BUTTON_STYLE.replace('QPushButton', 'QToolButton').replace('padding:7px 13px;', 'padding:4px;')
        for btn in self.action_toolbar_buttons.values():
            btn.setStyleSheet(style)
        self._action_toolbar_separator.setStyleSheet(f'color:{theme.BORDER};')

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
        badge.setPixmap(make_app_pixmap(76))
        badge.setFixedSize(76, 76)
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
        self._style_welcome_page()
        self._refresh_recent_folders()
        return page
    # [FN CLOSED] _build_welcome_page

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
            f'#welcomeCard {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; border-radius:16px; }}'
        )
        self.welcome_open_btn.setStyleSheet(
            f'QPushButton {{ background:{theme.ACCENT}; color:#ffffff; border:none; '
            f'border-radius:9px; padding:14px 28px; }} '
            f'QPushButton:hover {{ background:{theme.ACCENT}; }}'
        )
        self.welcome_new_project_btn.setStyleSheet(
            f'QPushButton {{ background:{theme.CODE_BG}; color:{theme.ACCENT}; '
            f'border:2px dashed {theme.BORDER}; border-radius:9px; padding:12px 26px; }} '
            f'QPushButton:hover {{ border-color:{theme.ACCENT}; background:{theme.PANEL}; }}'
        )
    # [FN CLOSED] _style_welcome_page

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
        self.setAcceptDrops(True)

        tree_panel = QWidget()
        tree_panel_layout = QVBoxLayout(tree_panel)
        tree_panel_layout.setContentsMargins(6, 6, 6, 6)
        tree_panel_layout.setSpacing(6)
        tree_panel_layout.addWidget(self._build_view_mode_bar())
        tree_panel_layout.addWidget(self.tree, 1)

        add_row_style = (
            f'QPushButton {{ background:{theme.CODE_BG}; color:{theme.DIM}; border:2px dashed {theme.BORDER}; '
            f'border-radius:8px; padding:8px; font-weight:600; }} '
            f'QPushButton:hover {{ color:{theme.ACCENT}; border-color:{theme.ACCENT}; background:{theme.PANEL}; }}'
        )
        self.add_file_btn = QPushButton('+  Nuovo file')
        self.add_file_btn.setCursor(Qt.PointingHandCursor)
        self.add_file_btn.setToolTip('Crea un nuovo file nella cartella del progetto')
        self.add_file_btn.setStyleSheet(add_row_style)
        self.add_file_btn.clicked.connect(self._prompt_add_file)

        # a grouping bundles elements from anywhere in the project (any tag, any file, any parent)
        # under one name, independent of the source tree's own MOD/CLS/FN nesting — see
        # kant/groupings.py for the persistence format and _prompt_add_grouping for the picker
        self.add_grouping_btn = QPushButton('+  Nuovo gruppo')
        self.add_grouping_btn.setCursor(Qt.PointingHandCursor)
        self.add_grouping_btn.setToolTip('Raggruppa elementi da file diversi sotto un nome comune')
        self.add_grouping_btn.setStyleSheet(add_row_style)
        self.add_grouping_btn.clicked.connect(self._prompt_add_grouping)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        add_row.addWidget(self.add_file_btn)
        add_row.addWidget(self.add_grouping_btn)
        tree_panel_layout.addLayout(add_row)

        # MAPPA opens from a small tab stuck to the bottom-center edge of the window (like a
        # drawer handle) rather than a button buried in the tree panel; clicking it again while
        # the map is open closes it back down. Parented directly to the shell (not the stack) so
        # it stays put and clickable regardless of which page/tab is showing underneath.
        self.map_tab_btn = QPushButton(' MAPPA', self.shell)
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
        # the one unpinned "preview" element page (VS Code-style): clicking a new KANT element
        # retargets this same tab slot instead of adding a new one, until the user pins it — see
        # _show_element_tab/_retarget_element_page/_pin_element_page
        self._preview_page = None
        # same VS Code-style preview slot, one level up: the one unpinned whole-FILE tab. A FileTab
        # can't be retargeted in place the way an element page can (its tree/undo stack/dirty state/
        # disk_fingerprint are all tied to one path from construction on), so "reuse" here means
        # close the old preview tab and open the new file in its place, rather than an in-place
        # content swap — see _open_file/_set_preview_file_tab/_pin_file_tab
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

        self.claude_pane = ClaudePane(os.getcwd())
        self.claude_pane.before_run = self._prepare_ai_snapshot
        self.claude_pane.context_hint = self._build_ai_context_hint
        self.claude_pane.focus_hint = self._build_ai_focus_summary
        self.claude_pane.refresh_focus_label()
        self.claude_pane.finished.connect(self._finish_ai_review)
        self.claude_pane.finished.connect(self._refresh_and_validate_after_ai)

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
            lambda *_: self.settings.setValue('workspaceSplitterSizes', self.workspace_splitter.sizes())
        )

        self.main_splitter = QSplitter(Qt.Vertical)
        self.main_splitter.addWidget(self.workspace_splitter)
        self.main_splitter.addWidget(self.terminal_dock)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        saved_main_sizes = self.settings.value('mainVerticalSplitterSizes')
        if saved_main_sizes and len(saved_main_sizes) == 2:
            self.main_splitter.setSizes([int(x) for x in saved_main_sizes])
        else:
            self.main_splitter.setSizes([650, 180])
        self.main_splitter.splitterMoved.connect(
            lambda *_: self.settings.setValue('mainVerticalSplitterSizes', self.main_splitter.sizes())
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
            lambda *_: self.settings.setValue('splitterSizes', self.splitter.sizes())
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
        self.cursor_pos_label = QLabel('')
        self.encoding_label = QLabel('')
        # shows the focused CodeEdit's vim mode (NORMAL/INSERT/VISUAL); empty whenever vim mode is
        # off or no code block has focus — see _on_focus_changed and _update_vim_mode_label
        self.vim_mode_label = QLabel('')
        self.vim_mode_label.setFont(QFont('Consolas', theme.TREE_FONT_PT, QFont.DemiBold))
        bar.addPermanentWidget(self.vim_mode_label)
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
    # [FN CLOSED] _build_status_bar

    def _style_status_bar(self):
        self.statusBar().setStyleSheet(
            f'QStatusBar {{ background:{theme.PANEL}; color:{theme.DIM}; border-top:1px solid {theme.BORDER}; }}'
        )
        self.vim_mode_label.setStyleSheet(f'color:{theme.ACCENT}; padding:0 8px;')
        self.python_env_label.setStyleSheet(
            f'QPushButton {{ border:none; background:transparent; color:{theme.DIM}; padding:0 8px; }} '
            f'QPushButton:hover {{ color:{theme.ACCENT}; }}'
        )

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
        self.cursor_pos_label.setText(f'  Riga {cursor.blockNumber() + 1}, Col {cursor.columnNumber() + 1}  ')

    # [FN CATEGORY] _build_view_mode_bar — two mutually-exclusive toggle buttons controlling how the
    # LEFT project tree is built: "Codice" labels files/sections by their KANT tag (the convention
    # view), "File" is a classic PyCharm-style plain file/folder browser by real name. The center
    # code view always shows the open file's KANT structure regardless of this setting.
    # [FN] _build_view_mode_bar — builds the Codice/File toggle row
    # [FN OPEN] _build_view_mode_bar
    def _build_view_mode_bar(self):
        bar = QWidget()
        self.view_mode_bar = bar
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        def add_label(text):
            lbl = QLabel(text)
            lbl.setFont(QFont('Consolas', theme.CODE_FONT_PT - 2, QFont.DemiBold))
            lbl.setStyleSheet(f'color:{theme.DIM}; padding:0 4px;')
            layout.addWidget(lbl)

        def add_gap():
            layout.addSpacing(18)

        self.code_view_btn = QPushButton('Codice')
        self.code_view_btn.setCheckable(True)
        self.code_view_btn.setChecked(True)
        self.code_view_btn.setToolTip('Mostra il file attivo come sezioni KANT modificabili (vista predefinita)')
        self.code_view_btn.clicked.connect(lambda: self._set_view_mode('code'))

        self.file_view_btn = QPushButton('File')
        self.file_view_btn.setCheckable(True)
        self.file_view_btn.setToolTip('Mostra il file attivo come testo grezzo, senza la struttura a sezioni KANT')
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

        add_label('Vista')
        layout.addWidget(self.code_view_btn)
        layout.addWidget(self.file_view_btn)
        layout.addWidget(self.groups_view_btn)
        layout.addStretch(1)
        self._style_view_mode_bar()
        self._update_action_buttons()
        return bar
    # [FN CLOSED] _build_view_mode_bar

    def _style_view_mode_bar(self):
        self.view_mode_bar.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        checked_style = (
            theme.BUTTON_STYLE + f'QPushButton:checked {{ background:{theme.ACCENT}; color:#ffffff; border-color:{theme.ACCENT}; }}'
        )
        for btn in self.view_mode_bar.findChildren(QPushButton):
            highlight = btn in (self.code_view_btn, self.file_view_btn, self.groups_view_btn)
            btn.setStyleSheet(checked_style if highlight else theme.BUTTON_STYLE)

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
        if not hasattr(page, 'findChildren'):
            return None
        viewport = page.scroll_area.viewport() if isinstance(page, FileTab) else page.viewport()
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
            return 'intero progetto'
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
        self.title_bar.run_menu_action.setEnabled(has_tab)
        self.title_bar.find_menu_action.setEnabled(has_tab)
        self.action_toolbar_buttons['save'].setEnabled(has_tab)
        self.action_toolbar_buttons['undo'].setEnabled(bool(has_tab and self.active_tab.undo_stack))
        self.action_toolbar_buttons['redo'].setEnabled(bool(has_tab and self.active_tab.redo_stack))
        self.action_toolbar_buttons['find'].setEnabled(has_tab)
        self.action_toolbar_buttons['run'].setEnabled(has_tab)
        self.action_toolbar_buttons['debug'].setEnabled(has_tab)
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
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(1)
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
        self.find_input.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        layout.addWidget(self.find_input, 1)

        prev_btn = QPushButton('')
        prev_btn.setIcon(draw_icon('arrow-up', 14))
        prev_btn.setIconSize(QSize(14, 14))
        prev_btn.setFixedWidth(32)
        prev_btn.setToolTip('Occorrenza precedente')
        prev_btn.clicked.connect(self._find_prev)
        layout.addWidget(prev_btn)

        next_btn = QPushButton('')
        next_btn.setIcon(draw_icon('arrow-down', 14))
        next_btn.setIconSize(QSize(14, 14))
        next_btn.setFixedWidth(32)
        next_btn.setToolTip('Occorrenza successiva (Invio)')
        next_btn.clicked.connect(self._find_next)
        layout.addWidget(next_btn)

        self.find_status = QLabel('')
        self.find_status.setStyleSheet(f'color:{theme.DIM};')
        layout.addWidget(self.find_status)

        close_btn = QPushButton('×')
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
            f'border-radius:6px; padding:5px 8px;'
        )
        icon_button_style = theme.BUTTON_STYLE.replace('padding:7px 13px;', 'padding:4px;')
        for btn in self.find_bar.findChildren(QPushButton):
            btn.setStyleSheet(icon_button_style if not btn.text() else theme.BUTTON_STYLE)

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
        prefix.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        layout.addWidget(prefix)

        self.vim_command_input = QLineEdit()
        self.vim_command_input.setPlaceholderText('w, q, wq, x, qa…')
        self.vim_command_input.setFont(QFont('Consolas', theme.CODE_FONT_PT))
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
            f'border-radius:6px; padding:5px 8px;'
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
        remaining = max(1, line)
        for edit in tab.view_container.findChildren(CodeEdit):
            blocks = edit.blockCount()
            if remaining > blocks:
                remaining -= blocks
                continue
            cursor = edit.textCursor()
            cursor.movePosition(QTextCursor.Start)
            for _ in range(remaining - 1):
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
        self._rebuild_tree()
    # [FN CLOSED] _set_view_mode

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
        self.info_popup_close_btn = QPushButton('×')
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
        self.incoming_label_btn.setToolTip("Elenca chi fa riferimento all'elemento selezionato, e da dove")
        self.incoming_label_btn.clicked.connect(lambda: self._toggle_info_popup(self.incoming_view))
        self.outgoing_label_btn = QPushButton('OUTGOING')
        self.outgoing_label_btn.setToolTip("Elenca a cosa fa riferimento l'elemento selezionato, e dove")
        self.outgoing_label_btn.clicked.connect(lambda: self._toggle_info_popup(self.outgoing_view))
        for btn in (self.incoming_label_btn, self.outgoing_label_btn):
            label_layout.addWidget(btn)
        label_layout.addStretch(1)
        # the filename itself lives here, not in the title bar — that slot shows the KANT identity
        # of whatever's isolated instead (see _update_filename_label)
        self.file_path_label = QLabel('')
        self.file_path_label.setStyleSheet(f'color:{theme.DIM}; font-weight:700;')
        self.file_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.file_path_label.setCursor(Qt.IBeamCursor)
        label_layout.addWidget(self.file_path_label)
        layout.addWidget(label_bar)

        panel.setFixedHeight(42)
        return panel

    def _style_io_tabs(self):
        list_style = (
            f'QListWidget {{ background:{theme.CODE_BG}; color:{theme.TEXT}; border:none; padding:4px; '
            f'font-family:Consolas; }} '
            f'QListWidget::item {{ padding:5px 8px; border-radius:5px; }} '
            f'QListWidget::item:hover {{ background:{theme.PANEL}; }} '
            f'QListWidget::item:selected {{ background:{theme.PANEL}; color:{theme.ACCENT}; }}'
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
        self.io_tabs.setStyleSheet(f'background:{theme.PANEL}; border-top:1px solid {theme.BORDER};')
        for btn in (self.incoming_label_btn, self.outgoing_label_btn):
            btn.setStyleSheet(theme.BUTTON_STYLE + 'QPushButton { padding:4px 12px; }')
        self.info_popup_close_btn.setStyleSheet(
            f'QPushButton {{ border:none; background:transparent; color:{theme.DIM}; '
            f'font-size:16px; font-weight:700; padding:0px 4px; }} '
            f'QPushButton:hover {{ color:{theme.TAG_COLORS["TST"]}; }}'
        )
        # map_tab_btn's own corner rounding flips with which edge it's on (bottom of the shell vs
        # top of the map dialog) — handled by _style_map_tab_button, called from _position_map_tab
        # since it already knows which edge, and runs whenever the tab is (re)shown or moved
        self._position_map_tab()
        # same idea, rotated a quarter turn: rounded left corners only, flat right — sticks out of
        # the window's right edge instead of its bottom edge
        self.claude_tab_btn.setStyleSheet(
            f'QPushButton {{ background:{theme.PANEL}; color:{theme.TEXT}; '
            f'border:1px solid {theme.BORDER}; border-right:none; '
            f'border-top-left-radius:8px; border-bottom-left-radius:8px; font-weight:700; }} '
            f'QPushButton:hover {{ color:{theme.ACCENT}; border-color:{theme.ACCENT}; }}'
        )
        self.terminal_sidebar.setStyleSheet(f'background:{theme.PANEL}; border-right:1px solid {theme.BORDER};')
        for btn in self.terminal_sidebar_group.buttons():
            btn.setStyleSheet(
                f'QToolButton {{ border:none; border-radius:4px; background:transparent; }} '
                f'QToolButton:hover {{ background:{theme.CODE_BG}; }} '
                f'QToolButton:checked {{ background:{theme.CODE_BG}; border:1px solid {theme.BORDER}; }}'
            )
        self.errors_view.setStyleSheet(f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:none; padding:6px;')
        if self.map_dialog is not None:
            self.map_dialog.apply_style()

    # [FN CATEGORY] _build_terminal_dock — a narrow left sidebar (3 icon buttons, exclusive like a
    # vertical tab bar) switches a QStackedWidget between the real shell (self.terminal), a second
    # TerminalPane running an interactive Python REPL, and a live list of the active file's
    # diagnostics — so the bottom panel isn't only ever the shell. The Python REPL process starts
    # lazily on first switch to that tab, not at construction, since most sessions never open it.
    # [FN] _build_terminal_dock — sidebar + stacked terminal/REPL/errors panel
    # [FN OPEN] _build_terminal_dock
    def _build_terminal_dock(self):
        self.terminal = TerminalPane(os.getcwd())
        self.python_terminal = TerminalPane(os.getcwd())
        self.errors_view = QTreeWidget()
        self.errors_view.setHeaderHidden(True)
        self.errors_view.itemClicked.connect(self._open_result_item)

        self.terminal_stack = QStackedWidget()
        self.terminal_stack.addWidget(self.terminal)
        self.terminal_stack.addWidget(self.python_terminal)
        self.terminal_stack.addWidget(self.errors_view)

        sidebar = QWidget()
        sidebar.setFixedWidth(36)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(2, 4, 2, 0)
        sidebar_layout.setSpacing(2)
        self.terminal_sidebar_group = QButtonGroup(sidebar)
        self.terminal_sidebar_group.setExclusive(True)
        for index, (icon_name, tooltip) in enumerate((
            ('terminal', 'Terminale'), ('repl', 'Terminale Python'), ('warning', 'Errori nel file aperto'),
        )):
            btn = QToolButton()
            btn.setCheckable(True)
            btn.setIcon(draw_icon(icon_name, 22))
            btn.setIconSize(QSize(22, 22))
            btn.setFixedSize(32, 32)
            btn.setToolTip(tooltip)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, i=index: self._switch_terminal_tab(i))
            self.terminal_sidebar_group.addButton(btn, index)
            sidebar_layout.addWidget(btn)
        sidebar_layout.addStretch(1)
        self.terminal_sidebar_group.button(0).setChecked(True)
        self.terminal_sidebar = sidebar

        dock = QWidget()
        dock_layout = QHBoxLayout(dock)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        dock_layout.setSpacing(0)
        dock_layout.addWidget(sidebar)
        dock_layout.addWidget(self.terminal_stack, 1)
        return dock
    # [FN CLOSED] _build_terminal_dock

    def _switch_terminal_tab(self, index):
        self.terminal_stack.setCurrentIndex(index)
        if index == 1 and self.python_terminal.process is None:
            self.python_terminal.run_python_repl(self._active_python())

    def _toggle_info_popup(self, widget, force_open=False):
        if self.info_popup.currentWidget() is widget and self.info_popup.isVisible() and not force_open:
            self._close_info_popup()
            return
        self.info_popup.setCurrentWidget(widget)
        self.info_popup.setVisible(True)
        self.info_popup_top_bar.setVisible(True)
        self.io_tabs.setFixedHeight(200)

    def _close_info_popup(self):
        self.info_popup.setVisible(False)
        self.info_popup_top_bar.setVisible(False)
        self.io_tabs.setFixedHeight(42)

    # [FN CATEGORY] _open_xref_window — MAPPA opens the cross-reference graph in a dialog internal to
    # the IDE (parented to the main window, floating over the editor — not a strip in the coding pane
    # nor a separate OS window), kept as a single reused instance and raised if already open. Rebuilds
    # the graph from the (cache-backed) xref on every open so it reflects the current code, and wires
    # double-click-a-node back to _navigate_to_element so the map doubles as a jump-to launcher. The
    # close-tab is reparented onto the dialog itself while it's open: the dialog is a separate
    # top-level window that would otherwise render fully over the shell's own copy of the tab,
    # making it unclickable until the map was closed some other way.
    # [FN] _open_xref_window — opens/raises the internal cross-reference map dialog
    # [FN OPEN] _open_xref_window
    def _open_xref_window(self):
        if self.map_dialog is None:
            self.map_dialog = XrefMapDialog(self)
            self.map_dialog.nodeActivated.connect(self._navigate_to_element)
            self.map_dialog.resized.connect(self._position_map_tab)
        self.map_dialog.apply_style()
        project_name = os.path.basename(self.project_root_path) if self.project_root_path else ''
        self.map_dialog.set_graph(self._get_xref(), project_name, self.project_root_path or '')
        self.map_dialog.show()
        self._position_map_dialog()
        self.map_dialog.raise_()
        self.map_dialog.activateWindow()
        self.map_tab_btn.setParent(self.map_dialog)
        self.map_tab_btn.setText(' MAPPA')
        self.map_tab_btn.setIcon(draw_icon('arrow-down', 12))
        self.map_tab_btn.show()
        self._position_map_tab()
    # [FN CLOSED] _open_xref_window

    def _toggle_xref_window(self):
        if self.map_dialog is not None and self.map_dialog.isVisible():
            key = self.map_dialog.selected_key()
            self.map_dialog.hide()
            self.map_tab_btn.setParent(self.shell)
            self.map_tab_btn.setText(' MAPPA')
            self.map_tab_btn.setIcon(draw_icon('arrow-up', 12))
            self.map_tab_btn.show()
            self._position_map_tab()
            if key:
                self._navigate_to_element(key)
            return
        self._open_xref_window()

    # [FN CATEGORY] _toggle_claude_pane — flattens the AI terminal pane to zero width via the outer
    # splitter (not hiding the widget) so its running process/state is untouched, and remembers the
    # width it had so restoring gives back the same size instead of an arbitrary default.
    # [FN] _toggle_claude_pane — collapses/restores the AI terminal pane
    # [FN OPEN] _toggle_claude_pane
    def _toggle_claude_pane(self):
        sizes = self.splitter.sizes()
        if len(sizes) < 2:
            return
        if sizes[1] > 0:
            self._claude_pane_width = sizes[1]
            self.splitter.setSizes([sizes[0] + sizes[1], 0])
            self.claude_tab_btn.setIcon(draw_icon('arrow-left', 12))
        else:
            restore = self._claude_pane_width or 360
            total = sum(sizes)
            self.splitter.setSizes([max(200, total - restore), restore])
            self.claude_tab_btn.setIcon(draw_icon('arrow-right', 12))
        self._position_claude_tab()
    # [FN CLOSED] _toggle_claude_pane

    # [FN CATEGORY] _navigate_to_element — jumps the editor to a cross-reference element by its key
    # ('<rel_path>::<uid>'): opens the file if needed, shows the full file view, then scrolls the
    # section into view. Shared by the map's double-click and the Incoming/Outgoing list rows.
    # [FN] _navigate_to_element — opens and reveals an xref element in the editor
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
        self._render_view(tab)
        self._reveal_section(tab, node.uid)
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
                label = read_top_level_label(file_path)
                if label is not None:
                    rel = os.path.relpath(file_path, project_root).replace(os.sep, '/')
                    trees[rel] = label[2]
            for rel, text in open_texts.items():
                trees[rel] = parse_kant(text)
            return build_xref(trees)

        def apply(graph, error):
            if self._xref_pending_generation == generation:
                self._xref_pending_generation = None
            if error or generation != self._xref_generation or project_root != self.project_root_path:
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
                item = QListWidgetItem(f'{arrow}  [{el.tag}] {el.desc}      ·  {el.file}')
                item.setData(ROLE_KEY, target_key)
                item.setForeground(QColor(theme.TAG_COLORS.get(el.tag, theme.TEXT)))
                item.setToolTip(el.category_desc or 'Doppio clic per aprire')
                view.addItem(item)

        fill(self.incoming_view, incoming, '←')   # ← comes from
        fill(self.outgoing_view, outgoing, '→')   # → goes to
    # [FN CLOSED] _update_io_tabs


    # ---- project tree (AI-NAV: project lifecycle and outline building) ---

    def _recent_folders(self):
        folders = self.settings.value('recentFolders', [])
        if isinstance(folders, str):
            folders = [folders]
        return [p for p in folders if isinstance(p, str) and os.path.isdir(p)]

    def _remember_folder(self, path):
        folders = [p for p in self._recent_folders() if os.path.abspath(p) != os.path.abspath(path)]
        folders.insert(0, path)
        self.settings.setValue('recentFolders', folders[:7])
        self._refresh_recent_folders()

    def _refresh_recent_folders(self):
        if not hasattr(self, 'recent_layout'):
            return
        while self.recent_layout.count():
            item = self.recent_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        for path in self._recent_folders()[:5]:
            card = RecentFolderCard(path)
            card.clicked.connect(self._open_project_folder)
            self.recent_layout.addWidget(card)

    def _open_folder(self):
        path = QFileDialog.getExistingDirectory(self, 'Apri cartella')
        if not path:
            return
        self._open_project_folder(path)

    # [FN CATEGORY] _prompt_new_project — the welcome page's "+" button: creates a brand-new
    # project folder (not just opening an existing one, like _open_folder above) with an optional
    # KANT-tagged starter module (reusing the exact same build_new_file_content machinery the "+"
    # file/element dialogs already use — a new project's first file is language-correct and tagged
    # from line one, not an empty shell) and an optional `git init`, then opens it the same way any
    # existing folder would be.
    # [FN] _prompt_new_project — the welcome page's "+" button click handler
    # [FN OPEN] _prompt_new_project
    def _prompt_new_project(self):
        recent = self._recent_folders()
        default_parent = os.path.dirname(recent[0]) if recent else os.path.expanduser('~')
        result = self._ide_new_project_form(default_parent, default_language='Python')
        if result is None:
            return
        name = result['name']
        if not is_safe_child_name(name):
            self._ide_message('Nuovo progetto', 'Usa solo un nome, senza percorsi.')
            return
        target_dir = os.path.join(result['parent_dir'], name)
        if os.path.exists(target_dir):
            self._ide_message('Nuovo progetto', 'Esiste già una cartella con questo nome.')
            return
        try:
            os.makedirs(target_dir)
            if result['create_starter']:
                ext = ELEMENT_LANGUAGES.get(result['language'], ELEMENT_LANGUAGES['Generico'])['ext']
                content = build_new_file_content('module', result['language'], name)
                write_file_atomic(os.path.join(target_dir, f'main{ext}'), content)
        except OSError as error:
            self._ide_message('Nuovo progetto', f'Impossibile creare il progetto: {error}')
            return
        if result['init_git']:
            git_result = self._run_git(['init'], target_dir)
            if git_result is None or git_result.returncode:
                error = (git_result.stderr or git_result.stdout) if git_result else 'Git non disponibile'
                self._ide_message('Nuovo progetto', f'Progetto creato, ma "git init" non è riuscito:\n{error.strip()[:400]}')
        self._open_project_folder(target_dir)
    # [FN CLOSED] _prompt_new_project

    # [FN CATEGORY] _set_project_chrome_visible — title-bar menus and the action toolbar belong to
    # the project workspace, so the welcome screen keeps only the app identity and window controls.
    # [FN] _set_project_chrome_visible — shows/hides the project title-bar menus and toolbar
    # [FN OPEN] _set_project_chrome_visible
    def _set_project_chrome_visible(self, visible):
        for btn in (
            self.title_bar.file_menu_btn, self.title_bar.search_menu_btn,
            self.title_bar.appearance_menu_btn, self.title_bar.lsp_menu_btn,
            self.title_bar.git_menu_btn,
        ):
            btn.setVisible(visible)
        self.action_toolbar.setVisible(visible)
    # [FN CLOSED] _set_project_chrome_visible

    # [FN CATEGORY] _go_back_to_welcome — flushes any pending edit (nothing is discarded — autosave
    # means switching screens is always safe) and returns to the folder-picker/recent-folders screen
    # [FN] _go_back_to_welcome — the titlebar back arrow's action
    # [FN OPEN] _go_back_to_welcome
    def _go_back_to_welcome(self):
        if not self._flush_all_tabs():
            return
        self._refresh_recent_folders()
        self.stack.setCurrentIndex(0)
        self._set_project_chrome_visible(False)
        self.map_tab_btn.setParent(self.shell)
        self.map_tab_btn.setText(' MAPPA')
        self.map_tab_btn.setIcon(draw_icon('arrow-up', 12))
        self.map_tab_btn.hide()
        self.claude_tab_btn.hide()
        if self.map_dialog is not None:
            self.map_dialog.hide()
    # [FN CLOSED] _go_back_to_welcome

    def _choose_ai_agent(self):
        return self._ide_agent_choice_form(CLAUDE_MODELS, CODEX_MODELS, MODEL_DEFAULT)

    def _open_project_folder(self, path):
        path = os.path.abspath(path)
        self._invalidate_xref()
        if self.project_root_path and os.path.abspath(self.project_root_path) != path:
            if not self._close_all_tabs(flush=True):
                return
            self.settings.remove('session/openFile')
        if self.project_root_path != path:
            self.git_root = None
            self.git_status = {}
        self.project_root_path = path
        self._remember_folder(path)
        self.settings.setValue('session/openFolder', path)
        self.terminal.set_cwd(path)
        self.claude_pane.set_cwd(path)
        self._auto_select_interpreter()
        if is_python_majority_project(path):
            self._switch_terminal_tab(1)
        self._rebuild_tree()
        self._check_kant_map(path)
        if self.kant_map_path is None:
            if has_any_kant_tags(path):
                # tags already exist somewhere in the project — just the map file is missing, a
                # fast deterministic rebuild from what's already tagged, no AI call needed
                if self._ide_yes_no(
                    'KANT map',
                    'Nessuna KANT_*.md trovata in questo progetto. Generarla adesso?',
                ):
                    self._sync_kant_map()
            else:
                # no KANT convention anywhere yet — that requires actually reading the code and
                # deciding what to tag, which only /kant-code-map (via Claude Code) can do
                if self._ide_yes_no(
                    'Convenzione KANT',
                    'Questo progetto non usa ancora la convenzione KANT.\n'
                    'Lanciare /kant-code-map per taggare il codice e generare la mappa?',
                ):
                    choice = self._choose_ai_agent()
                    if choice:
                        self._launch_kant_code_map(choice['agent'], choice['model'], choice['effort'])
        self._watch_project_tree()
        self.stack.setCurrentIndex(1)
        self._set_project_chrome_visible(True)
        self.map_tab_btn.show()
        self._position_map_tab()
        self.claude_tab_btn.show()
        self._position_claude_tab()

    # [FN CATEGORY] _check_kant_map — looks for a KANT_*.md structural map at the project root (the
    # file /kant-code-map writes) and reflects whether one exists in the label below the project tree
    # [FN] _check_kant_map — detects the project's KANT_*.md map file, if any
    # [FN OPEN] _check_kant_map
    def _check_kant_map(self, folder):
        try:
            candidates = sorted(f for f in os.listdir(folder) if f.startswith('KANT_') and f.endswith('.md'))
        except OSError:
            candidates = []
        self.kant_map_path = os.path.join(folder, candidates[0]) if candidates else None
        self._update_kant_map_label()
    # [FN CLOSED] _check_kant_map

    def _update_kant_map_label(self):
        if self.kant_map_path:
            self.kant_map_label.setText(f'✓ {os.path.basename(self.kant_map_path)}')
            self.kant_map_label.setStyleSheet(f'color:{theme.OK}; padding:0 8px;')
        else:
            self.kant_map_label.setText('✗ Nessuna KANT_*.md — genera con /kant-code-map')
            self.kant_map_label.setStyleSheet(f'color:{theme.DIM}; padding:0 8px;')

    # [FN CATEGORY] _sync_kant_map — the IDE owns KANT_<project>.md as generated output, not a side
    # artifact a person hand-edits: every successful save resyncs it (via FileTab.saved), and
    # _open_project_folder offers to create it the first time a project has none. Rewalks the whole
    # project and re-reads every KANT-tagged file's top-level label, the same way _build_project_tree
    # does, rather than trusting whatever's currently open in tabs — a save in one file shouldn't make
    # the map forget every other file.
    # ponytail: full project rescan stays simple but runs off the UI thread; incremental state is not
    # worth owning until profiling shows the background pass itself is too expensive.
    # [FN] _sync_kant_map — rewrites KANT_<project>.md from the current on-disk KANT structure
    # [FN OPEN] _sync_kant_map
    def _sync_kant_map(self):
        if not self.project_root_path:
            return
        self._invalidate_xref()  # a save changed the code, so the reference graph may have too
        project_root = self.project_root_path
        project_name = os.path.basename(project_root)
        path = self.kant_map_path or os.path.join(project_root, f'KANT_{project_name}.md')
        self._map_sync_generation += 1
        generation = self._map_sync_generation

        def build_map():
            return build_kant_map(project_root, project_name)

        def save_map(text, error):
            if error or generation != self._map_sync_generation or project_root != self.project_root_path:
                return
            try:
                write_file_atomic(path, text)
            except OSError:
                return
            self.kant_map_path = path
            self._update_kant_map_label()

        self._run_background(build_map, save_map)
    # [FN CLOSED] _sync_kant_map

    # [FN] _validate_kant_project — validates the generated KANT map and marker structure after AI runs
    # [FN OPEN] _validate_kant_project
    def _validate_kant_project(self):
        if not self.project_root_path:
            return ''
        self._check_kant_map(self.project_root_path)
        result, errors, visual_errors, map_out_of_sync = validate_kant_project(self.project_root_path, self.kant_map_path)

        if map_out_of_sync:
            # "map non coerente" (KANT_<project>.md missing entries the source now has) is the one
            # validation failure that's mechanically, deterministically fixable — it's exactly what
            # _sync_kant_map already regenerates from source on every save. No reason to surface it
            # as an error the user has to notice and manually resync themselves; self-heal instead
            # and only report whatever real (marker-syntax) errors remain, if any.
            errors = [e for e in errors if not e.startswith('KANT map non coerente')]
            self._sync_kant_map()
            note = 'mappa KANT non coerente con il codice — rigenerata automaticamente'
            if errors:
                sample = '\n'.join(f'- {error}' for error in errors[:8])
                extra = f'\n- ... altri {len(errors) - 8} errori' if len(errors) > 8 else ''
                result = f'# KANT verifica: ERRORI\n{sample}{extra}\n# ({note})'
            else:
                result = f'# KANT verifica: OK ({note})'

        self._show_validation_results(errors, visual_errors)
        return result
    # [FN CLOSED] _validate_kant_project

    def _run_kant_validation(self):
        result = self._validate_kant_project()
        if result:
            self.terminal.write_info('\n' + result + '\n')

    def _show_validation_results(self, errors, visual_errors):
        if not hasattr(self, 'results_view'):
            return
        self.results_view.clear()
        root = QTreeWidgetItem(self.results_view, [f'Verifica KANT: {"OK" if not errors else str(len(errors)) + " errore/i"}'])
        for path, rel, line, message in visual_errors:
            item = QTreeWidgetItem(root, [f'{rel}:{line}: {message}'])
            item.setData(0, ROLE_KIND, 'validation-result')
            item.setData(0, ROLE_PATH, path)
            item.setData(0, ROLE_LINE, line)
            item.setData(0, ROLE_TEXT, message)
        for message in errors:
            if not any(message.startswith(f'{rel}:') for _path, rel, _line, _msg in visual_errors):
                QTreeWidgetItem(root, [message])
        if not errors:
            QTreeWidgetItem(root, ['Nessun errore'])
        root.setExpanded(True)
        self._toggle_info_popup(self.results_view, force_open=bool(errors))

    # [FN CATEGORY] _launch_kant_code_map — hands off to Claude Code itself rather than trying to
    # replicate its judgment: deciding what deserves a tag and writing a sensible description isn't
    # mechanical the way resyncing an already-tagged tree is. Forces kant-code-map too, so the
    # bundled skill body is used even when the opened project does not have that skill installed.
    # The request is plain language, NOT a literal "/kant-code-map":
    # claude -p parses a leading slash as a CLI command and rejects it ("unknown command") when the
    # skill isn't discoverable from the project's cwd — so the slash is referenced mid-sentence only,
    # where it reads as text, not as a command.
    # [FN] _launch_kant_code_map — asks Claude to run the KANT code map over the whole project
    # [FN OPEN] _launch_kant_code_map
    def _launch_kant_code_map(self, agent, model=None, effort=None):
        if model:
            self.claude_pane.set_agent(agent)  # refreshes the model combo's list for this agent first
            self.claude_pane.model_select.setCurrentText(model)
        self.claude_pane.run_prompt(
            'Applica la convenzione KANT all\'intero progetto, come il comando /kant-code-map: '
            'aggiungi i commenti tag KANT sopra ogni elemento del codice sorgente e crea o aggiorna '
            'KANT_<nome-progetto>.md alla radice del progetto.',
            extra_skills=('kant-code-map',),
            agent=agent,
            auto_permissions_once=True,
            effort=effort,
        )
    # [FN CLOSED] _launch_kant_code_map

    def _build_project_tree(self, parent_item, dir_path):
        files = sorted(iter_kant_tagged_files(dir_path), key=lambda p: os.path.relpath(p, dir_path).lower())
        for file_path in files:
            label, error = read_top_level_label_result(file_path)
            if error is not None:
                file_item = QTreeWidgetItem(parent_item)
                file_item.setData(0, ROLE_KIND, 'invalidfile')
                file_item.setData(0, ROLE_PATH, file_path)
                self.tree.setItemWidget(file_item, 0, self._invalid_file_label(file_item, os.path.basename(file_path), error))
                continue
            if label is None:
                continue  # no KANT tags — only convention-tagged files show up in the tree
            tag, desc, _tree, top_node = label
            file_item = QTreeWidgetItem(parent_item)
            file_item.setData(0, ROLE_KIND, 'file')
            file_item.setData(0, ROLE_PATH, file_path)
            file_item.setData(0, ROLE_UID, top_node.uid)  # the file's own top-level KANT element
            self.tree.setItemWidget(
                file_item, 0, self._tree_label(
                    file_item, tag, desc, bold=True, git_status=self._git_status_for_path(file_path),
                    detail=top_node.category_desc,
                )
            )
            # start from the top node's own children, not the node itself — it's already
            # shown as this file item's own label, showing it again would duplicate it. top_node
            # itself is document order 0 (_nodes_in_order's first result), so its children start at 1.
            self._build_outline_items(file_item, top_node, file_path, [1])

    def _build_outline_items(self, parent_item, node, path, order_counter):
        for child in node.body:
            if not isinstance(child, Node):
                continue
            item = QTreeWidgetItem(parent_item)
            item.setData(0, ROLE_KIND, 'section')
            item.setData(0, ROLE_UID, child.uid)
            item.setData(0, ROLE_PATH, path)
            item.setData(0, ROLE_ORDER, order_counter[0])
            order_counter[0] += 1
            self.tree.setItemWidget(item, 0, self._tree_label(item, child.tag, child.desc or child.name, detail=child.category_desc))
            self._build_outline_items(item, child, path, order_counter)

    def _tree_label(self, item, tag, text, bold=False, git_status='', detail=''):
        color = theme.TAG_COLORS.get(tag, theme.TEXT)
        bg = theme.TAG_BACKGROUNDS.get(tag, '#eef2f7')
        weight = '700' if bold else '400'
        git_html = (
            f' <span style="color:{theme.WARN}; font-weight:700">[{html_escape(git_status)}]</span>'
            if git_status else ''
        )
        # rich-text spans ignore QLabel.setFont proportionally, so the size needs to be explicit
        # here rather than inherited — one point larger than the label's own TREE_FONT_PT
        detail_html = (
            f'<br><span style="color:{theme.DIM}; font-size:{theme.TREE_FONT_PT + 1}pt">{html_escape(detail)}</span>'
            if detail else ''
        )
        lbl = _TreeItemLabel(
            self.tree, item,
            f'<span style="color:{color}; background-color:{bg}; font-weight:700; '
            f'padding:0px 4px; border-radius:4px">[{tag}]</span> '
            f'<span style="font-weight:{weight}">{html_escape(text)}</span>{git_html}{detail_html}'
        )
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        lbl.setMargin(0)
        lbl.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:0px 4px;')
        lbl.setWordWrap(True)  # long labels wrap instead of overflowing the column
        lbl.setCursor(Qt.PointingHandCursor)
        return lbl

    # [FN CATEGORY] _build_plain_project_tree — classic PyCharm-style file browser: every file and
    # folder by its real name, no KANT labeling, no outline nesting under files — just the filesystem
    # [FN] _build_plain_project_tree — renders the project folder tree by filename ("File" mode)
    # [FN OPEN] _build_plain_project_tree
    def _build_plain_project_tree(self, parent_item, dir_path):
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                if entry.name in theme.IGNORE_DIRS:
                    continue
                marker = ' *' if self._git_status_for_dir(entry.path) else ''
                dir_item = QTreeWidgetItem(parent_item, [entry.name + marker])
                dir_item.setData(0, ROLE_KIND, 'dir')
                dir_item.setData(0, ROLE_PATH, entry.path)
                dir_item.setForeground(0, QColor(theme.DIM))
                self._build_plain_project_tree(dir_item, entry.path)
            else:
                file_item = QTreeWidgetItem(parent_item)
                file_item.setData(0, ROLE_KIND, 'plainfile')
                file_item.setData(0, ROLE_PATH, entry.path)
                self.tree.setItemWidget(file_item, 0, self._plain_file_label(file_item, entry.name, self._git_status_for_path(entry.path)))
    # [FN CLOSED] _build_plain_project_tree

    # [FN CATEGORY] _build_groupings_tree — "Gruppi" mode: one top-level row per saved Grouping
    # (kant/groupings.py), its members as children — resolved against the same cross-reference
    # graph (_get_xref) the Incoming/Outgoing panel already builds, so a member row gets real tag/
    # file/description for free instead of needing its own lookup. A member whose element no longer
    # resolves (renamed/deleted since the grouping was saved) still shows, dimmed, rather than
    # silently vanishing — the grouping itself is the source of truth, not today's xref snapshot.
    # [FN] _build_groupings_tree — renders every project grouping and its members ("Gruppi" mode)
    # [FN OPEN] _build_groupings_tree
    def _build_groupings_tree(self, parent_item, dir_path):
        groupings = load_groupings(dir_path)
        if not groupings:
            empty_item = QTreeWidgetItem(parent_item, ['Nessun gruppo — usa "+ Nuovo gruppo" per crearne uno'])
            empty_item.setData(0, ROLE_KIND, 'grouping_empty')
            empty_item.setForeground(0, QColor(theme.DIM))
            return
        xref = self._get_xref()
        for grouping in groupings:
            group_item = QTreeWidgetItem(parent_item)
            group_item.setData(0, ROLE_KIND, 'grouping')
            group_item.setData(0, ROLE_KEY, grouping.id)
            label = _TreeItemLabel(
                self.tree, group_item,
                f'<span style="font-weight:700">{html_escape(grouping.name)}</span> '
                f'<span style="color:{theme.DIM}">({len(grouping.members)})</span>'
            )
            label.setFont(QFont('Consolas', theme.TREE_FONT_PT, QFont.DemiBold))
            label.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:0px 4px;')
            label.setCursor(Qt.PointingHandCursor)
            self.tree.setItemWidget(group_item, 0, label)
            for key in grouping.members:
                member_item = QTreeWidgetItem(group_item)
                member_item.setData(0, ROLE_KIND, 'grouping_member')
                member_item.setData(0, ROLE_KEY, key)
                element = xref.get(key)
                if element is not None:
                    self.tree.setItemWidget(
                        member_item, 0,
                        self._tree_label(member_item, element.tag, element.desc or element.name, detail=element.file),
                    )
                else:
                    rel = key.split('::', 1)[0]
                    dim_label = _TreeItemLabel(
                        self.tree, member_item,
                        f'<span style="color:{theme.DIM}">{html_escape(rel)} (non risolvibile)</span>',
                    )
                    dim_label.setFont(QFont('Consolas', theme.TREE_FONT_PT))
                    dim_label.setStyleSheet('background:transparent; padding:0px 4px;')
                    self.tree.setItemWidget(member_item, 0, dim_label)
            group_item.setExpanded(True)
    # [FN CLOSED] _build_groupings_tree

    def _invalid_file_label(self, item, name, error):
        lbl = _TreeItemLabel(
            self.tree, item,
            f'<span style="color:{theme.TAG_COLORS["TST"]}; font-weight:700">[ERR]</span> '
            f'{html_escape(name)} <span style="color:{theme.DIM}">{html_escape(str(error))}</span>'
        )
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        lbl.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:0px 4px;')
        lbl.setWordWrap(True)
        lbl.setCursor(Qt.PointingHandCursor)
        return lbl

    def _plain_file_label(self, item, name, git_status=''):
        git_html = (
            f' <span style="color:{theme.WARN}; font-weight:700">[{html_escape(git_status)}]</span>'
            if git_status else ''
        )
        lbl = _TreeItemLabel(self.tree, item, html_escape(name) + git_html)
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        lbl.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:0px 4px;')
        lbl.setWordWrap(True)
        lbl.setCursor(Qt.PointingHandCursor)
        return lbl

    def _on_tree_item_clicked(self, item, _column):
        kind = item.data(0, ROLE_KIND)
        if kind == 'file':
            path = item.data(0, ROLE_PATH)
            if self._open_file(path):
                tab = self.open_tabs.get(path)
                if tab is not None:
                    self._render_view(tab)
                    self._update_io_tabs(None)
        elif kind in ('plainfile', 'invalidfile'):
            self._open_file(item.data(0, ROLE_PATH))
        elif kind == 'grouping_member':
            self._navigate_to_element(item.data(0, ROLE_KEY))
        elif kind == 'section':
            path = item.data(0, ROLE_PATH)
            order = item.data(0, ROLE_ORDER)
            self._open_file(path)
            tab = self.open_tabs.get(path)
            if tab is None:
                return
            uid = item.data(0, ROLE_UID)
            # a legacy (no #id) file mints a fresh uid on every reparse (_open_file always
            # re-reads from disk rather than trusting the tree's last parse), so the tree item's
            # uid can silently fail to match tab.tree — falling through to the whole-file view
            # instead of isolating. Document order survives reparse and is the reliable fallback,
            # the same pattern _navigate_to_element already uses for this exact problem.
            node = self._find_node_by_uid(tab.tree, uid)
            if node is None and order is not None:
                nodes = self._nodes_in_order(tab.tree)
                if order < len(nodes):
                    node = nodes[order]
            resolved_uid = node.uid if node is not None else uid
            self._show_element_tab(tab, resolved_uid)
            self._update_io_tabs(resolved_uid)

    # [FN] _on_tree_item_double_clicked — opens the item in the shared coding tab bar
    # [FN OPEN] _on_tree_item_double_clicked
    def _on_tree_item_double_clicked(self, item, _column):
        kind = item.data(0, ROLE_KIND)
        if kind not in ('file', 'section'):
            return
        path = item.data(0, ROLE_PATH)
        tab = self.open_tabs.get(path)
        if tab is None:
            if not self._open_file(path):
                return
            tab = self.open_tabs.get(path)
            if tab is None:
                return
        if kind == 'file':
            self.tabs.setCurrentWidget(tab)
            return
        order = item.data(0, ROLE_ORDER)
        uid = item.data(0, ROLE_UID)
        node = self._find_node_by_uid(tab.tree, uid)
        if node is None and order is not None:
            nodes = self._nodes_in_order(tab.tree)
            if order < len(nodes):
                node = nodes[order]
        self._show_element_tab(tab, node.uid if node is not None else uid)
    # [FN CLOSED] _on_tree_item_double_clicked

    # [FN CATEGORY] _show_element_tab — VS Code-style preview reuse: clicking a KANT element that
    # isn't already open retargets the one unpinned "preview" tab (if any) in place instead of
    # piling up a new tab per click. Pinning a tab (the pin button in place of its ×) takes it out
    # of the reusable slot, so the next new element opens a fresh tab that is itself the new preview
    # — repeating indefinitely, exactly like the file/uid dedupe case already handled below. Editing
    # the preview tab's content pins it too (see the dirty check below) — otherwise clicking a
    # different element while mid-edit silently swaps the view out from under the user with no
    # warning, same failure VS Code's own "promote preview to permanent on edit" rule prevents.
    # [FN] _show_element_tab — opens an element beside its parent in the main coding tab bar
    # [FN OPEN] _show_element_tab
    def _show_element_tab(self, tab, uid):
        key = (tab.path, uid)
        existing = self._element_pages.get(key)
        if existing is not None:
            self.tabs.setCurrentWidget(existing)
            self._set_ai_context_page(existing)
            return
        if self._preview_page is not None:
            if self._preview_page._file_tab.dirty:
                self._pin_element_page(self._preview_page)
            else:
                self._retarget_element_page(self._preview_page, tab, uid)
                return
        page, layout = self._build_element_page()
        if uid is None:
            node = next((item for item in tab.tree.body if isinstance(item, Node)), None)
        else:
            node = self._find_node_by_uid(tab.tree, uid)
        if node is None:
            page.deleteLater()
            return
        page._element_key = key
        page._file_tab = tab
        page._view_layout = layout
        page._pinned = False
        index = self.tabs.addTab(page, '')
        page._tab_label = _TabLabel(self.tabs, page)
        self.tabs.setTabToolTip(index, tab.path)
        self._element_pages[key] = page
        self._update_element_tab_title(page)
        self._set_preview_page(page)
        self.tabs.setCurrentIndex(index)
        self._set_ai_context_page(page)
    # [FN CLOSED] _show_element_tab

    # [FN CATEGORY] _retarget_element_page — the actual "reuse this tab slot" move: re-key
    # _element_pages, point the page at the new (file, element), and let _render_element_page
    # rebuild its content from scratch — no risk to unsaved edits, since the underlying FileTab
    # (open_tabs, dirty state, tree) for whichever file the page WAS showing is untouched by this;
    # only which node this particular tab slot displays changes.
    # [FN] _retarget_element_page — reuses an existing (presumably preview) page for a new element
    # [FN OPEN] _retarget_element_page
    def _retarget_element_page(self, page, tab, uid):
        node = (
            next((item for item in tab.tree.body if isinstance(item, Node)), None) if uid is None
            else self._find_node_by_uid(tab.tree, uid)
        )
        if node is None:
            return
        old_key = getattr(page, '_element_key', None)
        if old_key is not None:
            self._element_pages.pop(old_key, None)
        page._element_key = (tab.path, node.uid)
        page._file_tab = tab
        self._element_pages[page._element_key] = page
        index = self.tabs.indexOf(page)
        self.tabs.setTabToolTip(index, tab.path)
        self._render_element_page(page)
        self._update_element_tab_title(page)
        self.tabs.setCurrentWidget(page)
        self._set_ai_context_page(page)
    # [FN CLOSED] _retarget_element_page

    # [FN CATEGORY] _set_preview_page — swaps the new preview page's tab-bar × (QTabBar.RightSide)
    # for a pin button; pinning swaps it again for a plain close button (not Qt's native fallback —
    # a pinned tab has nothing left to "unpin" back to, since it's no longer anyone's preview slot,
    # so the only remaining action for that corner is closing it outright) and frees this page from
    # ever being silently retargeted again.
    # [FN] _set_preview_page — marks a page as the one reusable/unpinned preview tab
    # [FN OPEN] _set_preview_page
    def _set_preview_page(self, page):
        self._preview_page = page
        pin_btn = QToolButton()
        pin_btn.setText('📌')
        pin_btn.setAutoRaise(True)
        pin_btn.setCursor(Qt.PointingHandCursor)
        pin_btn.setToolTip('Blocca questa scheda (impedisce che venga sostituita da un nuovo elemento)')
        pin_btn.clicked.connect(lambda: self._pin_element_page(page))
        index = self.tabs.indexOf(page)
        self.tabs.tabBar().setTabButton(index, QTabBar.RightSide, pin_btn)
        # setTabButton alone can leave the widget internally hidden with no matching re-show — the
        # exact same reproduced Qt bug _update_tab_title's _tab_label already works around (see its
        # comment); missing here meant a real mouse click could land on nothing; QTest.mouseClick
        # calling the widget directly bypassed hit-testing and never caught it.
        pin_btn.show()
    # [FN CLOSED] _set_preview_page

    def _pin_element_page(self, page):
        if page is None:
            return
        page._pinned = True
        if self._preview_page is page:
            self._preview_page = None
        index = self.tabs.indexOf(page)
        if index == -1:
            return
        close_btn = QToolButton()
        close_btn.setText('×')
        close_btn.setAutoRaise(True)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip('Chiudi questa scheda')
        close_btn.clicked.connect(lambda: self._close_element_tab(page))
        bar = self.tabs.tabBar()
        old_btn = bar.tabButton(index, QTabBar.RightSide)
        bar.setTabButton(index, QTabBar.RightSide, close_btn)
        close_btn.show()  # same setTabButton re-show workaround as _set_preview_page above
        if old_btn is not None:
            # setTabButton only detaches the old widget, it doesn't delete it — same leak
            # _update_tab_title's _tab_label cleanup already guards against, just for this corner
            old_btn.deleteLater()

    # [FN] _set_preview_file_tab — same pin-button swap as _set_preview_page, one level up for
    # whole-file tabs (see _open_file for why files are closed-and-reopened rather than retargeted)
    # [FN OPEN] _set_preview_file_tab
    def _set_preview_file_tab(self, tab):
        self._preview_file_tab = tab
        pin_btn = QToolButton()
        pin_btn.setText('📌')
        pin_btn.setAutoRaise(True)
        pin_btn.setCursor(Qt.PointingHandCursor)
        pin_btn.setToolTip('Blocca questa scheda (impedisce che venga sostituita aprendo un altro file)')
        pin_btn.clicked.connect(lambda: self._pin_file_tab(tab))
        index = self.tabs.indexOf(tab)
        self.tabs.tabBar().setTabButton(index, QTabBar.RightSide, pin_btn)
        pin_btn.show()  # same setTabButton re-show workaround as _set_preview_page
    # [FN CLOSED] _set_preview_file_tab

    def _pin_file_tab(self, tab):
        if tab is None:
            return
        if self._preview_file_tab is tab:
            self._preview_file_tab = None
        index = self.tabs.indexOf(tab)
        if index == -1:
            return
        close_btn = QToolButton()
        close_btn.setText('×')
        close_btn.setAutoRaise(True)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip('Chiudi questa scheda')
        close_btn.clicked.connect(lambda: self._close_tab(self.tabs.indexOf(tab)))
        bar = self.tabs.tabBar()
        old_btn = bar.tabButton(index, QTabBar.RightSide)
        bar.setTabButton(index, QTabBar.RightSide, close_btn)
        close_btn.show()  # same setTabButton re-show workaround as _set_preview_page
        if old_btn is not None:
            old_btn.deleteLater()  # same leak _update_tab_title's _tab_label cleanup already guards against

    def _render_element_page(self, page):
        layout = page._view_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        tab = page._file_tab
        node = self._find_node_by_uid(tab.tree, page._element_key[1])
        if node is not None:
            self._build_node_widgets(tab, Node(tag='ROOT', name='', open_raw=None, body=[node]), layout, 0)

    def _update_element_tab_title(self, page):
        tab = page._file_tab
        node = self._find_node_by_uid(tab.tree, page._element_key[1])
        if node is None:
            return
        html = _tag_header_html(node.tag, node.name, node.desc, bold_name=True)
        if tab.dirty:
            html += f' <span style="color:{theme.ACCENT}">●</span>'
        page._tab_label.setText(html)
        page._tab_label.adjustSize()
        index = self.tabs.indexOf(page)
        self.tabs.tabBar().setTabButton(index, QTabBar.LeftSide, page._tab_label)
        page._tab_label.show()

    def _close_element_tab(self, page):
        if page is None:
            return
        self._element_pages.pop(getattr(page, '_element_key', None), None)
        if self._preview_page is page:
            self._preview_page = None
        if self._ai_context_page is page:
            self._ai_context_page = self.active_tab
        index = self.tabs.indexOf(page)
        if index != -1:
            self.tabs.removeTab(index)
        page.deleteLater()

    def _close_element_tabs_for(self, tab):
        for page in list(self._element_pages.values()):
            if page._file_tab is tab:
                self._close_element_tab(page)

    # ---- tabs (AI-NAV: active-tab ownership and close/flush lifecycle) ---

    @property
    def active_page(self):
        return self.tabs.currentWidget()

    @property
    def active_tab(self):
        page = self.active_page
        return getattr(page, '_file_tab', page)

    def _active_filter_uid(self):
        page = self.active_page
        return page._element_key[1] if hasattr(page, '_element_key') else getattr(page, 'filter_uid', None)

    def _on_active_tab_changed(self, _index):
        page = self.active_page
        tab = self.active_tab
        if hasattr(page, '_element_key'):
            self._render_element_page(page)
        elif isinstance(page, FileTab):
            self._render_view(page, page.filter_uid)
        if page is not None:
            self._set_ai_context_page(page)
        self._update_action_buttons()
        self._update_filename_label()
        self._update_syntax_status()
        self._update_lsp_diagnostics()
        self._update_io_tabs(self._active_filter_uid())
        self.encoding_label.setText('UTF-8' if tab else '')
        self.cursor_pos_label.setText('')

    # [FN CATEGORY] _close_tab — closes one tab; flushes its pending autosave first unless the file
    # is about to be deleted out from under it (flush=False), in which case that would just recreate
    # the file we're deleting
    # [FN] _close_tab — closes the tab at the given index
    # [FN OPEN] _close_tab
    def _close_tab(self, index, flush=True):
        page = self.tabs.widget(index)
        if page is None:
            return True
        tab = getattr(page, '_file_tab', page)
        if page is not tab:
            self._close_element_tab(page)
            return True
        if flush:
            if not tab.flush_pending_save():
                return False
        else:
            tab.autosave_timer.stop()
        self.lsp_client.close_document(tab.path)
        if tab.path in self.open_tabs:
            del self.open_tabs[tab.path]
        if tab.path in self.fs_watcher.files():
            self.fs_watcher.removePath(tab.path)
        self._close_element_tabs_for(tab)
        if self._preview_file_tab is tab:
            self._preview_file_tab = None
        index = self.tabs.indexOf(tab)  # child tabs may have been dragged before it and shifted it
        if index != -1:
            self.tabs.removeTab(index)
        tab.deleteLater()
        return True
    # [FN CLOSED] _close_tab

    def _close_active_tab(self):
        if self.tabs.currentIndex() != -1:
            self._close_tab(self.tabs.currentIndex())

    def _flush_all_tabs(self):
        ok = True
        for tab in self.open_tabs.values():
            ok = tab.flush_pending_save() and ok
        return ok

    def _close_all_tabs(self, flush=True):
        while self.tabs.count():
            if not self._close_tab(0, flush=flush):
                return False
        return True

    # [FN] _kant_identity_node — the node whose identity a tab should be shown by: whatever's
    # isolated, or (optionally) the file's own top-level KANT node when nothing is; None when
    # neither is available (untagged/unparseable file)
    # [FN OPEN] _kant_identity_node
    def _kant_identity_node(self, tab, fallback_to_top_level=False):
        node = self._find_node_by_uid(tab.tree, tab.filter_uid) if tab.filter_uid else None
        if node is None and fallback_to_top_level:
            node = next((n for n in tab.tree.body if isinstance(n, Node)), None)
        return node
    # [FN CLOSED] _kant_identity_node

    def _kant_identity_text(self, tab, fallback_to_top_level=False):
        node = self._kant_identity_node(tab, fallback_to_top_level)
        return f'[{node.tag}] {node.desc or node.name}' if node is not None else None

    # [FN CATEGORY] _update_tab_title — the main tab is titled by the KANT identity of whatever
    # it's isolated on, or the file's own top-level tag in whole-file view — matching the title
    # bar and element-tab label, which both already do this. It uses the same colored/bold
    # "[TAG] name" convention as the tree and coding panel too, via a rich-HTML label swapped in
    # for the tab's plain text (see _TabLabel) — only an untagged/unparseable file falls back to
    # plain filename text, since there's no tag to color.
    # [FN] _update_tab_title — refreshes a tab's label to match what it's currently showing
    # [FN OPEN] _update_tab_title
    def _update_tab_title(self, tab):
        idx = self.tabs.indexOf(tab)
        if idx == -1:
            return
        bar = self.tabs.tabBar()
        node = self._kant_identity_node(tab, fallback_to_top_level=True)
        if node is None:
            if getattr(tab, '_tab_label', None) is not None:
                bar.setTabButton(idx, QTabBar.LeftSide, None)
                # setTabButton only detaches/hides the old widget, it doesn't delete it — without
                # this, every tag<->untagged transition on this tab leaked one hidden orphaned
                # QLabel, never freed until the whole tab bar (window) closes
                tab._tab_label.deleteLater()
                tab._tab_label = None
            name = os.path.basename(tab.path)
            self.tabs.setTabText(idx, (name + ' ●') if tab.dirty else name)
            return
        html = _tag_header_html(node.tag, node.name, node.desc, bold_name=True)
        if tab.dirty:
            html += f' <span style="color:{theme.ACCENT}">●</span>'
        if getattr(tab, '_tab_label', None) is None:
            tab._tab_label = _TabLabel(self.tabs, tab)
        # set the text BEFORE (re-)registering the button — QTabBar computes its tab layout
        # against the button's sizeHint at registration time, not on later in-place text changes,
        # so re-registering after every text update is what keeps the tab wide enough for it
        tab._tab_label.setText(html)
        tab._tab_label.adjustSize()
        bar.setTabButton(idx, QTabBar.LeftSide, tab._tab_label)
        # re-registering the SAME widget instance at a position it already occupies makes Qt hide
        # it internally without a matching re-show (a real, reproduced bug — every theme refresh
        # or repeated _update_tab_title call left the label blank), so force it visible again here
        tab._tab_label.show()
        self.tabs.setTabText(idx, '')
    # [FN CLOSED] _update_tab_title

    def _on_tab_dirty_changed(self, tab):
        self._update_tab_title(tab)
        for page in self._element_pages.values():
            if page._file_tab is tab:
                self._update_element_tab_title(page)
        self._update_action_buttons()
        if tab is self.active_tab:
            self._update_filename_label()
            self.syntax_timer.start(800)

    def _on_tab_save_failed(self, tab, message):
        self.terminal.write_info(f'\n# save errore: {tab.path}\n{message}\n')
        if tab is self.active_tab:
            self.syntax_label.setText(f'SAVE ERR: {message}')
            self.syntax_label.setStyleSheet(f'color:{theme.TAG_COLORS["TST"]}; font-weight:700;')

    # ---- file open/save (AI-NAV: parse -> FileTab -> atomic save) --------

    # [FN CATEGORY] _open_file — opens a file as a new tab, or just switches to it if it's already
    # open (an already-open tab's live edits are never discarded/re-read from disk by re-clicking it)
    # [FN] _open_file — opens or activates a file's tab
    # [FN OPEN] _open_file
    def _open_file(self, path):
        existing = self.open_tabs.get(path)
        if existing is not None:
            self.tabs.setCurrentWidget(existing)
            self._set_ai_context_page(existing)
            return True
        # always re-read from disk rather than trusting a tree parsed earlier for the sidebar —
        # the file may have changed since then (fs-watcher debounce, external edit), and a stale
        # tree here would silently overwrite newer disk content on the next save
        try:
            with open(path, 'r', encoding='utf-8', newline='') as f:
                text = f.read()
            tree = parse_kant(text)
        except UnicodeDecodeError:
            self._ide_message('File non testuale', f'{os.path.basename(path)} non e un file UTF-8 apribile.')
            return False
        except OSError as e:
            self._ide_message('Apri file', f'Impossibile aprire {os.path.basename(path)}: {e}')
            return False
        except KantParseError as e:
            self._ide_message('Marcatori KANT non validi', f'{os.path.basename(path)}: {e}\nApro il file come testo grezzo.')
            tree = Node(tag='ROOT', name='', open_raw=None, body=[Run(lines=text.split('\n'))])

        # reuse the preview file-tab slot in place (same tab index) instead of piling up a new
        # permanent tab per file click — a dirty preview is pinned first, same rule _show_element_tab
        # already applies to element tabs, so mid-edit content is never silently closed. A pinned
        # CHILD element tab counts the same as dirty content: closing the file tab would force-close
        # every element tab under it too (_close_tab -> _close_element_tabs_for has no pin exception,
        # since an explicit "close this file" really should take its element views with it) — but here
        # the file itself was never asked to close, the user just clicked a different file, so a
        # pinned child must promote its still-unpinned parent to pinned rather than silently losing it.
        insert_index = None
        if self._preview_file_tab is not None:
            has_pinned_child = any(
                page._pinned and page._file_tab is self._preview_file_tab
                for page in self._element_pages.values()
            )
            if self._preview_file_tab.dirty or has_pinned_child:
                self._pin_file_tab(self._preview_file_tab)
            else:
                insert_index = self.tabs.indexOf(self._preview_file_tab)
                if not self._close_tab(insert_index):
                    insert_index = None

        tab = FileTab(path, tree, detect_line_ending(path))
        tab.dirtyChanged.connect(lambda t=tab: self._on_tab_dirty_changed(t))
        tab.saveFailed.connect(lambda msg, t=tab: self._on_tab_save_failed(t, msg))
        tab.saveConflict.connect(lambda t=tab: self._on_tab_save_conflict(t))
        tab.saved.connect(lambda t=tab: self._on_tab_saved(t))
        self.open_tabs[path] = tab
        self._watch_open_file(path)
        # the tab's tree carries the authoritative in-memory #ids from here on (see _get_xref),
        # so any graph built from the disk parse alone is stale the moment a tab opens
        self._invalidate_xref()
        if insert_index is not None and 0 <= insert_index <= self.tabs.count():
            idx = self.tabs.insertTab(insert_index, tab, os.path.basename(path))
        else:
            idx = self.tabs.addTab(tab, os.path.basename(path))
        self.tabs.setTabToolTip(idx, path)
        self._set_preview_file_tab(tab)
        # setCurrentWidget below fires currentChanged -> _on_active_tab_changed, which renders
        # this tab already (whether via addTab's implicit switch when it's the first tab, or via
        # this explicit switch otherwise) — a second _render_view() here used to double-build the
        # widget tree, leaving stale deleteLater()-pending CodeEdits mixed into findChildren(CodeEdit)
        # results (broke vim's j/k structural motion, among anything else scanning that list).
        self.tabs.setCurrentWidget(tab)
        self._set_ai_context_page(tab)
        self._update_lsp_diagnostics()
        self.settings.setValue('session/openFile', path)
        return True
    # [FN CLOSED] _open_file

    def _save_file(self):
        tab = self.active_tab
        if tab is None:
            return
        if not tab.save():
            return
        self._update_tab_title(tab)
        self._update_filename_label()
        self._update_syntax_status()

    def _undo_file(self):
        tab = self.active_tab
        if tab is None or not tab.undo_file():
            return
        self._render_view(tab, tab.filter_uid)
        self._update_tab_title(tab)
        self._update_filename_label()
        self._update_action_buttons()
        self._update_syntax_status()
        self._update_lsp_diagnostics()

    def _redo_file(self):
        tab = self.active_tab
        if tab is None or not tab.redo_file():
            return
        self._render_view(tab, tab.filter_uid)
        self._update_tab_title(tab)
        self._update_filename_label()
        self._update_action_buttons()
        self._update_syntax_status()
        self._update_lsp_diagnostics()

    def _update_filename_label(self):
        tab = self.active_tab
        # the title bar slot shows the KANT identity (isolated element, or the file's own top-level
        # tag when nothing is filtered in) — the filename itself lives in file_path_label instead,
        # on the Incoming/Outgoing row
        uid = self._active_filter_uid()
        node = self._find_node_by_uid(tab.tree, uid) if tab and uid else None
        if node is None and tab:
            node = next((n for n in tab.tree.body if isinstance(n, Node)), None)
        text = f'[{node.tag}] {node.desc or node.name}' if node is not None else ''
        if tab and tab.dirty:
            text += '  ●'
        self.filename_label.setText(text)
        self.filename_label.setStyleSheet(f'color:{theme.ACCENT if (tab and tab.dirty) else theme.DIM};')
        self.file_path_label.setText(os.path.basename(tab.path) if tab else '')

    def _update_syntax_status(self):
        tab = self.active_tab
        if tab is None:
            self.syntax_label.setText('')
            if hasattr(self, 'errors_view'):
                self.errors_view.clear()
            return
        path = tab.path
        text = serialize_kant(tab.tree)
        self.syntax_label.setText('Controllo sintassi...')
        python_exe = self._active_python()
        self._run_background(
            lambda: check_file_syntax(path, text, python_exe),
            lambda result, error: self._apply_syntax_status(path, text, result, error),
        )

    # [FN CATEGORY] _apply_syntax_status — the local checker (check_file_syntax) covers most
    # languages with a real compiler/interpreter's syntax-only pass, but for anything it can't run
    # (tool missing from PATH) it falls back to a shallow bracket-balance check that can say "OK"
    # on code an LSP server would flag. When a language server is running and has real errors, its
    # diagnostics override a merely-shallow "OK" instead of just being appended as trailing text.
    # [FN] _apply_syntax_status — combines the local checker with LSP diagnostics, LSP taking
    # priority when the two disagree
    # [FN OPEN] _apply_syntax_status
    def _apply_syntax_status(self, path, text, result, error):
        tab = self.active_tab
        if tab is None or tab.path != path or serialize_kant(tab.tree) != text:
            return
        if error:
            result = {'ok': False, 'line': 1, 'message': str(error)}
        lsp = lsp_server_for_path(path)
        lsp_text = self._lsp_status_text(tab.path, lsp)
        lsp_error_diag = self._lsp_first_error(path) if lsp else None
        if result['ok'] and lsp_error_diag is not None:
            line = lsp_error_diag.get('range', {}).get('start', {}).get('line', 0) + 1
            message = lsp_error_diag.get('message', '').splitlines()[0]
            self.syntax_label.setText(f"ERR riga {line}: {message}{lsp_text}")
            self.syntax_label.setStyleSheet(f'color:{theme.TAG_COLORS["TST"]}; font-weight:700;')
        elif result['ok']:
            self.syntax_label.setText(f"OK {result.get('message', 'Sintassi OK')}{lsp_text}")
            self.syntax_label.setStyleSheet(f'color:{theme.OK}; font-weight:700;')
        else:
            self.syntax_label.setText(f"ERR riga {result.get('line', 1)}: {result['message']}{lsp_text}")
            self.syntax_label.setStyleSheet(f'color:{theme.TAG_COLORS["TST"]}; font-weight:700;')
        self._refresh_errors_view(tab, result)
    # [FN CLOSED] _apply_syntax_status

    # [FN CATEGORY] _refresh_errors_view — mirrors whatever _apply_syntax_status just decided is
    # authoritative for the active file into the terminal dock's errors tab: the full LSP
    # diagnostics list when a server is running and has any, otherwise the single local syntax
    # error (same source result already merged into syntax_label, just as a clickable list here).
    # [FN] _refresh_errors_view — populates the errors tab from the active file's current status
    # [FN OPEN] _refresh_errors_view
    def _refresh_errors_view(self, tab, local_result):
        if not hasattr(self, 'errors_view'):
            return
        self.errors_view.clear()
        path = tab.path
        diagnostics = self.lsp_diagnostics.get(os.path.abspath(path), [])
        entries = diagnostics[:100] if diagnostics else (
            [] if local_result.get('ok', True) else [{
                'range': {'start': {'line': local_result.get('line', 1) - 1}}, 'message': local_result.get('message', ''),
            }]
        )
        for diag in entries:
            line = diag.get('range', {}).get('start', {}).get('line', 0) + 1
            message = diag.get('message', '').splitlines()[0]
            item = QTreeWidgetItem(self.errors_view, [f'{os.path.basename(path)}:{line}: {message}'])
            item.setData(0, ROLE_KIND, 'diagnostic-result')
            item.setData(0, ROLE_PATH, path)
            item.setData(0, ROLE_LINE, line)
            item.setData(0, ROLE_TEXT, message)
            # LSP severity: 1=Error, 2=Warning, 3=Info, 4=Hint (missing severity, e.g. the local
            # syntax-check fallback above, means "the one thing that's wrong" -> treat as an error)
            severity = diag.get('severity', 1)
            color = {1: theme.TAG_COLORS['TST'], 2: theme.HOT}.get(severity, theme.DIM)
            item.setForeground(0, QColor(color))
    # [FN CLOSED] _refresh_errors_view

    def _lsp_first_error(self, path):
        """First Error-severity (LSP severity 1, or unspecified) diagnostic for path, or None."""
        for diag in self.lsp_diagnostics.get(os.path.abspath(path), []):
            if diag.get('severity', 1) == 1:
                return diag
        return None

    def _lsp_status_text(self, path, server):
        if not server:
            return ' | LSP locale'
        diagnostics = self.lsp_diagnostics.get(os.path.abspath(path), [])
        if not diagnostics:
            return f' | LSP {server}'
        first = diagnostics[0]
        line = first.get('range', {}).get('start', {}).get('line', 0) + 1
        msg = first.get('message', '').splitlines()[0]
        return f' | LSP {server}: {len(diagnostics)} diag, riga {line}: {msg[:80]}'

    def _update_lsp_diagnostics(self, tab=None):
        tab = tab or self.active_tab
        if tab is None:
            return
        text = serialize_kant(tab.tree)
        self.lsp_client.update_document(self.project_root_path, tab.path, text)

    def _on_lsp_diagnostics(self, path, diagnostics):
        self.lsp_diagnostics[os.path.abspath(path)] = diagnostics
        tab = self.active_tab
        if tab and os.path.abspath(tab.path) == os.path.abspath(path):
            self._update_syntax_status()
            self._show_lsp_diagnostics(tab, diagnostics)

    def _show_lsp_diagnostics(self, tab, diagnostics):
        if not diagnostics or not hasattr(self, 'results_view'):
            return
        self.results_view.clear()
        root = QTreeWidgetItem(self.results_view, [f'LSP diagnostics: {len(diagnostics)}'])
        for diag in diagnostics[:100]:
            start = diag.get('range', {}).get('start', {})
            line = start.get('line', 0) + 1
            message = diag.get('message', '').splitlines()[0]
            item = QTreeWidgetItem(root, [f'{os.path.basename(tab.path)}:{line}: {message}'])
            item.setData(0, ROLE_KIND, 'diagnostic-result')
            item.setData(0, ROLE_PATH, tab.path)
            item.setData(0, ROLE_LINE, line)
            item.setData(0, ROLE_TEXT, message)
        root.setExpanded(True)
        self._toggle_info_popup(self.results_view, force_open=True)

    def _line_count_before_run(self, node, target):
        count = 0
        for item in node.body:
            if isinstance(item, Run):
                if item is target:
                    return count
                count += len(item.lines)
                continue
            if item.category_raw:
                count += 1
            if item.tag_raw:
                count += 1
            if item.open_raw:
                count += 1
            inner = self._line_count_before_run(item, target)
            if inner is not None:
                return count + inner
            if item.closed_raw:
                count += 1
            if item.incoming_raw:
                count += 1
            if item.outgoing_raw:
                count += 1
        return None

    def _active_lsp_position(self, edit=None, cursor=None):
        if edit is None:
            tab = self.active_tab
            if tab is None:
                return None
            edit = QApplication.focusWidget()
            if not isinstance(edit, CodeEdit):
                edits = self.active_page.findChildren(CodeEdit)
                edit = edits[0] if edits else None
            if edit is None:
                return None
        else:
            # edit may belong to an element tab, not whatever file tab was active before it —
            # kant_tab (set at construction) is the reliable owner, self.active_tab only a fallback
            tab = getattr(edit, 'kant_tab', None) or self.active_tab
            if tab is None:
                return None
        if cursor is None:
            cursor = edit.textCursor()
        offset = self._line_count_before_run(tab.tree, getattr(edit, 'kant_item', None))
        if offset is None:
            offset = 0
        return {
            'textDocument': {'uri': file_uri(tab.path)},
            'position': {
                'line': offset + cursor.blockNumber(),
                'character': len(cursor.block().text()[:cursor.columnNumber()].encode('utf-16-le')) // 2,
            },
        }

    def _lsp_command(self, action, retry=12):
        tab = self.active_tab
        if tab is None:
            self._ide_message('LSP', 'Apri un file prima di usare i comandi LSP.')
            return
        if not lsp_server_for_path(tab.path):
            self._local_lsp_command(action, tab)
            return
        self._update_lsp_diagnostics()
        method = {
            'hover': 'textDocument/hover',
            'definition': 'textDocument/definition',
            'references': 'textDocument/references',
            'rename': 'textDocument/rename',
            'format': 'textDocument/formatting',
        }[action]
        if action == 'format':
            params = {'textDocument': {'uri': file_uri(tab.path)}, 'options': {'tabSize': 4, 'insertSpaces': True}}
        else:
            params = self._active_lsp_position()
            if params is None:
                self._ide_message('LSP', 'Metti il cursore dentro un blocco di codice.')
                return
            if action == 'references':
                params['context'] = {'includeDeclaration': True}
            elif action == 'rename':
                new_name, ok = self._ide_text('LSP rename', 'Nuovo nome:')
                if not ok or not new_name.strip():
                    return
                params['newName'] = new_name.strip()
        request_id = self.lsp_client.request(method, params)
        if request_id is None:
            if retry > 0:
                QTimer.singleShot(350, lambda: self._lsp_command(action, retry=retry - 1))
            else:
                self._ide_message('LSP', 'Server LSP non pronto.')
            return
        self.lsp_pending_requests[request_id] = (action, tab.path)

    def _lsp_missing_server_message(self, path):
        ext = Path(path).suffix.lower()
        servers = LSP_SERVERS_BY_EXT.get(ext, ())
        if not servers:
            return f'Nessun server LSP configurato per file {ext or "senza estensione"}.'
        return (
            f'Nessun server LSP trovato nel PATH per file {ext}.\n'
            f'Installa uno di questi: {", ".join(servers)}.'
        )

    def _active_symbol(self):
        tab = self.active_tab
        edit = QApplication.focusWidget()
        if tab is not None and not isinstance(edit, CodeEdit):
            edits = self.active_page.findChildren(CodeEdit)
            edit = edits[0] if edits else None
        if not isinstance(edit, CodeEdit):
            return ''
        return self._symbol_at_cursor(edit.textCursor())

    def _local_lsp_command(self, action, tab):
        if action == 'format':
            self._local_format(tab)
            return
        symbol = self._active_symbol()
        if not symbol:
            self._ide_message('LSP locale', 'Metti il cursore sopra un simbolo.')
            return
        if action == 'hover':
            definitions = self._local_definition_locations(symbol, limit=1)
            where = f'\nDefinizione probabile: {definitions[0][1]}:{definitions[0][2]}' if definitions else ''
            self._ide_message('LSP hover locale', f'Simbolo: {symbol}{where}')
        elif action == 'definition':
            self._show_lsp_locations('definition locale', [(path, line, text) for path, _rel, line, text in self._local_definition_locations(symbol)])
        elif action == 'references':
            self._show_lsp_locations('references locale', [(path, line, text) for path, _rel, line, text in self._local_reference_locations(symbol)])
        elif action == 'rename':
            new_name, ok = self._ide_text('LSP rename locale', f'Rinomina {symbol} in:')
            if ok and re.fullmatch(r'[A-Za-z_]\w*', new_name.strip()):
                self._local_rename_in_tab(tab, symbol, new_name.strip())

    def _local_definition_locations(self, symbol, limit=200):
        return definition_locations(self.project_root_path, symbol, limit)

    def _local_reference_locations(self, symbol, limit=200):
        return reference_locations(self.project_root_path, symbol, limit)

    def _local_rename_in_tab(self, tab, old, new):
        text = serialize_kant(tab.tree)
        new_text, count = re.subn(rf'\b{re.escape(old)}\b', new, text)
        if not count:
            self._ide_message('LSP rename locale', 'Nessuna occorrenza nel file aperto.')
            return
        self._apply_local_text(tab, new_text, f'Rinominate {count} occorrenze nel file aperto.')

    def _local_format(self, tab):
        text = serialize_kant(tab.tree)
        lines = [line.rstrip() for line in text.splitlines()]
        new_text = '\n'.join(lines).rstrip() + '\n'
        if new_text == text:
            self._ide_message('LSP format locale', 'Il file e gia pulito.')
            return
        self._apply_local_text(tab, new_text, 'Whitespace ripulito.')

    # [FN CATEGORY] _format_with_external_tool — runs black (falling back to `ruff format`) on the
    # active file's text over stdin/stdout, independent of any LSP server — _lsp_command('format')
    # only works when a language server for the file type is actually configured and running; this
    # works for any Python file regardless, using whichever of the two tools is on PATH.
    # [FN] _format_with_external_tool — formats the active Python file with black or ruff
    # [FN OPEN] _format_with_external_tool
    def _format_with_external_tool(self):
        tab = self.active_tab
        if tab is None:
            return
        if Path(tab.path).suffix.lower() != '.py':
            self._ide_message('Formatta', 'Formattazione black/ruff disponibile solo per file Python.')
            return
        text = serialize_kant(tab.tree)
        python_exe = self._active_python()
        if has_module(python_exe, 'black'):
            args = [python_exe, '-m', 'black', '-q', '-']
        elif has_module(python_exe, 'ruff'):
            args = [python_exe, '-m', 'ruff', 'format', '--stdin-filename', tab.path, '-']
        elif shutil.which('black'):
            args = ['black', '-q', '-']
        elif shutil.which('ruff'):
            args = ['ruff', 'format', '--stdin-filename', tab.path, '-']
        else:
            self._ide_message('Formatta', 'Ne black ne ruff sono installati (pip install black, oppure pip install ruff).')
            return
        try:
            result = subprocess.run(args, input=text, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            self._ide_message('Formatta', f'Errore avvio formatter: {e}')
            return
        if result.returncode:
            self._ide_message('Formatta', f'Formattazione fallita:\n{result.stderr or result.stdout}')
            return
        if result.stdout == text:
            self._ide_message('Formatta', 'Il file e gia formattato.')
            return
        self._apply_local_text(tab, result.stdout, 'Formattato con black/ruff.')
    # [FN CLOSED] _format_with_external_tool

    def _apply_local_text(self, tab, text, message):
        try:
            new_tree = parse_kant(text)
        except KantParseError as e:
            self._ide_message('LSP locale', f'Edit rifiutato: romperebbe i marker KANT.\n{e}')
            return
        tab.remember_undo_state()
        tab.tree = new_tree
        tab.mark_dirty()
        self._render_view(tab, tab.filter_uid)
        self._update_tab_title(tab)
        self._update_filename_label()
        self._ide_message('LSP locale', message)

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

    def _on_lsp_response(self, request_id, method, result):
        if method == 'textDocument/completion':
            edit = self.lsp_completion_requests.pop(request_id, None)
            if edit is not None:
                self._apply_completion_result(edit, result)
            return
        if request_id in self.lsp_hover_requests:
            edit, global_pos = self.lsp_hover_requests.pop(request_id)
            self._show_hover_tooltip(edit, global_pos, result)
            return
        action, path = self.lsp_pending_requests.pop(request_id, ('', ''))
        tab = self.open_tabs.get(path)
        if action == 'hover':
            text = self._lsp_hover_text(result)
            self._ide_message('LSP hover', text or 'Nessuna informazione.')
        elif action in ('definition', 'references'):
            self._show_lsp_locations(action, self._lsp_locations(result))
        elif action == 'format' and tab is not None:
            self._apply_lsp_text_edits(tab, result or [])
        elif action == 'rename' and tab is not None:
            self._apply_lsp_workspace_edits(result or {})

    # [FN CATEGORY] _request_completion — the CodeEdit.completion_provider callback: fires an async
    # LSP textDocument/completion at the cursor (or a local word-completion fallback when no server
    # is available for this file type), matching the on-demand hover/definition/etc. path's
    # local/LSP split. The document is re-synced first since typing doesn't otherwise push
    # didChange to the server until the next explicit LSP action.
    # [FN] _request_completion — asks for fresh completion candidates at edit's current cursor
    # [FN OPEN] _request_completion
    def _request_completion(self, edit):
        tab = getattr(edit, 'kant_tab', None) or self.active_tab
        if tab is None:
            return
        if not lsp_server_for_path(tab.path):
            self._local_completion(edit, tab)
            return
        self._update_lsp_diagnostics(tab)
        params = self._active_lsp_position(edit)
        if params is None:
            return
        request_id = self.lsp_client.request('textDocument/completion', params)
        if request_id is not None:
            self.lsp_completion_requests[request_id] = edit
    # [FN CLOSED] _request_completion

    def _apply_completion_result(self, edit, result):
        items = result.get('items', []) if isinstance(result, dict) else (result or [])
        labels, seen = [], set()
        for item in items:
            text = (item.get('insertText') or item.get('label') or '').strip() if isinstance(item, dict) else ''
            if text and text not in seen:
                seen.add(text)
                labels.append(text)
        try:
            edit.show_completions(labels)
        except RuntimeError:
            pass  # the widget (or its tab) was closed while the request was in flight

    # [FN] _local_completion — word-completion fallback for files with no LSP server available:
    # every identifier already typed anywhere in the open file is a candidate
    # [FN OPEN] _local_completion
    def _local_completion(self, edit, tab=None):
        tab = tab or getattr(edit, 'kant_tab', None) or self.active_tab
        if tab is None:
            return
        prefix = edit._text_under_cursor()
        if not prefix:
            edit.show_completions([])
            return
        words = set(re.findall(r'[A-Za-z_]\w*', serialize_kant(tab.tree)))
        words.discard(prefix)
        candidates = sorted(w for w in words if w.lower().startswith(prefix.lower()))
        edit.show_completions(candidates)
    # [FN CLOSED] _local_completion

    # [FN CATEGORY] quick-doc-on-hover — CodeEdit.hover_provider callback, mirroring PyCharm/VS
    # Code: resting the mouse on a symbol shows its documentation as a tooltip, no click needed.
    # Same LSP method (textDocument/hover) the menu-triggered "Hover" action already uses, but
    # tracked in its own request dict (lsp_hover_requests) since the result must be routed back to
    # a specific mouse position/CodeEdit instead of shown as a message dialog.
    # [FN] _request_hover — asks for documentation at the symbol under the mouse
    # [FN OPEN] _request_hover
    def _request_hover(self, edit, cursor, global_pos):
        tab = getattr(edit, 'kant_tab', None) or self.active_tab
        if tab is None:
            return
        symbol = self._symbol_at_cursor(cursor)
        if not symbol:
            return
        if not lsp_server_for_path(tab.path):
            self._local_hover(edit, global_pos, symbol)
            return
        self._update_lsp_diagnostics(tab)
        params = self._active_lsp_position(edit, cursor)
        if params is None:
            return
        request_id = self.lsp_client.request('textDocument/hover', params)
        if request_id is not None:
            self.lsp_hover_requests[request_id] = edit, global_pos
    # [FN CLOSED] _request_hover

    def _symbol_at_cursor(self, cursor):
        word_cursor = QTextCursor(cursor)
        word_cursor.select(QTextCursor.WordUnderCursor)
        symbol = word_cursor.selectedText().strip()
        return symbol if re.fullmatch(r'[A-Za-z_]\w*', symbol) else ''

    def _show_hover_tooltip(self, edit, global_pos, result):
        # the LSP response is markdown (real docstrings/type signatures from pyright/pylsp), so it
        # goes through the same markdown->HTML renderer the AI chat uses for fenced code/inline
        # code, instead of showing literal "**"/backtick syntax
        text = self._lsp_hover_text(result)
        try:
            if text:
                show_code_hover_popup(global_pos, _markdown_to_html(text))
            else:
                hide_code_hover_popup()
        except RuntimeError:
            pass  # the widget was closed while the request was in flight

    def _local_hover(self, edit, global_pos, symbol):
        definitions = self._local_definition_locations(symbol, limit=1)
        where = (
            f'<br><span style="color:{theme.DIM}">Definizione probabile: '
            f'{html_escape(definitions[0][1])}:{definitions[0][2]}</span>'
        ) if definitions else ''
        try:
            show_code_hover_popup(global_pos, f'<b>{html_escape(symbol)}</b>{where}')
        except RuntimeError:
            pass

    def _lsp_hover_text(self, result):
        contents = (result or {}).get('contents') if isinstance(result, dict) else result
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict):
            return contents.get('value') or contents.get('language') or ''
        if isinstance(contents, list):
            return '\n'.join(self._lsp_hover_text({'contents': c}) for c in contents)
        return ''

    def _lsp_locations(self, result):
        if not result:
            return []
        if isinstance(result, dict):
            result = [result]
        locations = []
        for loc in result:
            target_uri = loc.get('targetUri') or loc.get('uri')
            target_range = loc.get('targetSelectionRange') or loc.get('range') or {}
            start = target_range.get('start', {})
            if target_uri:
                locations.append((path_from_file_uri(target_uri), start.get('line', 0) + 1))
        return locations

    def _lsp_workspace_edits(self, result):
        edits = {}
        for uri, changes in result.get('changes', {}).items():
            edits.setdefault(path_from_file_uri(uri), []).extend(changes)
        for change in result.get('documentChanges', []):
            uri = change.get('textDocument', {}).get('uri')
            if uri:
                edits.setdefault(path_from_file_uri(uri), []).extend(change.get('edits', []))
        return edits

    def _apply_lsp_workspace_edits(self, result):
        grouped = self._lsp_workspace_edits(result)
        prepared = []
        try:
            for path, edits in grouped.items():
                absolute = os.path.abspath(path)
                tab = next((t for p, t in self.open_tabs.items() if os.path.normcase(os.path.abspath(p)) == os.path.normcase(absolute)), None)
                if tab is not None:
                    old_text = serialize_kant(tab.tree)
                else:
                    with open(absolute, 'r', encoding='utf-8', newline='') as f:
                        old_text = f.read()
                new_text = self._text_with_lsp_edits(old_text, edits)
                prepared.append((absolute, tab, new_text, parse_kant(new_text)))
        except (OSError, UnicodeDecodeError, KantParseError) as e:
            self._ide_message('LSP rename', f'Rename rifiutato: {e}')
            return

        try:
            for path, tab, new_text, _new_tree in prepared:
                if tab is None:
                    write_file_atomic(path, new_text)
        except OSError as e:
            self._ide_message('LSP rename', f'Rename non salvato: {e}')
            return

        for _path, tab, _new_text, new_tree in prepared:
            if tab is None:
                continue
            tab.remember_undo_state()
            tab.tree = new_tree
            tab.mark_dirty()
            self._render_view(tab, tab.filter_uid)
            self._update_tab_title(tab)
        if prepared:
            self._invalidate_xref()
            self._update_filename_label()
            self._update_lsp_diagnostics()

    def _show_lsp_locations(self, action, locations):
        self.results_view.clear()
        root = QTreeWidgetItem(self.results_view, [f'LSP {action}: {len(locations)} risultato/i'])
        for location in locations:
            path, line = location[:2]
            text = location[2] if len(location) > 2 else ''
            rel = os.path.relpath(path, self.project_root_path) if self.project_root_path else path
            item = QTreeWidgetItem(root, [f'{rel}:{line}' + (f': {text}' if text else '')])
            item.setData(0, ROLE_KIND, 'lsp-result')
            item.setData(0, ROLE_PATH, path)
            item.setData(0, ROLE_LINE, line)
            item.setData(0, ROLE_TEXT, text)
        if not locations:
            QTreeWidgetItem(root, ['Nessun risultato'])
        root.setExpanded(True)
        self._toggle_info_popup(self.results_view, force_open=True)

    def _offset_for_lsp_position(self, text, pos):
        lines = text.splitlines(keepends=True)
        if not lines:
            return 0
        line = pos.get('line', 0)
        if line >= len(lines):
            return len(text)
        units = max(0, pos.get('character', 0))
        prefix = lines[line].encode('utf-16-le')[:units * 2].decode('utf-16-le', errors='ignore')
        return sum(len(lines[i]) for i in range(line)) + len(prefix)

    def _text_with_lsp_edits(self, text, edits):
        for edit in sorted(edits, key=lambda e: (
            e.get('range', {}).get('start', {}).get('line', 0),
            e.get('range', {}).get('start', {}).get('character', 0),
        ), reverse=True):
            range_ = edit.get('range', {})
            start = self._offset_for_lsp_position(text, range_.get('start', {}))
            end = self._offset_for_lsp_position(text, range_.get('end', {}))
            text = text[:start] + edit.get('newText', '') + text[end:]
        return text

    def _apply_lsp_text_edits(self, tab, edits):
        if not edits:
            return
        text = self._text_with_lsp_edits(serialize_kant(tab.tree), edits)
        try:
            new_tree = parse_kant(text)
        except KantParseError as e:
            self._ide_message('LSP', f'Edit rifiutato: romperebbe i marker KANT.\n{e}')
            return
        tab.remember_undo_state()
        tab.tree = new_tree
        tab.mark_dirty()
        self._render_view(tab, tab.filter_uid)
        self._update_tab_title(tab)
        self._update_filename_label()
        self._update_lsp_diagnostics()

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
                w.deleteLater()
        tab.section_widgets.clear()
        tab.collapsibles.clear()
        if only_uid is None:
            self._build_node_widgets(tab, tab.tree, tab.view_layout, 0)
            self._ensure_empty_file_is_editable(tab)
            self._add_add_element_block(tab)
            return
        node = self._find_node_by_uid(tab.tree, only_uid)
        if node is None:
            self._build_node_widgets(tab, tab.tree, tab.view_layout, 0)
            self._ensure_empty_file_is_editable(tab)
            self._add_add_element_block(tab)
            return
        wrapper = Node(tag='ROOT', name='', open_raw=None, body=[node])
        self._build_node_widgets(tab, wrapper, tab.view_layout, 0)

    def _ensure_empty_file_is_editable(self, tab):
        if tab.view_container.findChildren(CodeEdit):
            return
        run = next((item for item in tab.tree.body if isinstance(item, Run)), None)
        if run is None:
            run = Run(lines=[''])
            tab.tree.body.append(run)
        edit = CodeEdit('\n'.join(run.lines))
        edit.kant_item = run
        edit.kant_tab = tab
        edit.textChanged.connect(lambda e=edit, it=run, t=tab: self._on_code_changed(t, e, it))
        edit.completion_provider = self._request_completion
        edit.hover_provider = self._request_hover
        edit.definition_provider = lambda _edit: self._lsp_command('definition')
        edit.rename_provider = lambda _edit: self._lsp_command('rename')
        edit.vim_action = self._vim_dispatch
        tab.view_layout.addWidget(edit)

    # [FN CATEGORY] _add_add_element_block — the "+" card at the bottom of the whole-file KANT
    # outline (scroll all the way down to find it). Deliberately a QPushButton, not a hand-rolled
    # clickable QFrame — free keyboard focus/Enter-activation and hover state instead of
    # reimplementing them.
    # [FN] _add_add_element_block — appends the "add a new element" card to a file's outline
    # [FN OPEN] _add_add_element_block
    def _add_add_element_block(self, tab):
        block = QPushButton('+  Aggiungi un elemento')
        block.setCursor(Qt.PointingHandCursor)
        block.setToolTip('Crea un nuovo modulo, classe, funzione o altro elemento in questo file')
        block.setMinimumHeight(56)
        block.setStyleSheet(
            f'QPushButton {{ background:{theme.CODE_BG}; color:{theme.DIM}; border:2px dashed {theme.BORDER}; '
            f'border-radius:10px; font-size:{theme.CODE_FONT_PT + 2}pt; font-weight:600; margin-top:8px; }} '
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
        tab.tree.body.append(node)
        tab.mark_dirty()
        self._invalidate_xref()
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
        groupings = load_groupings(self.project_root_path)
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

    def _build_node_widgets(self, tab, node, layout, depth):
        i = 0
        while i < len(node.body):
            item = node.body[i]
            if isinstance(item, Run):
                text = '\n'.join(item.lines)
                if not text.strip():
                    i += 1
                    continue
                edit = CodeEdit(text)
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
                        self._render_coordinated_leaf_group(tab, group, layout)
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
                    self._build_node_widgets(tab, item, section.content_layout, depth + 1)
                else:
                    leaf = LeafSection(item, show_header=depth > 0)
                    leaf.editMetadata.connect(lambda node, t=tab: self._edit_kant_metadata(t, node))
                    layout.addWidget(leaf)
                    tab.section_widgets[item.uid] = leaf
                    self._build_node_widgets(tab, item, leaf.content_layout, depth + 1)
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

    def _render_coordinated_leaf_group(self, tab, nodes, layout):
        panel = QWidget()
        panel.setObjectName('coordinatedConstants')
        panel.setStyleSheet(
            f'#coordinatedConstants {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; '
            f'border-radius:10px; padding:0; }}'
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(5, 3, 5, 3)
        panel_layout.setSpacing(1)
        for node in nodes:
            leaf = LeafSection(node, compact=True)
            panel_layout.addWidget(leaf)
            tab.section_widgets[node.uid] = leaf
            self._build_node_widgets(tab, node, leaf.content_layout, 0)
        layout.addWidget(panel)

    def _on_code_changed(self, tab, edit, item):
        tab.remember_undo_state(coalesce=True)
        item.lines = edit.toPlainText().split('\n')
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
    # project root on empty space); rename/delete only for an actual file or folder under the cursor
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
        rename_action = delete_action = git_diff_action = git_stage_action = git_unstage_action = None
        run_test_action = None
        test_name = None
        if item is not None and kind in ('file', 'plainfile', 'invalidfile', 'dir'):
            menu.addSeparator()
            rename_action = menu.addAction('Rinomina…')
            rename_action.setToolTip('Rinomina questo file o cartella')
            delete_action = menu.addAction('Elimina')
            delete_action.setToolTip('Sposta questo file o cartella nel cestino')
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
            groupings = load_groupings(self.project_root_path)
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
                add_member(self.project_root_path, matched_group.id, element_key)
                if self.view_mode == 'groups':
                    self._rebuild_tree()
    # [FN CLOSED] _show_tree_context_menu

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
