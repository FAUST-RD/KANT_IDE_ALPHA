"""MainWindow: wires the project tree, section views, toolbar, git, LSP, panes."""
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QSettings, Signal, QTimer
from PySide6.QtGui import (
    QColor, QFont, QKeySequence, QShortcut, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QPushButton,
    QSizeGrip, QSplitter, QStackedWidget, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from kant import theme
from kant.theme import set_theme
from kant.model import Run, Node, parse_kant, serialize_kant, read_top_level_label, read_top_level_label_result, KantParseError
from kant.fileio import file_fingerprint, write_file_atomic, detect_line_ending
from kant.syntax import check_file_syntax, run_command_for_path
from kant.xref import build_xref
from kant.lsp import LSP_SERVERS_BY_EXT, file_uri, path_from_file_uri, lsp_server_for_path, LspClient
from kant.gitutil import find_git_root, git_status_map
from kant.dialogs import IdeDialogsMixin
from kant.projectops import (
    build_kant_map, definition_locations, has_any_kant_tags, iter_kant_tagged_files,
    reference_locations, scan_project_replace, search_project, validate_kant_project,
)
from kant.workspace import ROLE_PATH, WorkspaceMixin, discard_snapshot, rollback_snapshot
from kant.widgets import (
    CodeEdit, TerminalPane, ClaudePane, CollapsibleSection, LeafSection,
    ProjectTree, make_star_icon, TitleBar, FileTab, XrefMapDialog,
)


ROLE_KIND = Qt.UserRole
# ROLE_PATH (Qt.UserRole + 1) lives in kant/workspace.py — workspace's tree-item rename/delete
# code needs it too, and mainwindow imports workspace (not the other way around)
ROLE_UID = Qt.UserRole + 2
ROLE_LINE = Qt.UserRole + 5
ROLE_TEXT = Qt.UserRole + 6
ROLE_KEY = Qt.UserRole + 7   # xref element key '<rel_path>::<uid>', on Incoming/Outgoing list items


# [FN CATEGORY] MainWindow — wires the project tree, the section view and the toolbar together;
# owns the currently-open file's parsed tree and dirty state
# [FN] MainWindow — the KANT Editor application window
# [FN OPEN] MainWindow
class MainWindow(IdeDialogsMixin, WorkspaceMixin, QMainWindow):
    backgroundFinished = Signal(object, object, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('KANT Editor')
        self.setWindowIcon(make_star_icon())
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.resize(1500, 950)
        self.setFont(QFont('Consolas', 10))

        self.open_tabs = {}  # path -> FileTab, every currently open file
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
        self._closing = False
        self._background = ThreadPoolExecutor(max_workers=2, thread_name_prefix='kant')
        self.backgroundFinished.connect(self._finish_background)
        self._git_refresh_pending = False
        self._ai_snapshot = None
        self._map_sync_generation = 0
        self.syntax_timer = QTimer(self)
        self.syntax_timer.setSingleShot(True)
        self.syntax_timer.timeout.connect(self._update_syntax_status)
        self.lsp_diagnostics = {}
        self.lsp_pending_requests = {}
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

        self.stack = QStackedWidget()
        shell_layout.addWidget(self.stack, 1)
        self.setCentralWidget(self.shell)
        self.stack.addWidget(self._build_welcome_page())  # index 0: shown until a folder is opened
        self.stack.addWidget(self._build_main_page())      # index 1: project tree + view
        self.stack.setCurrentIndex(0)
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
        QShortcut(QKeySequence('Ctrl+Shift+F'), self, self._search_project)
        QShortcut(QKeySequence('Ctrl+Shift+H'), self, self._replace_project)
        QShortcut(QKeySequence.Undo, self, self._undo_file)
        QShortcut(QKeySequence.Redo, self, self._redo_file)
        QShortcut(QKeySequence('F12'), self, lambda: self._lsp_command('definition'))
        QShortcut(QKeySequence('Shift+F12'), self, lambda: self._lsp_command('references'))
    # [FN CLOSED] _setup_shortcuts

    def closeEvent(self, event):
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
            [('Decidi più tardi', None), ('Ripristina originali', 'rollback'), ('Tieni i file attuali', 'discard')],
        )
        if choice is None:
            return
        if choice == 'rollback':
            try:
                rollback_snapshot(project, snapshot, theme.IGNORE_DIRS | {'.kant-trash'})
            except OSError as error:
                self._ide_message('Revisione AI in sospeso', f'Impossibile ripristinare: {error}')
                return
        discard_snapshot(snapshot)
        self._clear_ai_snapshot_marker()
    # [FN CLOSED] _check_pending_ai_snapshot

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'size_grip'):
            self.size_grip.move(self.width() - 22, self.height() - 22)
            self.size_grip.raise_()

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

    def _apply_theme(self):
        self.setStyleSheet(theme.APP_STYLE)
        self.shell.setStyleSheet(f'#appShell {{ border:1px solid {theme.BORDER}; background:{theme.BG}; }}')
        self.title_bar.apply_style()
        if hasattr(self, 'welcome_title'):
            self.welcome_title.setStyleSheet(f'color:{theme.ACCENT}; letter-spacing:3px;')
            self.welcome_desc.setStyleSheet(f'color:{theme.DIM};')
            self.recent_title.setStyleSheet(f'color:{theme.DIM};')
        if hasattr(self, 'tree'):
            self.tree.setStyleSheet(
                f'QTreeWidget {{ background:{theme.PANEL}; color:{theme.TEXT}; border:none; border-right:1px solid {theme.BORDER}; padding:14px 10px; }} '
                f'QTreeWidget::item {{ padding:3px 0; }} '
                f'QTreeWidget::item:selected {{ background:{"#1e293b" if self.night_mode else "#eef4ff"}; color:{theme.ACCENT}; border-radius:6px; }}'
            )
            self._rebuild_tree()
        if hasattr(self, 'tabs'):
            self.terminal.setStyleSheet(
                f'background:{theme.CODE_BG}; color:{theme.TEXT}; border-top:1px solid {theme.BORDER}; padding:12px;'
            )
            self.claude_pane.apply_style()
            self._style_io_tabs()
            self._style_view_mode_bar()
            self._style_find_bar()
            self._style_status_bar()
            self._update_kant_map_label()
            for tab in self.open_tabs.values():
                tab.apply_style()
                self._render_view(tab, tab.filter_uid)
            if self.active_tab is not None:
                self._update_io_tabs(self.active_tab.filter_uid)
        self._refresh_recent_folders()

    def _build_welcome_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(20)

        self.welcome_title = QLabel('KANT Editor')
        title = self.welcome_title
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont('Consolas', 28, QFont.DemiBold))
        title.setStyleSheet(f'color:{theme.ACCENT}; letter-spacing:3px;')
        layout.addWidget(title)

        self.welcome_desc = QLabel(
            'Apri la cartella di un progetto per esplorarlo: i file vengono etichettati secondo i '
            'marcatori KANT ([TAG OPEN] Nome / [TAG CLOSED] Nome) e mostrati suddivisi in sezioni '
            'pieghevoli secondo la gerarchia MOD > CLS > FN, ecc.'
        )
        desc = self.welcome_desc
        desc.setAlignment(Qt.AlignCenter)
        desc.setWordWrap(True)
        desc.setMaximumWidth(560)
        desc.setStyleSheet(f'color:{theme.DIM};')
        desc.setFont(QFont('Consolas', 14))
        desc_row = QHBoxLayout()
        desc_row.setAlignment(Qt.AlignCenter)
        desc_row.addWidget(desc)
        layout.addLayout(desc_row)

        open_btn = QPushButton('Apri cartella…')
        open_btn.setFont(QFont('Consolas', 18))
        open_btn.setStyleSheet(theme.BUTTON_STYLE + 'QPushButton { padding:16px 32px; }')
        open_btn.clicked.connect(self._open_folder)
        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignCenter)
        btn_row.addWidget(open_btn)
        layout.addLayout(btn_row)

        self.recent_title = QLabel('Cartelle recenti')
        recent_title = self.recent_title
        recent_title.setAlignment(Qt.AlignCenter)
        recent_title.setFont(QFont('Consolas', 11, QFont.DemiBold))
        recent_title.setStyleSheet(f'color:{theme.DIM};')
        layout.addWidget(recent_title)

        self.recent_wrap = QWidget()
        self.recent_layout = QVBoxLayout(self.recent_wrap)
        self.recent_layout.setContentsMargins(0, 0, 0, 0)
        self.recent_layout.setSpacing(6)
        layout.addWidget(self.recent_wrap)
        self._refresh_recent_folders()
        return page

    def _build_main_page(self):
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.tree = ProjectTree()
        self.tree.setHeaderHidden(True)
        self.tree.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        self.tree.setMinimumWidth(160)
        self.tree.setIndentation(22)
        self.tree.setUniformRowHeights(False)  # rows can grow taller once labels wrap
        self.tree.setStyleSheet(
            f'QTreeWidget {{ background:{theme.PANEL}; color:{theme.TEXT}; border:none; border-right:1px solid {theme.BORDER}; padding:14px 10px; }} '
            f'QTreeWidget::item {{ padding:3px 0; }} '
            f'QTreeWidget::item:selected {{ background:#eef4ff; color:{theme.ACCENT}; border-radius:6px; }}'
        )
        self.tree.itemClicked.connect(self._on_tree_item_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        self.setAcceptDrops(True)

        tree_panel = QWidget()
        tree_panel_layout = QVBoxLayout(tree_panel)
        tree_panel_layout.setContentsMargins(0, 0, 0, 0)
        tree_panel_layout.setSpacing(0)
        tree_panel_layout.addWidget(self._build_view_mode_bar())
        self.map_label_btn = QPushButton('MAPPA')
        self.map_label_btn.clicked.connect(self._open_xref_window)
        tree_panel_layout.addWidget(self.map_label_btn)
        tree_panel_layout.addWidget(self.tree, 1)
        self.kant_map_label = QLabel('')
        self.kant_map_label.setWordWrap(True)
        self.kant_map_label.setFont(QFont('Consolas', theme.TREE_FONT_PT - 2))
        tree_panel_layout.addWidget(self.kant_map_label)

        # each open file is a tab (FileTab) with its own scroll area/view/dirty state; switching
        # tabs is just switching the QTabWidget's current index, nothing to rebuild
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_active_tab_changed)

        view_panel = QWidget()
        view_panel_layout = QVBoxLayout(view_panel)
        view_panel_layout.setContentsMargins(0, 0, 0, 0)
        view_panel_layout.setSpacing(0)
        view_panel_layout.addWidget(self._build_find_bar())
        view_panel_layout.addWidget(self.tabs, 1)
        self.io_tabs = self._build_io_tabs()
        view_panel_layout.addWidget(self.io_tabs)

        self.claude_pane = ClaudePane(os.getcwd())
        self.claude_pane.before_run = self._prepare_ai_snapshot
        self.claude_pane.finished.connect(self._finish_ai_review)
        self.claude_pane.finished.connect(self._refresh_and_validate_after_ai)

        self._style_io_tabs()
        self._update_io_tabs(None)
        self.terminal = TerminalPane(os.getcwd())
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
        self.main_splitter.addWidget(self.terminal)
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
        root_layout.addWidget(self.splitter, 1)
        self._build_status_bar()
        return central

    # [FN CATEGORY] _build_status_bar — cursor line/column, encoding and line-ending indicator for
    # the currently focused code block; QMainWindow's own status bar area, below the central widget
    # [FN] _build_status_bar — builds the bottom status bar
    # [FN OPEN] _build_status_bar
    def _build_status_bar(self):
        bar = self.statusBar()
        self.cursor_pos_label = QLabel('')
        self.encoding_label = QLabel('')
        bar.addPermanentWidget(self.cursor_pos_label)
        bar.addPermanentWidget(self.encoding_label)
        self._style_status_bar()
    # [FN CLOSED] _build_status_bar

    def _style_status_bar(self):
        self.statusBar().setStyleSheet(
            f'QStatusBar {{ background:{theme.PANEL}; color:{theme.DIM}; border-top:1px solid {theme.BORDER}; }}'
        )

    def _on_focus_changed(self, _old, new):
        if isinstance(new, CodeEdit):
            self._update_cursor_position_label(new)
            if not hasattr(new, '_status_bar_wired'):
                new._status_bar_wired = True
                new.cursorPositionChanged.connect(lambda e=new: self._update_cursor_position_label(e))

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
        self.code_view_btn.clicked.connect(lambda: self._set_view_mode('code'))

        self.file_view_btn = QPushButton('File')
        self.file_view_btn.setCheckable(True)
        self.file_view_btn.clicked.connect(lambda: self._set_view_mode('file'))

        self.view_mode_group = QButtonGroup(self)
        self.view_mode_group.setExclusive(True)
        self.view_mode_group.addButton(self.code_view_btn)
        self.view_mode_group.addButton(self.file_view_btn)

        add_label('Vista')
        layout.addWidget(self.code_view_btn)
        layout.addWidget(self.file_view_btn)
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
            btn.setStyleSheet(checked_style if btn in (self.code_view_btn, self.file_view_btn) else theme.BUTTON_STYLE)

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
        self.title_bar.project_search_menu_action.setEnabled(bool(self.project_root_path))
        self.title_bar.project_replace_menu_action.setEnabled(bool(self.project_root_path))
        self.title_bar.git_refresh_menu_action.setEnabled(bool(self.git_root))
        self.title_bar.git_diff_menu_action.setEnabled(has_git_file)
        self.title_bar.git_stage_menu_action.setEnabled(has_git_file)
        self.title_bar.git_unstage_menu_action.setEnabled(has_git_file)
        for action in (
            self.title_bar.lsp_hover_menu_action,
            self.title_bar.lsp_definition_menu_action,
            self.title_bar.lsp_references_menu_action,
            self.title_bar.lsp_rename_menu_action,
            self.title_bar.lsp_format_menu_action,
        ):
            action.setEnabled(has_tab)

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

        prev_btn = QPushButton('▲')
        prev_btn.setFixedWidth(32)
        prev_btn.clicked.connect(self._find_prev)
        layout.addWidget(prev_btn)

        next_btn = QPushButton('▼')
        next_btn.setFixedWidth(32)
        next_btn.clicked.connect(self._find_next)
        layout.addWidget(next_btn)

        self.find_status = QLabel('')
        self.find_status.setStyleSheet(f'color:{theme.DIM};')
        layout.addWidget(self.find_status)

        close_btn = QPushButton('×')
        close_btn.setFixedWidth(32)
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
        for btn in self.find_bar.findChildren(QPushButton):
            btn.setStyleSheet(theme.BUTTON_STYLE)

    def _show_find_bar(self):
        self.find_bar.setVisible(True)
        self.find_input.setFocus()
        self.find_input.selectAll()

    def _hide_find_bar(self):
        self.find_bar.setVisible(False)

    # [FN CATEGORY] _find_in_view — searches every CodeEdit in the current center view in document
    # order, continuing from the currently focused widget's cursor and wrapping across widget
    # boundaries and back to the start; reuses QPlainTextEdit's own .find() rather than hand-rolling
    # text search
    # [FN] _find_in_view — moves to the next/previous match across all visible code blocks
    # [FN OPEN] _find_in_view
    def _find_in_view(self, forward=True):
        text = self.find_input.text()
        tab = self.active_tab
        if not text or tab is None:
            return
        widgets = tab.view_container.findChildren(CodeEdit)
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
                tab.scroll_area.ensureWidgetVisible(widget, 50, 80)
                self.find_status.setText('')
                return
        self.find_status.setText('Nessun risultato')
    # [FN CLOSED] _find_in_view

    def _find_next(self):
        self._find_in_view(forward=True)

    def _find_prev(self):
        self._find_in_view(forward=False)

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
        if item.data(0, ROLE_KIND) not in ('search-result', 'validation-result', 'lsp-result', 'diagnostic-result'):
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
        self.results_view.itemDoubleClicked.connect(self._open_result_item)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

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
        self.incoming_label_btn.clicked.connect(lambda: self._toggle_info_popup(self.incoming_view))
        self.outgoing_label_btn = QPushButton('OUTGOING')
        self.outgoing_label_btn.clicked.connect(lambda: self._toggle_info_popup(self.outgoing_view))
        for btn in (self.incoming_label_btn, self.outgoing_label_btn):
            label_layout.addWidget(btn)
        label_layout.addStretch(1)
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
        self.results_view.setStyleSheet(f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:none; padding:6px;')
        self.info_popup.setStyleSheet(f'background:{theme.CODE_BG}; border-top:1px solid {theme.BORDER}; border-bottom:1px solid {theme.BORDER};')
        self.io_tabs.setStyleSheet(f'background:{theme.PANEL}; border-top:1px solid {theme.BORDER};')
        for btn in (self.incoming_label_btn, self.outgoing_label_btn, self.map_label_btn):
            btn.setStyleSheet(theme.BUTTON_STYLE + 'QPushButton { padding:4px 12px; }')
        if self.map_dialog is not None:
            self.map_dialog.apply_style()

    def _toggle_info_popup(self, widget, force_open=False):
        if self.info_popup.currentWidget() is widget and self.info_popup.isVisible() and not force_open:
            self.info_popup.setVisible(False)
            self.io_tabs.setFixedHeight(42)
            return
        self.info_popup.setCurrentWidget(widget)
        self.info_popup.setVisible(True)
        self.io_tabs.setFixedHeight(200)

    # [FN CATEGORY] _open_xref_window — MAPPA opens the cross-reference graph in a dialog internal to
    # the IDE (parented to the main window, floating over the editor — not a strip in the coding pane
    # nor a separate OS window), kept as a single reused instance and raised if already open. Rebuilds
    # the graph from the (cache-backed) xref on every open so it reflects the current code, and wires
    # double-click-a-node back to _navigate_to_element so the map doubles as a jump-to launcher.
    # [FN] _open_xref_window — opens/raises the internal cross-reference map dialog
    # [FN OPEN] _open_xref_window
    def _open_xref_window(self):
        if self.map_dialog is None:
            self.map_dialog = XrefMapDialog(self)
            self.map_dialog.nodeActivated.connect(self._navigate_to_element)
        self.map_dialog.apply_style()
        project_name = os.path.basename(self.project_root_path) if self.project_root_path else ''
        self.map_dialog.set_graph(self._get_xref(), project_name, self.project_root_path or '')
        self.map_dialog.show()
        self.map_dialog.raise_()
        self.map_dialog.activateWindow()
    # [FN CLOSED] _open_xref_window

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

    # [FN CATEGORY] _update_io_tabs — looks the selected element up in the deterministic
    # cross-reference graph (kant/xref.py) and fills two navigable lists: INCOMING = who references
    # it and from which file (what comes in, and from where), OUTGOING = what it references and in
    # which file (what goes out, and to where). Works for every tagged element — functions,
    # constants, classes, vars — not just FN/TST, since the xref is name-based. Each row stores its
    # target key so a double-click jumps there. Nothing is read from a comment, so it can never
    # drift from the code.
    # [FN] _update_io_tabs — refreshes the Incoming/Outgoing lists for the selected section
    # [FN OPEN] _update_io_tabs
    def _update_io_tabs(self, uid):
        self._last_io_uid = uid
        tab = self.active_tab
        node = self._find_node_by_uid(tab.tree, uid) if (uid is not None and tab is not None) else None
        self.incoming_view.clear()
        self.outgoing_view.clear()
        if node is None or not self.project_root_path:
            return
        xref = self._get_xref()
        rel = os.path.relpath(tab.path, self.project_root_path).replace(os.sep, '/')
        element = xref.get(f'{rel}::{node.uid}')
        if element is None:
            return

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

        fill(self.incoming_view, element.incoming, '←')   # ← comes from
        fill(self.outgoing_view, element.outgoing, '→')   # → goes to
    # [FN CLOSED] _update_io_tabs


    # ---- project tree -------------------------------------------------

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
            btn = QPushButton(path)
            btn.setMaximumWidth(720)
            btn.setStyleSheet(theme.BUTTON_STYLE)
            btn.clicked.connect(lambda _checked=False, p=path: self._open_project_folder(p))
            self.recent_layout.addWidget(btn)

    def _open_folder(self):
        path = QFileDialog.getExistingDirectory(self, 'Apri cartella')
        if not path:
            return
        self._open_project_folder(path)

    # [FN CATEGORY] _go_back_to_welcome — flushes any pending edit (nothing is discarded — autosave
    # means switching screens is always safe) and returns to the folder-picker/recent-folders screen
    # [FN] _go_back_to_welcome — the titlebar back arrow's action
    # [FN OPEN] _go_back_to_welcome
    def _go_back_to_welcome(self):
        if not self._flush_all_tabs():
            return
        self._refresh_recent_folders()
        self.stack.setCurrentIndex(0)
    # [FN CLOSED] _go_back_to_welcome

    def _choose_ai_agent(self):
        return self._ide_choice(
            'Motore AI',
            'Con quale agente vuoi applicare /kant-code-map su tutto il progetto?',
            [('Annulla', None), ('Claude Code', 'claude'), ('Codex', 'codex')],
        )

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
                    agent = self._choose_ai_agent()
                    if agent:
                        self._launch_kant_code_map(agent)
        self._watch_project_tree()
        self.stack.setCurrentIndex(1)

    def _refresh_git_status(self):
        if self._git_refresh_pending or not self.project_root_path:
            return
        project_root = self.project_root_path
        self._git_refresh_pending = True

        def read_status():
            git_root = find_git_root(project_root)
            return git_root, git_status_map(git_root)

        def apply_status(result, error):
            self._git_refresh_pending = False
            if self.project_root_path != project_root:
                self._refresh_git_status()
                return
            if error:
                return
            self.git_root, self.git_status = result
            self._update_action_buttons()
            self._rebuild_tree(refresh_git=False)

        self._run_background(read_status, apply_status)

    def _git_status_for_path(self, path):
        if not self.git_root:
            return ''
        rel = os.path.relpath(path, self.git_root)
        return self.git_status.get(rel, '')

    def _git_status_for_dir(self, path):
        if not self.git_root:
            return ''
        rel = os.path.relpath(path, self.git_root)
        prefix = '' if rel == '.' else rel + os.sep
        return 'M' if any(p.startswith(prefix) for p in self.git_status) else ''

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
            self.kant_map_label.setStyleSheet(f'color:{theme.OK}; background:{theme.PANEL}; padding:6px 10px;')
        else:
            self.kant_map_label.setText('✗ Nessuna KANT_*.md — genera con /kant-code-map')
            self.kant_map_label.setStyleSheet(f'color:{theme.DIM}; background:{theme.PANEL}; padding:6px 10px;')

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
        result, errors, visual_errors = validate_kant_project(self.project_root_path, self.kant_map_path)

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
    def _launch_kant_code_map(self, agent):
        self.claude_pane.run_prompt(
            'Applica la convenzione KANT all\'intero progetto, come il comando /kant-code-map: '
            'aggiungi i commenti tag KANT sopra ogni elemento del codice sorgente e crea o aggiorna '
            'KANT_<nome-progetto>.md alla radice del progetto.',
            extra_skills=('kant-code-map',),
            agent=agent,
            auto_permissions_once=True,
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
                self.tree.setItemWidget(file_item, 0, self._invalid_file_label(os.path.basename(file_path), error))
                continue
            if label is None:
                continue  # no KANT tags — only convention-tagged files show up in the tree
            tag, desc, _tree, top_node = label
            file_item = QTreeWidgetItem(parent_item)
            file_item.setData(0, ROLE_KIND, 'file')
            file_item.setData(0, ROLE_PATH, file_path)
            self.tree.setItemWidget(
                file_item, 0, self._tree_label(
                    tag, desc, bold=True, git_status=self._git_status_for_path(file_path),
                    detail=top_node.category_desc,
                )
            )
            # start from the top node's own children, not the node itself — it's already
            # shown as this file item's own label, showing it again would duplicate it
            self._build_outline_items(file_item, top_node, file_path)

    def _build_outline_items(self, parent_item, node, path):
        for child in node.body:
            if not isinstance(child, Node):
                continue
            item = QTreeWidgetItem(parent_item)
            item.setData(0, ROLE_KIND, 'section')
            item.setData(0, ROLE_UID, child.uid)
            item.setData(0, ROLE_PATH, path)
            self.tree.setItemWidget(item, 0, self._tree_label(child.tag, child.desc or child.name, detail=child.category_desc))
            self._build_outline_items(item, child, path)

    def _tree_label(self, tag, text, bold=False, git_status='', detail=''):
        color = theme.TAG_COLORS.get(tag, theme.TEXT)
        bg = theme.TAG_BACKGROUNDS.get(tag, '#eef2f7')
        weight = '700' if bold else '400'
        git_html = (
            f' <span style="color:{theme.WARN}; font-weight:700">[{html_escape(git_status)}]</span>'
            if git_status else ''
        )
        detail_html = f'<br><span style="color:{theme.DIM}">{html_escape(detail)}</span>' if detail else ''
        lbl = QLabel(
            f'<span style="color:{color}; background-color:{bg}; font-weight:700; '
            f'padding:2px 6px; border-radius:5px">[{tag}]</span> '
            f'<span style="font-weight:{weight}">{html_escape(text)}</span>{git_html}{detail_html}'
        )
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        lbl.setMargin(6)
        lbl.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:4px 8px;')
        lbl.setWordWrap(True)  # long labels wrap instead of overflowing the column
        lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
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
                self.tree.setItemWidget(file_item, 0, self._plain_file_label(entry.name, self._git_status_for_path(entry.path)))
    # [FN CLOSED] _build_plain_project_tree

    def _invalid_file_label(self, name, error):
        lbl = QLabel(
            f'<span style="color:{theme.TAG_COLORS["TST"]}; font-weight:700">[ERR]</span> '
            f'{html_escape(name)} <span style="color:{theme.DIM}">{html_escape(str(error))}</span>'
        )
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        lbl.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:4px 8px;')
        lbl.setWordWrap(True)
        lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        return lbl

    def _plain_file_label(self, name, git_status=''):
        git_html = (
            f' <span style="color:{theme.WARN}; font-weight:700">[{html_escape(git_status)}]</span>'
            if git_status else ''
        )
        lbl = QLabel(html_escape(name) + git_html)
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        lbl.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:4px 8px;')
        lbl.setWordWrap(True)
        lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
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
        elif kind == 'section':
            path = item.data(0, ROLE_PATH)
            self._open_file(path)
            tab = self.open_tabs.get(path)
            if tab is None:
                return
            uid = item.data(0, ROLE_UID)
            self._render_view(tab, uid)
            self._update_io_tabs(uid)

    # ---- tabs ------------------------------------------------------------

    @property
    def active_tab(self):
        return self.tabs.currentWidget()

    def _on_active_tab_changed(self, _index):
        tab = self.active_tab
        self._update_action_buttons()
        self._update_filename_label()
        self._update_syntax_status()
        self._update_lsp_diagnostics()
        self._update_io_tabs(tab.filter_uid if tab else None)
        self.encoding_label.setText('UTF-8' if tab else '')
        self.cursor_pos_label.setText('')

    # [FN CATEGORY] _close_tab — closes one tab; flushes its pending autosave first unless the file
    # is about to be deleted out from under it (flush=False), in which case that would just recreate
    # the file we're deleting
    # [FN] _close_tab — closes the tab at the given index
    # [FN OPEN] _close_tab
    def _close_tab(self, index, flush=True):
        tab = self.tabs.widget(index)
        if tab is None:
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
        self.tabs.removeTab(index)
        tab.deleteLater()
        return True
    # [FN CLOSED] _close_tab

    def _close_active_tab(self):
        tab = self.active_tab
        if tab is not None:
            self._close_tab(self.tabs.indexOf(tab))

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

    def _update_tab_title(self, tab):
        idx = self.tabs.indexOf(tab)
        if idx == -1:
            return
        name = os.path.basename(tab.path)
        self.tabs.setTabText(idx, (name + ' ●') if tab.dirty else name)

    def _on_tab_dirty_changed(self, tab):
        self._update_tab_title(tab)
        self._update_action_buttons()
        if tab is self.active_tab:
            self._update_filename_label()
            self.syntax_timer.start(800)

    def _on_tab_save_failed(self, tab, message):
        self.terminal.write_info(f'\n# save errore: {tab.path}\n{message}\n')
        if tab is self.active_tab:
            self.syntax_label.setText(f'SAVE ERR: {message}')
            self.syntax_label.setStyleSheet(f'color:{theme.TAG_COLORS["TST"]}; font-weight:700;')

    # ---- file open/save ------------------------------------------------

    # [FN CATEGORY] _open_file — opens a file as a new tab, or just switches to it if it's already
    # open (an already-open tab's live edits are never discarded/re-read from disk by re-clicking it)
    # [FN] _open_file — opens or activates a file's tab
    # [FN OPEN] _open_file
    def _open_file(self, path):
        existing = self.open_tabs.get(path)
        if existing is not None:
            self.tabs.setCurrentWidget(existing)
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
        idx = self.tabs.addTab(tab, os.path.basename(path))
        self.tabs.setTabToolTip(idx, path)
        self._render_view(tab)
        self.tabs.setCurrentWidget(tab)
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
        text = tab.path if tab else ''
        if tab and tab.dirty:
            text += '  ●'
        self.filename_label.setText(text)
        self.filename_label.setStyleSheet(f'color:{theme.ACCENT if (tab and tab.dirty) else theme.DIM};')
        self.title_bar.set_save_state(tab is not None, tab.dirty if tab else False)

    def _update_syntax_status(self):
        tab = self.active_tab
        if tab is None:
            self.syntax_label.setText('')
            return
        path = tab.path
        text = serialize_kant(tab.tree)
        self.syntax_label.setText('Controllo sintassi...')
        self._run_background(
            lambda: check_file_syntax(path, text),
            lambda result, error: self._apply_syntax_status(path, text, result, error),
        )

    def _apply_syntax_status(self, path, text, result, error):
        tab = self.active_tab
        if tab is None or tab.path != path or serialize_kant(tab.tree) != text:
            return
        if error:
            result = {'ok': False, 'line': 1, 'message': str(error)}
        lsp = lsp_server_for_path(path)
        lsp_text = self._lsp_status_text(tab.path, lsp)
        if result['ok']:
            self.syntax_label.setText(f"OK {result.get('message', 'Sintassi OK')}{lsp_text}")
            self.syntax_label.setStyleSheet(f'color:{theme.OK}; font-weight:700;')
        else:
            self.syntax_label.setText(f"ERR riga {result.get('line', 1)}: {result['message']}{lsp_text}")
            self.syntax_label.setStyleSheet(f'color:{theme.TAG_COLORS["TST"]}; font-weight:700;')

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

    def _update_lsp_diagnostics(self):
        tab = self.active_tab
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

    def _active_lsp_position(self):
        tab = self.active_tab
        if tab is None:
            return None
        edit = QApplication.focusWidget()
        if not isinstance(edit, CodeEdit):
            edits = tab.view_container.findChildren(CodeEdit)
            edit = edits[0] if edits else None
        if edit is None:
            return None
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
            edits = tab.view_container.findChildren(CodeEdit)
            edit = edits[0] if edits else None
        if not isinstance(edit, CodeEdit):
            return ''
        cursor = edit.textCursor()
        cursor.select(QTextCursor.WordUnderCursor)
        symbol = cursor.selectedText().strip()
        return symbol if re.fullmatch(r'[A-Za-z_]\w*', symbol) else ''

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

    def _on_lsp_response(self, request_id, method, result):
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
        command = run_command_for_path(tab.path)
        if command is None:
            self._ide_message('Run', 'Nessun comando run configurato per questo tipo di file.')
            return
        self.terminal.run_command(command, os.path.dirname(tab.path) or None)

    # ---- section view ---------------------------------------------------

    def _render_view(self, tab, only_uid=None):
        tab.filter_uid = only_uid
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
            return
        node = self._find_node_by_uid(tab.tree, only_uid)
        if node is None:
            self._build_node_widgets(tab, tab.tree, tab.view_layout, 0)
            self._ensure_empty_file_is_editable(tab)
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
        edit.textChanged.connect(lambda e=edit, it=run, t=tab: self._on_code_changed(t, e, it))
        tab.view_layout.addWidget(edit)

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
                edit.textChanged.connect(lambda e=edit, it=item, t=tab: self._on_code_changed(t, e, it))
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
                if has_children:
                    section = CollapsibleSection(item)
                    section.editMetadata.connect(lambda node, t=tab: self._edit_kant_metadata(t, node))
                    section.set_expanded(depth < 1)
                    layout.addWidget(section)
                    tab.collapsibles.append(section)
                    tab.section_widgets[item.uid] = section
                    self._build_node_widgets(tab, item, section.content_layout, depth + 1)
                else:
                    leaf = LeafSection(item)
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
        panel_layout.setContentsMargins(12, 10, 12, 10)
        panel_layout.setSpacing(6)
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
        tag, ok = self._ide_text('KANT metadata', 'Tag:', text=node.tag)
        if not ok:
            return
        tag = tag.strip().upper()
        name, ok = self._ide_text('KANT metadata', 'Nome tecnico:', text=node.name)
        if not ok:
            return
        name = name.strip()
        desc, ok = self._ide_text('KANT metadata', 'Descrizione breve:', text=node.desc or node.name)
        if not ok or not tag or not name:
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

    # ---- new file / rename / delete (project tree context menu) ---------

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
        new_file_action = menu.addAction('Nuovo file…')
        new_folder_action = menu.addAction('Nuova cartella…')
        restore_action = menu.addAction('Ripristina dal cestino...') if self._restore_candidates() else None
        rename_action = delete_action = git_diff_action = git_stage_action = git_unstage_action = None
        if item is not None and kind in ('file', 'plainfile', 'invalidfile', 'dir'):
            menu.addSeparator()
            rename_action = menu.addAction('Rinomina…')
            delete_action = menu.addAction('Elimina')
        if item is not None and kind in ('file', 'plainfile') and self.git_root:
            menu.addSeparator()
            git_diff_action = menu.addAction('Git diff')
            git_stage_action = menu.addAction('Git stage')
            git_unstage_action = menu.addAction('Git unstage')

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
    # [FN CLOSED] _show_tree_context_menu

    def _git_relpath(self, path):
        if not self.git_root or not path:
            return None
        return os.path.relpath(path, self.git_root)

    def _run_git(self, args, git_root=None):
        git_root = git_root or self.git_root
        if not git_root:
            return None
        return subprocess.run(
            ['git', '-C', git_root, *args],
            capture_output=True,
            text=True,
            timeout=8,
        )

    def _git_diff_file(self, path):
        rel = self._git_relpath(path)
        if not rel:
            return
        git_root = self.git_root
        def diff():
            result = self._run_git(['diff', '--', rel], git_root)
            text = result.stdout.strip() if result else ''
            if not text:
                cached = self._run_git(['diff', '--cached', '--', rel], git_root)
                text = cached.stdout.strip() if cached else ''
            return text

        self._run_background(
            diff,
            lambda text, error: self.terminal.write_info(
                f'\n# git diff -- {rel}\n{("Errore: " + str(error)) if error else (text or "Nessuna differenza")}\n'
            ),
        )

    def _git_stage_file(self, path, staged):
        rel = self._git_relpath(path)
        if not rel:
            return
        args = ['add', '--', rel] if staged else ['restore', '--staged', '--', rel]
        action = 'stage' if staged else 'unstage'
        git_root = self.git_root

        def done(result, error):
            if error or result is None or result.returncode:
                message = str(error) if error else ((result.stderr or result.stdout) if result else 'Git non disponibile')
                self.terminal.write_info(f'\n# git {action} {rel}\n{message}\n')
                return
            self._refresh_after_fs_change()
            self.terminal.write_info(f'\n# git {action} {rel}: OK\n')

        self._run_background(lambda: self._run_git(args, git_root), done)

    def _active_file_path(self):
        tab = self.active_tab
        return tab.path if tab is not None else None

    def _git_refresh(self):
        self._refresh_after_fs_change()
        self.terminal.write_info('\n# Git refresh: OK\n')

    def _git_diff_active_file(self):
        path = self._active_file_path()
        if path:
            self._git_diff_file(path)

    def _git_stage_active_file(self):
        path = self._active_file_path()
        if path:
            self._git_stage_file(path, staged=True)

    def _git_unstage_active_file(self):
        path = self._active_file_path()
        if path:
            self._git_stage_file(path, staged=False)
# [FN CLOSED] MainWindow
