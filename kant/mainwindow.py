"""MainWindow: wires the project tree, section views, toolbar, git, LSP, panes."""
import os
import shutil
import subprocess
import time
from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QPointF, QProcess, QRect, Qt, QSettings, QSize, Signal, QTimer
from PySide6.QtGui import (
    QBrush, QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QPolygonF,
    QShortcut, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSizeGrip, QSplitter, QStackedWidget, QTabWidget, QToolButton,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from kant import theme
from kant.theme import set_theme
from kant.model import Run, Node, parse_kant, serialize_kant, read_top_level_label, KantParseError
from kant.fileio import write_file_atomic, detect_line_ending, is_safe_child_name
from kant.syntax import check_file_syntax, check_kant_markers, run_command_for_path
from kant.lsp import lsp_server_for_path, LspClient
from kant.gitutil import find_git_root, parse_git_status, git_status_map
from kant.widgets import (
    CodeEdit, TerminalPane, ClaudePane, CollapsibleSection, LeafSection,
    ProjectTree, make_star_icon, TitleBar, FileTab,
)


ROLE_KIND = Qt.UserRole
ROLE_PATH = Qt.UserRole + 1
ROLE_UID = Qt.UserRole + 2
ROLE_TREE = Qt.UserRole + 4
ROLE_LINE = Qt.UserRole + 5
ROLE_TEXT = Qt.UserRole + 6


# [FN CATEGORY] MainWindow — wires the project tree, the section view and the toolbar together;
# owns the currently-open file's parsed tree and dirty state
# [FN] MainWindow — the KANT Editor application window
# [FN OPEN] MainWindow
class MainWindow(QMainWindow):
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
        self.syntax_timer = QTimer(self)
        self.syntax_timer.setSingleShot(True)
        self.syntax_timer.timeout.connect(self._update_syntax_status)
        self.lsp_diagnostics = {}
        self.lsp_client = LspClient(self)
        self.lsp_client.diagnosticsChanged.connect(self._on_lsp_diagnostics)
        self.lsp_timer = QTimer(self)
        self.lsp_timer.setSingleShot(True)
        self.lsp_timer.timeout.connect(self._update_lsp_diagnostics)
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

        # deterministic real-time project tracking: QFileSystemWatcher fires on every add/remove/
        # rename inside a watched directory (event-driven, not a polling guess), debounced so a burst
        # of changes (git checkout, a script writing many files) triggers one rebuild, not dozens
        self.fs_watcher = QFileSystemWatcher(self)
        self.fs_watcher.directoryChanged.connect(self._on_fs_directory_changed)
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
        self.save_btn = self.title_bar.save_btn
        self.run_btn = self.title_bar.run_btn

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
    # [FN CLOSED] _setup_shortcuts

    def closeEvent(self, event):
        if not self._flush_all_tabs():
            event.ignore()
            return
        for proc in (self.terminal.process, self.claude_pane.process):
            if proc is not None:
                proc.kill()
                proc.waitForFinished(1000)
        self.lsp_client.shutdown()
        self.settings.setValue('windowGeometry', self.saveGeometry())
        self.settings.setValue('session/cleanExit', True)
        self.settings.sync()
        super().closeEvent(event)

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
        reply = QMessageBox.question(
            self, 'Ripristina sessione',
            'La sessione precedente si è interrotta improvvisamente. '
            'Vuoi riprendere a lavorare da dove avevi lasciato?',
        )
        if reply != QMessageBox.Yes:
            return
        self._open_project_folder(folder)
        file_path = self.settings.value('session/openFile')
        if file_path and os.path.isfile(file_path):
            self._open_file(file_path)
    # [FN CLOSED] _check_crash_recovery

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
        self.kant_map_label = QLabel('')
        self.kant_map_label.setWordWrap(True)
        self.kant_map_label.setFont(QFont('Consolas', theme.TREE_FONT_PT - 2))
        tree_panel_layout.addWidget(self.kant_map_label)
        tree_panel_layout.addWidget(self.tree, 1)

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
        self.claude_pane.finished.connect(self._refresh_and_validate_after_ai)

        self._style_io_tabs()
        self._update_io_tabs(None)
        self.terminal = TerminalPane(os.getcwd())
        self.main_splitter = QSplitter(Qt.Vertical)
        self.main_splitter.addWidget(view_panel)
        self.main_splitter.addWidget(self.terminal)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        saved_vertical_sizes = self.settings.value('verticalSplitterSizes')
        if saved_vertical_sizes and len(saved_vertical_sizes) == 2:
            self.main_splitter.setSizes([int(x) for x in saved_vertical_sizes])
        else:
            self.main_splitter.setSizes([740, 70])
        self.main_splitter.splitterMoved.connect(
            lambda *_: self.settings.setValue('verticalSplitterSizes', self.main_splitter.sizes())
        )

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(tree_panel)
        self.splitter.addWidget(self.main_splitter)
        self.splitter.addWidget(self.claude_pane)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        saved_sizes = self.settings.value('splitterSizes')
        if saved_sizes and len(saved_sizes) == 3:
            self.splitter.setSizes([int(x) for x in saved_sizes])
        else:
            self.splitter.setSizes([theme.TREE_MIN_WIDTH, 900, 460])
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
        self.title_bar.run_menu_action.setEnabled(has_tab)
        self.title_bar.find_menu_action.setEnabled(has_tab)
        self.title_bar.project_search_menu_action.setEnabled(bool(self.project_root_path))
        self.title_bar.project_replace_menu_action.setEnabled(bool(self.project_root_path))
        self.title_bar.git_refresh_menu_action.setEnabled(bool(self.git_root))
        self.title_bar.git_diff_menu_action.setEnabled(has_git_file)
        self.title_bar.git_stage_menu_action.setEnabled(has_git_file)
        self.title_bar.git_unstage_menu_action.setEnabled(has_git_file)

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

    def _iter_project_text_files(self):
        if not self.project_root_path:
            return
        for root, subdirs, files in os.walk(self.project_root_path):
            subdirs[:] = [d for d in subdirs if d not in theme.IGNORE_DIRS]
            for name in files:
                path = os.path.join(root, name)
                try:
                    if os.path.getsize(path) > theme.SEARCH_MAX_BYTES:
                        continue
                    data = Path(path).read_bytes()
                    if b'\0' in data:
                        continue
                    yield path, data.decode('utf-8')
                except (OSError, UnicodeDecodeError):
                    continue

    def _search_project(self):
        if not self.project_root_path:
            return
        needle, ok = QInputDialog.getText(self, 'Cerca nel progetto', 'Testo da cercare:')
        if not ok or not needle:
            return
        matches = []
        for path, text in self._iter_project_text_files():
            rel = os.path.relpath(path, self.project_root_path)
            for lineno, line in enumerate(text.splitlines(), 1):
                if needle in line:
                    matches.append((path, rel, lineno, line.strip()))
                    if len(matches) >= 200:
                        break
            if len(matches) >= 200:
                break
        self._show_search_results(needle, matches)

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
        if item.data(0, ROLE_KIND) != 'search-result':
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
        needle, ok = QInputDialog.getText(self, 'Sostituisci nel progetto', 'Testo da sostituire:')
        if not ok or not needle:
            return
        replacement, ok = QInputDialog.getText(self, 'Sostituisci nel progetto', 'Nuovo testo:')
        if not ok:
            return
        changes = []
        for path, text in self._iter_project_text_files():
            count = text.count(needle)
            if count:
                changes.append((path, text.replace(needle, replacement), count))
        total = sum(count for _path, _text, count in changes)
        if not total:
            self.terminal.write_info(f'\n# Sostituisci progetto: {needle!r}\nNessuna occorrenza\n')
            return
        reply = QMessageBox.question(
            self,
            'Sostituisci nel progetto',
            f'Sostituire {total} occorrenze in {len(changes)} file?',
        )
        if reply != QMessageBox.Yes:
            return
        if not self._flush_all_tabs():
            return
        changed_paths = set()
        for path, text, _count in changes:
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
    def _rebuild_tree(self):
        self.tree.clear()
        if not self.project_root_path:
            return
        self._refresh_git_status()
        if self.view_mode == 'code':
            self._build_project_tree(self.tree.invisibleRootItem(), self.project_root_path)
        else:
            self._build_plain_project_tree(self.tree.invisibleRootItem(), self.project_root_path)
        self.tree._rewrap_labels()
    # [FN CLOSED] _rebuild_tree

    # [FN CATEGORY] _watch_project_tree — (re)registers every directory under the project root with
    # QFileSystemWatcher. Directories, not files: a directory's own change event fires for any
    # add/remove/rename of an entry inside it, which is exactly "the project structure changed" —
    # watching every individual file too would multiply the watch count for no extra signal here.
    # [FN] _watch_project_tree — points the filesystem watcher at the current project's directories
    # [FN OPEN] _watch_project_tree
    def _watch_project_tree(self):
        watched = self.fs_watcher.directories()
        if watched:
            self.fs_watcher.removePaths(watched)
        if not self.project_root_path:
            return
        dirs = [self.project_root_path]
        for root, subdirs, _files in os.walk(self.project_root_path):
            subdirs[:] = [d for d in subdirs if d not in theme.IGNORE_DIRS]
            dirs.extend(os.path.join(root, d) for d in subdirs)
        if dirs:
            self.fs_watcher.addPaths(dirs)
    # [FN CLOSED] _watch_project_tree

    def _on_fs_directory_changed(self, _path):
        self.fs_refresh_timer.start(400)  # debounce a burst of filesystem events into one rebuild

    # [FN CATEGORY] _refresh_after_fs_change — rebuilds the tree and the KANT-map badge from the
    # current filesystem state, then re-registers the watcher (new subdirectories need watching too,
    # deleted ones can no longer be watched) — this is the actual "real-time representation" update
    # [FN] _refresh_after_fs_change — reacts to an external filesystem change under the project
    # [FN OPEN] _refresh_after_fs_change
    def _refresh_after_fs_change(self):
        if not self.project_root_path:
            return
        self._rebuild_tree()
        self._check_kant_map(self.project_root_path)
        self._watch_project_tree()
    # [FN CLOSED] _refresh_after_fs_change

    # [FN] _refresh_and_validate_after_ai — refreshes the project tree and reports KANT validation after an AI run
    # [FN OPEN] _refresh_and_validate_after_ai
    def _refresh_and_validate_after_ai(self):
        self._refresh_after_fs_change()
        if not getattr(self.claude_pane, 'validate_after_finish', False):
            return
        self.claude_pane.validate_after_finish = False
        result = self._validate_kant_project()
        if result:
            self.claude_pane.write_info('\n' + result + '\n')
    # [FN CLOSED] _refresh_and_validate_after_ai

    def _build_io_tabs(self):
        self.incoming_view = QPlainTextEdit()
        self.incoming_view.setReadOnly(True)
        self.incoming_view.setFont(QFont('Consolas', theme.CODE_FONT_PT))

        self.outgoing_view = QPlainTextEdit()
        self.outgoing_view.setReadOnly(True)
        self.outgoing_view.setFont(QFont('Consolas', theme.CODE_FONT_PT))

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
        self.results_label_btn = QPushButton('RISULTATI')
        self.results_label_btn.clicked.connect(lambda: self._toggle_info_popup(self.results_view))
        for btn in (self.incoming_label_btn, self.outgoing_label_btn, self.results_label_btn):
            label_layout.addWidget(btn)
        label_layout.addStretch(1)
        layout.addWidget(label_bar)

        panel.setFixedHeight(42)
        return panel

    def _style_io_tabs(self):
        for view in (self.incoming_view, self.outgoing_view):
            view.setStyleSheet(f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:none; padding:10px;')
        self.results_view.setStyleSheet(f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:none; padding:6px;')
        self.info_popup.setStyleSheet(f'background:{theme.CODE_BG}; border-top:1px solid {theme.BORDER}; border-bottom:1px solid {theme.BORDER};')
        self.io_tabs.setStyleSheet(f'background:{theme.PANEL}; border-top:1px solid {theme.BORDER};')
        for btn in (self.incoming_label_btn, self.outgoing_label_btn, self.results_label_btn):
            btn.setStyleSheet(theme.BUTTON_STYLE + 'QPushButton { padding:4px 12px; }')

    def _toggle_info_popup(self, widget, force_open=False):
        if self.info_popup.currentWidget() is widget and self.info_popup.isVisible() and not force_open:
            self.info_popup.setVisible(False)
            self.io_tabs.setFixedHeight(42)
            return
        self.info_popup.setCurrentWidget(widget)
        self.info_popup.setVisible(True)
        self.io_tabs.setFixedHeight(150)

    # [FN CATEGORY] _update_io_tabs — looks up a section by uid and shows its INCOMING/OUTGOING data
    # (per the KANT convention, only meaningful for FN/TST) in the two tabs above the terminal
    # [FN] _update_io_tabs — refreshes the Incoming/Outgoing tabs for the selected section
    # [FN OPEN] _update_io_tabs
    def _update_io_tabs(self, uid):
        tab = self.active_tab
        node = self._find_node_by_uid(tab.tree, uid) if (uid is not None and tab is not None) else None
        self.incoming_view.setPlainText(node.incoming if node and node.incoming else '—')
        self.outgoing_view.setPlainText(node.outgoing if node and node.outgoing else '—')
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

    def _ide_choice(self, title, message, choices):
        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.setMinimumWidth(460)
        dialog.setStyleSheet(
            f'QDialog {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; }} '
            f'QLabel {{ color:{theme.TEXT}; }}'
        )

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        title_label = QLabel(title)
        title_label.setFont(QFont('Consolas', theme.TREE_FONT_PT + 2, QFont.DemiBold))
        title_label.setStyleSheet(f'color:{theme.WARN};')
        layout.addWidget(title_label)

        message_label = QLabel(message)
        message_label.setWordWrap(True)
        message_label.setStyleSheet(f'color:{theme.TEXT};')
        layout.addWidget(message_label)

        result = {'value': None}
        buttons = QHBoxLayout()
        buttons.addStretch(1)

        def choose(value):
            result['value'] = value
            dialog.accept()

        for label, value in choices:
            btn = QPushButton(label)
            btn.setStyleSheet(theme.BUTTON_STYLE)
            btn.clicked.connect(lambda _checked=False, v=value: choose(v))
            buttons.addWidget(btn)

        layout.addLayout(buttons)
        return result['value'] if dialog.exec() == QDialog.Accepted else None

    def _ide_yes_no(self, title, message):
        return self._ide_choice(title, message, [('No', False), ('Si', True)]) is True

    def _choose_ai_agent(self):
        return self._ide_choice(
            'Motore AI',
            'Con quale agente vuoi applicare /kant-code-map su tutto il progetto?',
            [('Annulla', None), ('Claude Code', 'claude'), ('Codex', 'codex')],
        )

    def _open_project_folder(self, path):
        path = os.path.abspath(path)
        if self.project_root_path and os.path.abspath(self.project_root_path) != path:
            if not self._close_all_tabs(flush=True):
                return
            self.settings.remove('session/openFile')
        self.project_root_path = path
        self._remember_folder(path)
        self.settings.setValue('session/openFolder', path)
        self.terminal.set_cwd(path)
        self.claude_pane.set_cwd(path)
        self._rebuild_tree()
        self._check_kant_map(path)
        if self.kant_map_path is None:
            if self._has_any_kant_tags(path):
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
        self.git_root = find_git_root(self.project_root_path)
        self.git_status = git_status_map(self.git_root)
        self._update_action_buttons()

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
    # file /kant-code-map writes) and reflects whether one exists in the label above the project tree
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
    # ponytail: full project rescan on every save, no incremental update — fine at the scale this
    # tool targets (same tradeoff already accepted by read_top_level_label); revisit if it's ever slow.
    # [FN] _sync_kant_map — rewrites KANT_<project>.md from the current on-disk KANT structure
    # [FN OPEN] _sync_kant_map
    def _sync_kant_map(self):
        if not self.project_root_path:
            return
        project_name = os.path.basename(self.project_root_path)
        path = self.kant_map_path or os.path.join(self.project_root_path, f'KANT_{project_name}.md')

        entries = []
        for file_path in self._iter_kant_tagged_files(self.project_root_path):
            label = read_top_level_label(file_path)
            if label is None:
                continue
            tag, desc, _tree, top_node = label
            entries.append((os.path.relpath(file_path, self.project_root_path), tag, desc, top_node))
        entries.sort(key=lambda e: e[0])

        lines = [f'# KANT Code Map - {project_name}', '', '## Struttura', '', '```']
        for rel_path, tag, desc, top_node in entries:
            lines.append(self._format_kant_map_line(0, tag, rel_path.replace(os.sep, '/'), desc))
            self._append_map_children(lines, top_node, depth=1)
        lines.append('```')
        lines.append('')

        try:
            write_file_atomic(path, '\n'.join(lines))
        except OSError:
            return
        self.kant_map_path = path
        self._update_kant_map_label()
    # [FN CLOSED] _sync_kant_map

    # [FN] _format_kant_map_line — formats one KANT map row with skill-compatible depth prefixes
    # [FN OPEN] _format_kant_map_line
    def _format_kant_map_line(self, depth, tag, name, desc):
        prefix = '-' * depth + (' ' if depth else '')
        label = f'[{tag} {name}]' if depth == 0 else f'[{tag}] {name}'
        return f'{prefix}{label} \u2014 {desc or name}'
    # [FN CLOSED] _format_kant_map_line

    # [FN] _append_map_children — appends nested KANT nodes to the generated KANT map
    # [FN OPEN] _append_map_children
    def _append_map_children(self, lines, node, depth):
        for child in node.body:
            if not isinstance(child, Node):
                continue
            lines.append(self._format_kant_map_line(depth, child.tag, child.name, child.desc))
            self._append_map_children(lines, child, depth + 1)
    # [FN CLOSED] _append_map_children

    def _iter_kant_tagged_files(self, root):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in theme.IGNORE_DIRS]
            for name in filenames:
                yield os.path.join(dirpath, name)

    # [FN CATEGORY] _has_any_kant_tags — short-circuits on the first tagged file found, so an
    # already-tagged project (the common case) answers almost instantly; only a genuinely virgin
    # project pays for the full walk. Distinguishes "tags exist, just the map file is missing" (fast
    # deterministic _sync_kant_map covers it) from "no KANT convention anywhere yet" (needs
    # /kant-code-map to actually read the code and decide what to tag, not a mechanical rebuild).
    # [FN] _has_any_kant_tags — whether any file under root already carries a KANT tag
    # [FN OPEN] _has_any_kant_tags
    def _has_any_kant_tags(self, root):
        return any(read_top_level_label(f) is not None for f in self._iter_kant_tagged_files(root))
    # [FN CLOSED] _has_any_kant_tags

    # [FN] _validate_kant_project — validates the generated KANT map and marker structure after AI runs
    # [FN OPEN] _validate_kant_project
    def _validate_kant_project(self):
        if not self.project_root_path:
            return ''
        self._check_kant_map(self.project_root_path)
        errors = []
        tagged = []
        checked_markers = 0
        map_text = ''

        if self.kant_map_path is None:
            errors.append('manca KANT_*.md nella radice del progetto')
        else:
            try:
                map_text = Path(self.kant_map_path).read_text(encoding='utf-8')
            except OSError as e:
                errors.append(f'{os.path.basename(self.kant_map_path)} non leggibile: {e}')

        for file_path in self._iter_kant_tagged_files(self.project_root_path):
            rel_path = os.path.relpath(file_path, self.project_root_path).replace(os.sep, '/')
            try:
                text = Path(file_path).read_text(encoding='utf-8')
            except UnicodeDecodeError:
                continue
            except OSError as e:
                errors.append(f'{rel_path}: non leggibile: {e}')
                continue
            if ' OPEN]' in text or ' CLOSED]' in text:
                checked_markers += 1
                result = check_kant_markers(text)
                if not result['ok']:
                    errors.append(f"{rel_path}:{result.get('line', 1)} {result.get('message', 'marker KANT non valido')}")
            label = read_top_level_label(file_path)
            if label is not None:
                tagged.append((rel_path, label[0]))

        if map_text:
            missing = [rel_path for rel_path, tag in tagged if f'[{tag} {rel_path}]' not in map_text]
            if missing:
                sample = ', '.join(missing[:5])
                extra = f' (+{len(missing) - 5})' if len(missing) > 5 else ''
                errors.append(f'KANT map non coerente: mancano {sample}{extra}')

        if errors:
            sample = '\n'.join(f'- {err}' for err in errors[:8])
            extra = f'\n- ... altri {len(errors) - 8} errori' if len(errors) > 8 else ''
            return f'# KANT verifica: ERRORI\n{sample}{extra}'
        map_name = os.path.basename(self.kant_map_path) if self.kant_map_path else 'KANT_*.md'
        return f'# KANT verifica: OK ({map_name}, {checked_markers} file con marker)'
    # [FN CLOSED] _validate_kant_project

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
        )
    # [FN CLOSED] _launch_kant_code_map

    def _build_project_tree(self, parent_item, dir_path):
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
                self._build_project_tree(dir_item, entry.path)
            else:
                label = read_top_level_label(entry.path)
                if label is None:
                    continue  # no KANT tags — only convention-tagged files show up in the tree
                tag, desc, tree, top_node = label
                file_item = QTreeWidgetItem(parent_item)
                file_item.setData(0, ROLE_KIND, 'file')
                file_item.setData(0, ROLE_PATH, entry.path)
                file_item.setData(0, ROLE_TREE, tree)
                self.tree.setItemWidget(
                    file_item, 0, self._tree_label(tag, desc, bold=True, git_status=self._git_status_for_path(entry.path))
                )
                # start from the top node's own children, not the node itself — it's already
                # shown as this file item's own label, showing it again would duplicate it
                self._build_outline_items(file_item, top_node, entry.path, tree)

    def _build_outline_items(self, parent_item, node, path, tree):
        for child in node.body:
            if not isinstance(child, Node):
                continue
            item = QTreeWidgetItem(parent_item)
            item.setData(0, ROLE_KIND, 'section')
            item.setData(0, ROLE_UID, child.uid)
            item.setData(0, ROLE_PATH, path)
            item.setData(0, ROLE_TREE, tree)
            self.tree.setItemWidget(item, 0, self._tree_label(child.tag, child.desc or child.name))
            self._build_outline_items(item, child, path, tree)

    def _tree_label(self, tag, text, bold=False, git_status=''):
        color = theme.TAG_COLORS.get(tag, theme.TEXT)
        bg = theme.TAG_BACKGROUNDS.get(tag, '#eef2f7')
        weight = '700' if bold else '400'
        git_html = (
            f' <span style="color:{theme.WARN}; font-weight:700">[{html_escape(git_status)}]</span>'
            if git_status else ''
        )
        lbl = QLabel(
            f'<span style="color:{color}; background-color:{bg}; font-weight:700; '
            f'padding:2px 6px; border-radius:5px">[{tag}]</span> '
            f'<span style="font-weight:{weight}">{html_escape(text)}</span>{git_html}'
        )
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        lbl.setMargin(6)
        lbl.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:4px 8px;')
        lbl.setWordWrap(True)  # long labels wrap instead of overflowing the column
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

    def _plain_file_label(self, name, git_status=''):
        git_html = (
            f' <span style="color:{theme.WARN}; font-weight:700">[{html_escape(git_status)}]</span>'
            if git_status else ''
        )
        lbl = QLabel(html_escape(name) + git_html)
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        lbl.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:4px 8px;')
        lbl.setWordWrap(True)
        return lbl

    def _on_tree_item_clicked(self, item, _column):
        kind = item.data(0, ROLE_KIND)
        if kind == 'file':
            self._open_file(item.data(0, ROLE_PATH), item.data(0, ROLE_TREE))
        elif kind == 'plainfile':
            self._open_file(item.data(0, ROLE_PATH))
        elif kind == 'section':
            path = item.data(0, ROLE_PATH)
            self._open_file(path, item.data(0, ROLE_TREE))
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
        self.save_btn.setEnabled(tab is not None)
        self.run_btn.setEnabled(tab is not None)
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
        if tab.path in self.open_tabs:
            del self.open_tabs[tab.path]
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
    def _open_file(self, path, preparsed_tree=None):
        existing = self.open_tabs.get(path)
        if existing is not None:
            self.tabs.setCurrentWidget(existing)
            return True
        if preparsed_tree is not None:
            tree = preparsed_tree
        else:
            try:
                with open(path, 'r', encoding='utf-8', newline='') as f:
                    tree = parse_kant(f.read())
            except UnicodeDecodeError:
                QMessageBox.warning(self, 'File non testuale', f'{os.path.basename(path)} non e un file UTF-8 apribile.')
                return False
            except OSError as e:
                QMessageBox.critical(self, 'Apri file', f'Impossibile aprire {os.path.basename(path)}: {e}')
                return False
            except KantParseError as e:
                QMessageBox.critical(self, 'Marcatori KANT non validi', f'{os.path.basename(path)}: {e}')
                return False
        tab = FileTab(path, tree, detect_line_ending(path))
        tab.dirtyChanged.connect(lambda t=tab: self._on_tab_dirty_changed(t))
        tab.saveFailed.connect(lambda msg, t=tab: self._on_tab_save_failed(t, msg))
        tab.saved.connect(self._sync_kant_map)
        self.open_tabs[path] = tab
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
        result = check_file_syntax(tab.path, serialize_kant(tab.tree))
        lsp = lsp_server_for_path(tab.path)
        lsp_text = self._lsp_status_text(tab.path, lsp)
        if result['ok']:
            self.syntax_label.setText(f"OK {result.get('message', 'Sintassi OK')}{lsp_text}")
            self.syntax_label.setStyleSheet(f'color:{theme.OK}; font-weight:700;')
        else:
            self.syntax_label.setText(f"ERR riga {result.get('line', 1)}: {result['message']}{lsp_text}")
            self.syntax_label.setStyleSheet(f'color:{theme.TAG_COLORS["TST"]}; font-weight:700;')

    def _lsp_status_text(self, path, server):
        if not server:
            return ' | LSP -'
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
            QMessageBox.information(self, 'Run', 'Nessun comando run configurato per questo tipo di file.')
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
                    section.set_expanded(depth < 1)
                    layout.addWidget(section)
                    tab.collapsibles.append(section)
                    tab.section_widgets[item.uid] = section
                    self._build_node_widgets(tab, item, section.content_layout, depth + 1)
                else:
                    leaf = LeafSection(item)
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
        item.lines = edit.toPlainText().split('\n')
        tab.mark_dirty()

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
        if kind in ('file', 'plainfile', 'section'):
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
        rename_action = delete_action = git_diff_action = git_stage_action = git_unstage_action = None
        if item is not None and kind in ('file', 'plainfile', 'dir'):
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

    def _run_git(self, args):
        if not self.git_root:
            return None
        try:
            return subprocess.run(
                ['git', '-C', self.git_root, *args],
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self.terminal.write_info(f'\n# git errore: {e}\n')
            return None

    def _git_diff_file(self, path):
        rel = self._git_relpath(path)
        if not rel:
            return
        result = self._run_git(['diff', '--', rel])
        if result is None:
            return
        text = result.stdout.strip()
        if not text:
            cached = self._run_git(['diff', '--cached', '--', rel])
            text = cached.stdout.strip() if cached else ''
        self.terminal.write_info(f'\n# git diff -- {rel}\n{text or "Nessuna differenza"}\n')

    def _git_stage_file(self, path, staged):
        rel = self._git_relpath(path)
        if not rel:
            return
        args = ['add', '--', rel] if staged else ['restore', '--staged', '--', rel]
        result = self._run_git(args)
        if result is None:
            return
        if result.returncode:
            self.terminal.write_info(f'\n# git {"stage" if staged else "unstage"} {rel}\n{result.stderr or result.stdout}\n')
            return
        self._refresh_after_fs_change()
        self.terminal.write_info(f'\n# git {"stage" if staged else "unstage"} {rel}: OK\n')

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

    def _create_new_file(self, target_dir):
        if not target_dir:
            return
        name, ok = QInputDialog.getText(self, 'Nuovo file', 'Nome del file:')
        name = name.strip()
        if not ok or not name:
            return
        if not is_safe_child_name(name):
            QMessageBox.warning(self, 'Nuovo file', 'Usa solo un nome file, senza percorsi.')
            return
        path = os.path.join(target_dir, name)
        if os.path.exists(path):
            QMessageBox.warning(self, 'Nuovo file', 'Esiste già un file o una cartella con questo nome.')
            return
        try:
            with open(path, 'w', encoding='utf-8', newline=''):
                pass
        except OSError as e:
            QMessageBox.critical(self, 'Nuovo file', f'Impossibile creare il file: {e}')
            return
        self._refresh_after_fs_change()
        self._open_file(path)

    def _create_new_folder(self, target_dir):
        if not target_dir:
            return
        name, ok = QInputDialog.getText(self, 'Nuova cartella', 'Nome della cartella:')
        name = name.strip()
        if not ok or not name:
            return
        if not is_safe_child_name(name):
            QMessageBox.warning(self, 'Nuova cartella', 'Usa solo un nome cartella, senza percorsi.')
            return
        path = os.path.join(target_dir, name)
        try:
            os.makedirs(path, exist_ok=False)
        except OSError as e:
            QMessageBox.critical(self, 'Nuova cartella', f'Impossibile creare la cartella: {e}')
            return
        self._refresh_after_fs_change()

    # [FN CATEGORY] _rename_tree_item — renames a file or folder on disk; any open tab(s) for that
    # path (or nested under it, if it's a folder) are flushed first and retargeted to the new path
    # rather than left pointing at a path that no longer exists
    # [FN] _rename_tree_item — renames the right-clicked file or folder
    # [FN OPEN] _rename_tree_item
    def _rename_tree_item(self, item, kind):
        old_path = item.data(0, ROLE_PATH)
        if not old_path:
            return
        old_name = os.path.basename(old_path)
        new_name, ok = QInputDialog.getText(self, 'Rinomina', 'Nuovo nome:', text=old_name)
        new_name = new_name.strip()
        if not ok or not new_name or new_name == old_name:
            return
        if not is_safe_child_name(new_name):
            QMessageBox.warning(self, 'Rinomina', 'Usa solo un nome, senza percorsi.')
            return
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        if os.path.exists(new_path):
            QMessageBox.warning(self, 'Rinomina', 'Esiste già un file o una cartella con questo nome.')
            return
        is_dir = kind == 'dir'
        affected = [t for p, t in self.open_tabs.items() if p == old_path or (is_dir and p.startswith(old_path + os.sep))]
        for t in affected:
            if not t.flush_pending_save():
                return
        try:
            os.rename(old_path, new_path)
        except OSError as e:
            QMessageBox.critical(self, 'Rinomina', f'Impossibile rinominare: {e}')
            return
        for t in affected:
            self._retarget_tab(t, new_path + t.path[len(old_path):])
        self._refresh_after_fs_change()
    # [FN CLOSED] _rename_tree_item

    def _retarget_tab(self, tab, new_path):
        del self.open_tabs[tab.path]
        tab.path = new_path
        self.open_tabs[new_path] = tab
        idx = self.tabs.indexOf(tab)
        if idx != -1:
            self.tabs.setTabToolTip(idx, new_path)
            self._update_tab_title(tab)
        if tab is self.active_tab:
            self._update_filename_label()

    # [FN] _trash_target — returns a unique local trash path for a deleted project item
    # [FN OPEN] _trash_target
    def _trash_target(self, path):
        root = self.project_root_path or os.path.dirname(path)
        trash_dir = os.path.join(root, '.kant-trash')
        os.makedirs(trash_dir, exist_ok=True)
        base = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.path.basename(path)}"
        target = os.path.join(trash_dir, base)
        suffix = 1
        while os.path.exists(target):
            target = os.path.join(trash_dir, f'{base}-{suffix}')
            suffix += 1
        return target
    # [FN CLOSED] _trash_target

    # [FN] _move_to_trash — moves a file or folder into the project's reversible trash
    # [FN OPEN] _move_to_trash
    def _move_to_trash(self, path):
        target = self._trash_target(path)
        shutil.move(path, target)
        return target
    # [FN CLOSED] _move_to_trash

    # [FN] _delete_tree_item — moves the right-clicked file or folder to .kant-trash
    # [FN OPEN] _delete_tree_item
    def _delete_tree_item(self, item, kind):
        path = item.data(0, ROLE_PATH)
        if not path:
            return
        is_dir = kind == 'dir'
        reply = QMessageBox.question(
            self, 'Elimina',
            f'Eliminare {"la cartella" if is_dir else "il file"} "{os.path.basename(path)}"? '
            'Sara spostato nel cestino locale .kant-trash.',
        )
        if reply != QMessageBox.Yes:
            return
        affected = [t for p, t in self.open_tabs.items() if p == path or (is_dir and p.startswith(path + os.sep))]
        for t in affected:
            idx = self.tabs.indexOf(t)
            if idx != -1:
                self._close_tab(idx, flush=False)
        try:
            trashed = self._move_to_trash(path)
        except OSError as e:
            QMessageBox.critical(self, 'Elimina', f'Impossibile eliminare: {e}')
            return
        self.terminal.write_info(f'\n# cestino KANT\nNel cestino: {trashed}\nPer ripristinare: {path}\n')
        self._refresh_after_fs_change()
    # [FN CLOSED] _delete_tree_item
# [FN CLOSED] MainWindow
