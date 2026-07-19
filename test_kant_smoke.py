import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import shiboken6

from PySide6.QtCore import Qt, QEvent, QPointF, QSettings
from PySide6.QtGui import QImage, QKeyEvent, QKeySequence, QMouseEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QGraphicsItem, QLabel, QListWidget, QMenu, QMessageBox, QTabBar, QToolButton,
    QTreeWidget, QTreeWidgetItem,
)

import kant_editor
from kant import theme
from kant import mainwindow as kant_mainwindow_module
from kant import widgets as kant_widgets_module
from kant.mainwindow import MainWindow, ROLE_KIND, ROLE_PATH, ROLE_ORDER, ROLE_UID, ROLE_TEXT, ROLE_LINE, ROLE_KEY
from kant.lsp import file_uri, LspClient
from kant.model import Node, Run, build_new_element_node, parse_kant, serialize_kant, read_top_level_label_result
from kant.pyenv import (
    dependency_file, detect_venvs, has_module, interpreter_label, interpreter_version,
    is_python_majority_project, load_interpreter, save_interpreter,
)
from kant.xref import build_xref, XrefElement
from kant.widgets import (
    ClaudePane, CollapsibleSection, DiffHighlighter, FileTab, LeafSection, RecentFolderCard,
    _AiReviewCard, _agent_command, _markdown_to_html, _normalize_ai_text, CodeEdit, MODEL_DEFAULT,
    _code_hover_popup_instance, make_app_icon, make_app_pixmap, set_vim_mode, vim_mode_enabled,
    compress_attached_image, convert_attached_document,
)
from kant.mappa import (
    MIN_NODE_GAP, XrefMapDialog, XrefMapView, _force_layout_positions, _element_degree, _element_size,
    _position_settings_key, migrate_position_keys,
)
from kant import gitops as kant_gitops_module
from kant.gitops import GitPanelDialog
from kant.workspace import (
    apply_ai_review, build_ai_review, create_snapshot, discard_snapshot, normalize_missing_ids,
    rollback_snapshot, render_review_text, safe_project_path,
)
from kant.permission_mcp import handle_message
from kant.groupings import load_groupings, new_grouping, save_groupings
from kant.syntax import audit_kant_headers, check_kant_markers
from kant import skeleton
from kant.projectops import _canonical_map_text, build_kant_map, validate_kant_project


class LabelStub:
    def setText(self, *_args):
        pass

    def setStyleSheet(self, *_args):
        pass

    def write_info(self, *_args):
        pass


class _StatusButtonStub:
    """Stands in for the real QPushButton status-bar label in tests that don't need a live window."""

    def __init__(self):
        self._text = ''
        self._visible = False

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    def setVisible(self, visible):
        self._visible = visible

    def isVisible(self):
        return self._visible

    def setToolTip(self, *_args):
        pass


# [FN CATEGORY] _mkdtemp_safe — observed on some macOS CI runners: tempfile.gettempdir()'s reported
# base directory can transiently not exist mid-run, failing mkdtemp() outright for every test after
# whichever one first hits it (a whole-suite cascade, not one flaky test). Every direct
# tempfile.mkdtemp() call in this file (and _temp_dir below) routes through this instead, falling
# back to a directory this process just created itself (guaranteed to exist) rather than letting
# the base OS temp dir's transient absence fail an unrelated test.
# [FN] _mkdtemp_safe — tempfile.mkdtemp() with a guaranteed-to-exist fallback base directory
# [FN OPEN] _mkdtemp_safe
def _mkdtemp_safe(**kwargs):
    try:
        return tempfile.mkdtemp(**kwargs)
    except FileNotFoundError:
        fallback = os.path.join(os.getcwd(), '.pytest_tmp')
        os.makedirs(fallback, exist_ok=True)
        return tempfile.mkdtemp(dir=fallback, **kwargs)
# [FN CLOSED] _mkdtemp_safe


# [FN CATEGORY] _temp_dir — a MainWindow constructed against this path may still hold its
# QFileSystemWatcher open on it after .close() (Qt hides on close, it doesn't tear down child
# objects), which on Windows can make the OS refuse to delete the directory for a moment.
# tempfile.TemporaryDirectory()'s own cleanup raises on that; this swallows it instead — a few
# leftover temp dirs in the OS temp folder is a fine trade for tests that don't flake on Windows.
# [FN] _temp_dir — like tempfile.TemporaryDirectory() but ignores Windows cleanup races
# [FN OPEN] _temp_dir
@contextlib.contextmanager
def _temp_dir():
    path = _mkdtemp_safe()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
# [FN CLOSED] _temp_dir


def _write_app_py(source_dir):
    source = source_dir / 'app.py'
    source.write_text('\n'.join([
        '# [MOD CATEGORY] shop/__init__.py — exposes the server module from the package namespace',
        '# [MOD shop/__init__.py] — package exports',
        '# [MOD OPEN #abc12345] shop/__init__.py',
        'print(1)',
        '# [MOD CLOSED #abc12345] shop/__init__.py',
    ]), encoding='utf-8')
    return source


# [TST CATEGORY] KantSmokeTest — one offscreen regression check per feature area rather than a
# single mega-test: a failing assertion now names the feature that broke and pytest -k can run
# just it. setUpClass builds the one QApplication instance the whole process needs; individual
# tests each own their own temp directory/window instead of sharing state across the file, so
# they can run (and fail) independently of each other.
# [TST] KantSmokeTest — the project's regression suite, run offscreen (QT_QPA_PLATFORM=offscreen)
# [TST OPEN] KantSmokeTest
class KantSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        cls.app = QApplication.instance() or QApplication(sys.argv)
        # closeEvent now confirms before quitting (a real modal .exec()) — default it to "yes" for
        # every test's window.close() calls, since none of them are testing that dialog itself.
        # A dedicated method (not _ide_yes_no, which plenty of tests already stub for unrelated
        # dialogs) so this class-wide default can't silently collide with an individual test's own
        # _ide_yes_no override and make its window.close() no-op instead of really closing.
        cls._original_confirm_close = MainWindow._confirm_close
        MainWindow._confirm_close = lambda self: True
        # _check_crash_recovery reads/writes session/cleanExit in QSettings — real, persistent,
        # machine-wide state, not anything scoped to this test process. It's set False at the
        # START of every MainWindow() and only set True again by a clean closeEvent; any run that
        # doesn't reach that (a killed process, a crashed test, Ctrl+C) leaves it False, and the
        # NEXT MainWindow() constructed on this machine — in ANY process, this test suite or the
        # real app — then blocks in _check_crash_recovery's modal "resume session?" QDialog.exec(),
        # hanging forever with nothing present to click it. Never exercise that against real
        # QSettings here: one interrupted run must not be able to wedge every future test run.
        cls._original_check_crash_recovery = MainWindow._check_crash_recovery
        MainWindow._check_crash_recovery = lambda self: None
        # same hazard, same fix, for the other unconditional-on-startup QSettings check: a pending
        # AI-review snapshot left over from an interrupted test/run also pops a blocking modal
        # (_ide_choice) on every later MainWindow() until someone answers it.
        cls._original_check_pending_ai_snapshot = MainWindow._check_pending_ai_snapshot
        MainWindow._check_pending_ai_snapshot = lambda self: None

    @classmethod
    def tearDownClass(cls):
        MainWindow._confirm_close = cls._original_confirm_close
        MainWindow._check_crash_recovery = cls._original_check_crash_recovery
        MainWindow._check_pending_ai_snapshot = cls._original_check_pending_ai_snapshot

    def test_main_window_shell_claude_pane_and_agent_launch(self):
        window = MainWindow()
        assert window.splitter.orientation() == Qt.Horizontal
        assert window.splitter.widget(1) is window.claude_pane
        assert window.main_splitter.orientation() == Qt.Vertical
        assert window.main_splitter.widget(0) is window.workspace_splitter
        assert window.main_splitter.widget(1) is window.terminal_dock
        assert window.terminal_stack.widget(0) is window.terminal
        assert window.kant_map_label.parent() is window.statusBar()
        assert window.map_tab_btn.parent() is window.shell
        assert window.map_tab_btn.isHidden()  # only shown once a project is open
        window.claude_pane._add_message('domanda', 'user')
        window.claude_pane._append_stream('risposta')
        assert window.claude_pane._messages[-2][0] == 'user'
        assert window.claude_pane._messages[-1][0] == 'assistant'
        mcp_reply = handle_message(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': 'approve', 'arguments': {'tool_name': 'Write', 'input': {'file_path': 'sample.py'}},
            }},
            lambda arguments: {'behavior': 'allow', 'updatedInput': arguments['input']},
        )
        assert '"behavior": "allow"' in mcp_reply['result']['content'][0]['text']
        bridge_result = []
        window.claude_pane.auto_permissions.setChecked(True)

        def ask_permission():
            bridge = window.claude_pane.permission_bridge
            with socket.create_connection(('127.0.0.1', bridge.port), timeout=2) as connection:
                request = {'token': bridge.token, 'tool_name': 'Write', 'input': {'file_path': 'sample.py'}}
                connection.sendall((json.dumps(request) + '\n').encode('utf-8'))
                bridge_result.append(json.loads(connection.makefile('rb').readline().decode('utf-8')))

        permission_thread = threading.Thread(target=ask_permission, daemon=True)
        permission_thread.start()
        deadline = time.monotonic() + 3
        while permission_thread.is_alive() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        permission_thread.join(timeout=0.1)
        assert bridge_result and bridge_result[0]['behavior'] == 'allow'
        window.claude_pane.auto_permissions.setChecked(False)
        manual_request = {
            'tool_name': 'Edit', 'input': {'file_path': 'sample.py'},
            'event': threading.Event(), 'response': None,
        }
        window.claude_pane._permission_requested(manual_request)
        # color-coded so a misclick isn't as easy: deny is the danger color, both allow options
        # share the accept color — checked via the same TAG_COLORS/OK theme constants the styling
        # itself draws from, so a theme change can't silently desync this from the real palette
        perm_buttons = window.claude_pane._permission_cards[-1][2]
        assert theme.TAG_COLORS['TST'] in perm_buttons[0].styleSheet()
        assert theme.OK in perm_buttons[1].styleSheet() and theme.OK in perm_buttons[2].styleSheet()
        window.claude_pane._permission_cards[-1][2][1].click()
        assert manual_request['response']['behavior'] == 'allow'
        cards_before = len(window.claude_pane._permission_cards)
        window.claude_pane._auto_permissions_once = True
        one_shot_request = {
            'tool_name': 'Write', 'input': {'file_path': 'first_kant.py'},
            'event': threading.Event(), 'response': None,
        }
        window.claude_pane._permission_requested(one_shot_request)
        assert one_shot_request['response']['behavior'] == 'allow'
        assert len(window.claude_pane._permission_cards) == cards_before
        window.claude_pane._auto_permissions_once = False
        window.close()

    def test_claude_pane_effort_selector_and_send_shortcut(self):
        window = MainWindow()
        pane = window.claude_pane
        pane.set_agent('claude')
        assert not pane.model_select.isEditable() and not pane.effort_select.isEditable()
        # the closed face stays compact/icon-only on request, but the dropdown itself must always be
        # wide enough to show a full model/effort name — the actual regression before was reportedly
        # about the OPEN dropdown, not the closed face, so this is the width that has to hold
        assert pane.model_select.width() == 44 and pane.effort_select.width() == 44
        assert 'color:transparent' in pane.model_select.styleSheet()
        assert 'color:transparent' in pane.effort_select.styleSheet()
        assert pane.model_select.view().minimumWidth() >= 150
        assert pane.effort_select.view().minimumWidth() >= 100
        assert not pane.model_select.itemIcon(0).isNull() and not pane.effort_select.itemIcon(0).isNull()
        # effort's icon is colored per level, not the same icon/color for every entry
        low_icon = pane.effort_select.itemIcon(pane.effort_select.findText('low')).pixmap(14, 14).toImage()
        max_icon = pane.effort_select.itemIcon(pane.effort_select.findText('max')).pixmap(14, 14).toImage()
        assert low_icon.pixelColor(7, 7) != max_icon.pixelColor(7, 7)
        assert pane.attach_btn.text() == '' and not pane.attach_btn.icon().isNull()
        assert 'analizza il codice' in pane.prompt.placeholderText()
        # effort options sync per agent, same shape as the existing model selector
        assert pane.effort_select.currentText() == MODEL_DEFAULT
        claude_efforts = [pane.effort_select.itemText(i) for i in range(pane.effort_select.count())]
        assert 'xhigh' in claude_efforts and 'max' in claude_efforts
        pane.agent_select.setCurrentIndex(pane.agent_select.findData('codex'))
        codex_efforts = [pane.effort_select.itemText(i) for i in range(pane.effort_select.count())]
        assert 'xhigh' in codex_efforts and 'ultra' in codex_efforts
        pane.agent_select.setCurrentIndex(pane.agent_select.findData('claude'))

        pane.effort_select.setCurrentText('high')
        calls = []
        pane.run_prompt = lambda *args, **kwargs: calls.append(kwargs) or False
        pane.prompt.setPlainText('ciao')
        pane._send()
        assert calls and calls[0]['effort'] == 'high'  # the UI selection actually reaches run_prompt

        # Return sends; Ctrl+Return inserts a newline instead — a plain keyPressEvent override on
        # _PromptEdit (not a QShortcut), so a direct QTest.keyClick reliably exercises it, unlike
        # WidgetShortcut delivery which needs real window activation/focus offscreen Qt doesn't grant
        calls.clear()
        pane.prompt.setPlainText('')
        QTest.keyClicks(pane.prompt, 'riga1')
        QTest.keyClick(pane.prompt, Qt.Key_Return, Qt.ControlModifier)
        QTest.keyClicks(pane.prompt, 'riga2')
        assert calls == []  # Ctrl+Return must not have sent
        assert pane.prompt.toPlainText() == 'riga1\nriga2'
        QTest.keyClick(pane.prompt, Qt.Key_Return)  # plain Return sends
        assert calls and calls[0]['effort'] == 'high'
        window.close()

    def test_terminal_dock_sidebar_and_errors_view(self):
        window = MainWindow()
        assert window.terminal_stack.currentIndex() == 0
        assert window.terminal_sidebar_group.button(0).isChecked()

        # switching to the Python-REPL tab starts a real interactive python process lazily, only
        # on first switch — not at construction, since most sessions never open it
        assert window.python_terminal.process is None
        window._switch_terminal_tab(1)
        assert window.terminal_stack.currentIndex() == 1
        assert window.terminal_stack.currentWidget() is window.python_terminal
        assert window.python_terminal.process is not None
        window.python_terminal.process.kill()
        window.python_terminal.process.waitForFinished(2000)

        window._switch_terminal_tab(2)
        assert window.terminal_stack.currentIndex() == 2
        assert window.terminal_stack.currentWidget() is window.errors_view
        assert window.terminal_sidebar_group.button(3).property('kantIcon') == 'kant'
        window._show_validation_results(['sample.py: errore KANT'], [])
        assert window.terminal_stack.currentWidget() is window.kant_errors_view
        assert window.terminal_sidebar_group.button(3).isChecked()
        assert 'errore KANT' in window.kant_errors_view.topLevelItem(0).child(0).text(0)

        # the errors tab mirrors whatever _apply_syntax_status just decided for the active file —
        # exercised directly here with a synthetic bad result, same as a real failed local check
        source_dir = Path(_mkdtemp_safe())
        source = _write_app_py(source_dir)
        errors_tab = FileTab(str(source), parse_kant(source.read_text(encoding='utf-8')))
        window.tabs.addTab(errors_tab, 'app.py')
        window.tabs.setCurrentWidget(errors_tab)
        window._apply_syntax_status(str(source), serialize_kant(errors_tab.tree), {'ok': False, 'line': 3, 'message': 'boom'}, None)
        assert window.errors_view.topLevelItemCount() == 1
        error_item = window.errors_view.topLevelItem(0)
        assert 'boom' in error_item.text(0) and error_item.data(0, ROLE_LINE) == 3

        # a clean result clears the list instead of leaving a stale error behind
        window._apply_syntax_status(str(source), serialize_kant(errors_tab.tree), {'ok': True, 'message': 'Sintassi OK'}, None)
        assert window.errors_view.topLevelItemCount() == 0
        window.close()

    def test_kant_code_map_launch_args(self):
        launch_args = []
        set_agent_calls, model_select_calls = [], []
        launch_window = MainWindow.__new__(MainWindow)
        launch_window.claude_pane = type('Pane', (), {
            'run_prompt': lambda _self, *args, **kwargs: launch_args.append((args, kwargs)),
            'set_agent': lambda _self, agent: set_agent_calls.append(agent),
            'model_select': type('Combo', (), {'setCurrentText': lambda _self, text: model_select_calls.append(text)})(),
        })()
        MainWindow._launch_kant_code_map(launch_window, 'claude')
        assert launch_args[0][1]['auto_permissions_once'] is True
        assert launch_args[0][1]['effort'] is None
        assert not set_agent_calls  # no model given -> the combo is left alone
        MainWindow._launch_kant_code_map(launch_window, 'claude', 'claude-opus-4-8', 'high')
        assert set_agent_calls == ['claude'] and model_select_calls == ['claude-opus-4-8']
        assert launch_args[1][1]['effort'] == 'high'

    def test_project_kant_blanks_lists_every_file_and_flags_broken_ones(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'a.py').write_text(
                '# [FN CATEGORY] alpha —\n# [FN] alpha —\n# [FN OPEN] alpha\ndef alpha(): pass\n# [FN CLOSED] alpha\n',
                encoding='utf-8',
            )
            (root / 'b.py').write_text(
                '# [FN CATEGORY] beta — already documented\n# [FN] beta — does a thing\n'
                '# [FN OPEN] beta\ndef beta(): pass\n# [FN CLOSED] beta\n',
                encoding='utf-8',
            )
            (root / 'broken.py').write_text('# [FN OPEN] x\ndef x(): pass\n# [FN CLOSED] y\n', encoding='utf-8')
            window = MainWindow.__new__(MainWindow)
            lines, broken = MainWindow._project_kant_blanks(window, str(root))
            assert any('a.py' in line and 'alpha' in line for line in lines)
            assert not any('b.py' in line for line in lines)  # beta already has real text
            assert broken == ['broken.py']

    def test_launch_kant_fill_blanks_prompts_with_the_project_wide_listing(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'a.py').write_text(
                '# [FN CATEGORY] alpha —\n# [FN] alpha —\n# [FN OPEN] alpha\ndef alpha(): pass\n# [FN CLOSED] alpha\n',
                encoding='utf-8',
            )
            launch_args = []
            window = MainWindow.__new__(MainWindow)
            window.project_root_path = str(root)
            window.claude_pane = type('Pane', (), {
                'run_prompt': lambda _self, *args, **kwargs: launch_args.append((args, kwargs)),
                'set_agent': lambda _self, agent: None,
                'model_select': type('Combo', (), {'setCurrentText': lambda _self, text: None})(),
            })()
            MainWindow._launch_kant_fill_blanks(window, 'claude')
            assert len(launch_args) == 1
            prompt = launch_args[0][0][0]
            assert 'alpha' in prompt and 'a.py' in prompt and 'COME funziona' in prompt

    def test_launch_kant_fill_blanks_does_nothing_when_no_blanks_exist(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'a.py').write_text(
                '# [FN CATEGORY] alpha — documented\n# [FN] alpha — does a thing\n'
                '# [FN OPEN] alpha\ndef alpha(): pass\n# [FN CLOSED] alpha\n',
                encoding='utf-8',
            )
            launch_args = []
            window = MainWindow.__new__(MainWindow)
            window.project_root_path = str(root)
            window.claude_pane = type('Pane', (), {
                'run_prompt': lambda _self, *args, **kwargs: launch_args.append((args, kwargs)),
            })()
            MainWindow._launch_kant_fill_blanks(window, 'claude')
            assert not launch_args

    def _fake_ai_fill_window(self, tab):
        prompts, messages = [], []
        window = MainWindow.__new__(MainWindow)
        window.tabs = type('Tabs', (), {'currentWidget': lambda _self: tab})()
        window._render_view = lambda _tab, _uid: None
        window._update_tab_title = lambda _tab: None
        window._ide_message = lambda title, message: messages.append((title, message))
        window.claude_pane = type('Pane', (), {
            'effort_select': type('Combo', (), {'currentText': lambda _self: MODEL_DEFAULT})(),
            'run_prompt': lambda _self, prompt, effort=None: prompts.append((prompt, effort)),
        })()
        return window, prompts, messages

    def test_ai_fill_kant_blanks_inserts_skeleton_and_prompts_the_current_agent(self):
        src = 'def alpha():\n    pass\n'
        tab = type('Tab', (), {
            'path': 'mod.py', 'tree': parse_kant(src), 'filter_uid': None,
            'remember_undo_state': lambda _self: None, 'mark_dirty': lambda _self: None,
        })()
        window, prompts, messages = self._fake_ai_fill_window(tab)
        MainWindow._ai_fill_kant_blanks(window)
        assert not messages
        assert len(prompts) == 1
        prompt, effort = prompts[0]
        assert 'alpha' in prompt and '[FN]' in prompt and 'mod.py' in prompt
        assert effort is None
        # the skeleton insertion replaced the tab's tree with a marked-up one, in place
        assert tab.tree.body and any(getattr(n, 'name', None) == 'alpha' for n in tab.tree.body if hasattr(n, 'name'))

    def test_ai_fill_kant_blanks_reports_when_nothing_is_blank(self):
        src = '\n'.join([
            '# [FN CATEGORY] alpha — reads a value and doubles it',
            '# [FN] alpha — doubles the input',
            '# [FN OPEN] alpha',
            'def alpha():',
            '    pass',
            '# [FN CLOSED] alpha',
        ])
        tab = type('Tab', (), {
            'path': 'mod.py', 'tree': parse_kant(src), 'filter_uid': None,
            'remember_undo_state': lambda _self: None, 'mark_dirty': lambda _self: None,
        })()
        window, prompts, messages = self._fake_ai_fill_window(tab)
        MainWindow._ai_fill_kant_blanks(window)
        assert not prompts
        assert messages and 'Nessun campo KANT vuoto' in messages[0][1]

    def test_agent_command_building(self):
        automatic = _agent_command('codex', 'tagga', True)[1]
        assert '--full-auto' not in automatic
        assert automatic[:5] == ['exec', '--sandbox', 'workspace-write', '--ask-for-approval', 'never']
        # --model must precede the trailing prompt positional for both agents, or the CLI would
        # consume the flag/value as the prompt itself instead of the actual prompt text
        claude_args = _agent_command('claude', 'ciao', model='claude-opus-4-8')[1]
        assert claude_args == ['--model', 'claude-opus-4-8', '-p', 'ciao']
        codex_args = _agent_command('codex', 'ciao', True, 'gpt-5.6')[1]
        assert codex_args == [
            'exec', '--sandbox', 'workspace-write', '--ask-for-approval', 'never',
            '--model', 'gpt-5.6', 'ciao',
        ]
        assert _agent_command('claude', 'ciao')[1] == ['-p', 'ciao']  # no --model when unset
        # effort: a real flag for claude, a config override for codex — both come after --model,
        # before the trailing prompt positional
        assert _agent_command('claude', 'ciao', effort='high')[1] == ['--effort', 'high', '-p', 'ciao']
        codex_effort_args = _agent_command('codex', 'ciao', effort='medium')[1]
        assert codex_effort_args == ['exec', '-c', 'model_reasoning_effort="medium"', 'ciao']
        assert _agent_command('claude', 'ciao')[1] == ['-p', 'ciao']  # no --effort when unset
        # session_args (conversation-continuity marker) splices in before --model/effort for both
        # providers, still ahead of the trailing prompt positional
        assert _agent_command('claude', 'ciao', session_args=('--session-id', 'abc'))[1] == ['--session-id', 'abc', '-p', 'ciao']
        assert _agent_command('claude', 'ciao', session_args=('--resume', 'abc'))[1] == ['--resume', 'abc', '-p', 'ciao']
        codex_resume_args = _agent_command('codex', 'ciao', session_args=('resume', '--last'))[1]
        assert codex_resume_args == ['exec', 'resume', '--last', 'ciao']

    def test_claude_pane_resumes_conversation_across_messages(self):
        # each run_prompt call is otherwise a brand-new, memory-less claude/codex process — this
        # checks the fix: the pane mints a session id on the first Claude message and resumes that
        # same id on the next one (instead of starting fresh every time), Codex gets `exec resume
        # --last` from its second message onward, and switching project (set_cwd) resets both so a
        # new project doesn't inherit the old one's conversation.
        class _ProcessStub:
            captured = []

            def __init__(self, _parent=None):
                self.readyReadStandardOutput = self.readyReadStandardError = self
                self.errorOccurred = self.finished = self

            def connect(self, *_args):
                pass

            def setWorkingDirectory(self, _cwd):
                pass

            def start(self, executable, args):
                _ProcessStub.captured.append((executable, args))

            def closeWriteChannel(self):
                pass

        original_process_cls = kant_widgets_module.QProcess
        original_which = kant_widgets_module.shutil.which
        kant_widgets_module.QProcess = _ProcessStub
        kant_widgets_module.shutil.which = lambda command: command
        pane = ClaudePane(os.getcwd())
        try:
            pane.set_agent('claude')
            assert pane.run_prompt('primo messaggio')
            pane.process = None  # simulates _finished having already run
            assert pane.run_prompt('secondo messaggio')
            claude_calls = [args for _exe, args in _ProcessStub.captured]
            first_session_flag, first_session_id = claude_calls[0][0], claude_calls[0][1]
            assert first_session_flag == '--session-id'
            assert claude_calls[1][0] == '--resume' and claude_calls[1][1] == first_session_id

            _ProcessStub.captured.clear()
            pane.process = None
            pane.set_agent('codex')
            assert pane.run_prompt('primo messaggio codex')
            pane.process = None
            assert pane.run_prompt('secondo messaggio codex')
            codex_calls = [args for _exe, args in _ProcessStub.captured]
            assert codex_calls[0][:1] == ['exec'] and 'resume' not in codex_calls[0]
            assert codex_calls[1][:3] == ['exec', 'resume', '--last']

            # switching project resets both providers' tracked session
            pane.process = None
            pane._session_allowed_tools.add('Edit')
            assert pane.run_prompt('mantieni permesso')
            assert 'Edit' in pane._session_allowed_tools
            pane.process = None
            pane.set_cwd(os.getcwd())
            assert pane._claude_session_id is None and pane._codex_resumable is False
            assert not pane._session_allowed_tools
        finally:
            kant_widgets_module.QProcess = original_process_cls
            kant_widgets_module.shutil.which = original_which
            pane.deleteLater()

    def test_codex_context_hint_not_reframed_as_kant_code_map(self):
        # bug: every codex message (not just genuine /kant-code-map runs) was being rewritten into
        # "this is an explicit request to run /kant-code-map... create or update KANT_<project>.md",
        # burying both the real request and the hidden context_hint (the coding panel's currently
        # isolated file/element) under an unrelated project-wide tagging task — which is why the AI
        # seemed to ignore the focused element and "forget" what was actually asked.
        class _ProcessStub:
            captured = []

            def __init__(self, _parent=None):
                self.readyReadStandardOutput = self.readyReadStandardError = self
                self.errorOccurred = self.finished = self

            def connect(self, *_args):
                pass

            def setWorkingDirectory(self, _cwd):
                pass

            def start(self, _executable, args):
                _ProcessStub.captured.append(args)

            def closeWriteChannel(self):
                pass

        original_process_cls = kant_widgets_module.QProcess
        original_which = kant_widgets_module.shutil.which
        kant_widgets_module.QProcess = _ProcessStub
        # run_prompt refuses to start when shutil.which(executable) finds nothing — passes locally
        # only by accident (the real claude/codex CLI happens to be on PATH on a dev machine), fails
        # on a clean CI runner where neither is installed. Stub it like the sibling tests already do.
        kant_widgets_module.shutil.which = lambda command: command
        pane = ClaudePane(os.getcwd())
        try:
            pane.set_agent('codex')
            assert pane.run_prompt(
                'modifica solo questa funzione',
                context_hint='Contesto implicito: applica le modifiche solo a [FN] alpha.',
            )
            sent_prompt = _ProcessStub.captured[-1][-1]
            assert '/kant-code-map' not in sent_prompt
            assert 'KANT_<nome-progetto>.md' not in sent_prompt
            assert 'Prima leggi il file temporaneo' in sent_prompt
            assert 'modifica solo questa funzione' in sent_prompt
            with open(pane.system_prompt_file, encoding='utf-8') as f:
                saved_instructions = f.read()
            assert 'applica le modifiche solo a [FN] alpha' in saved_instructions
            # context_hint must come BEFORE the kant-comment-standard skill body: verified live
            # (direct CLI runs) that a long, unrelated skill body positioned before the hint made
            # Claude ignore it and ask the user to paste code instead of reading the focused file
            # itself, 6/6 times across two hint wordings; reordering hint-first fixed it 6/6 times.
            assert saved_instructions.index('applica le modifiche solo a [FN] alpha') < saved_instructions.index('KANT comment standard')

            pane.process = None
            assert pane.run_prompt(
                'Applica la convenzione KANT a tutto il progetto', extra_skills=('kant-code-map',),
            )
            kant_map_prompt = _ProcessStub.captured[-1][-1]
            assert '/kant-code-map' in kant_map_prompt
        finally:
            kant_widgets_module.QProcess = original_process_cls
            kant_widgets_module.shutil.which = original_which
            pane.deleteLater()

    def test_claude_context_hint_precedes_skill_body(self):
        # same bug, Claude's own delivery path (--append-system-prompt / --append-system-prompt-file
        # instead of a temp-file-read instruction): verified live that the hint being positioned
        # AFTER the ~3.5KB kant-comment-standard body made Claude reliably (6/6 runs, two wordings)
        # ask the user to paste code instead of reading the coding panel's focused file itself, no
        # matter how strongly the hint was worded; reordering hint-first fixed it 6/6 times.
        class _ProcessStub:
            captured = []

            def __init__(self, _parent=None):
                self.readyReadStandardOutput = self.readyReadStandardError = self
                self.errorOccurred = self.finished = self

            def connect(self, *_args):
                pass

            def setWorkingDirectory(self, _cwd):
                pass

            def start(self, _executable, args):
                _ProcessStub.captured.append(args)

            def closeWriteChannel(self):
                pass

        original_process_cls = kant_widgets_module.QProcess
        original_which = kant_widgets_module.shutil.which
        kant_widgets_module.QProcess = _ProcessStub
        # run_prompt refuses to start when shutil.which(executable) finds nothing — passes locally
        # only by accident (the real claude/codex CLI happens to be on PATH on a dev machine), fails
        # on a clean CI runner where neither is installed. Stub it like the sibling tests already do.
        kant_widgets_module.shutil.which = lambda command: command
        pane = ClaudePane(os.getcwd())
        try:
            pane.set_agent('claude')
            assert pane.run_prompt(
                'cosa ne pensi di queste variabili',
                context_hint='Implicit context: shop/__init__.py::__all__. Read it yourself.',
            )
            args = _ProcessStub.captured[-1]
            # a longer combined system prompt (hint + skill bodies) switches from the inline flag
            # to a temp-file flag past run_prompt's own length threshold — a real, correct branch,
            # not something this test should assume away; read whichever one was actually used
            if '--append-system-prompt' in args:
                system_prompt = args[args.index('--append-system-prompt') + 1]
            else:
                system_prompt_path = args[args.index('--append-system-prompt-file') + 1]
                system_prompt = Path(system_prompt_path).read_text(encoding='utf-8')
            assert system_prompt.index('Implicit context') < system_prompt.index('KANT comment standard')
        finally:
            kant_widgets_module.QProcess = original_process_cls
            kant_widgets_module.shutil.which = original_which
            pane.deleteLater()

    def test_ai_context_hint(self):
        hint_tree = parse_kant('\n'.join([
            '# [MOD OPEN #hm1] hint.py',
            '# [FN OPEN #hf1] alpha', 'def alpha(): pass', '# [FN CLOSED #hf1] alpha',
            '# [FN OPEN #hf2] beta', 'def beta(): pass', '# [FN CLOSED #hf2] beta',
            '# [VAR OPEN #hv1] exported package modules',
            '__all__ = ["server"]', 'TEST_USER_EMAIL = "test@example.com"',
            '# [VAR CLOSED #hv1] exported package modules',
            '# [MOD CLOSED #hm1] hint.py',
        ]))
        hint_tab = type('Tab', (), {'tree': hint_tree, 'path': 'hint.py', 'filter_uid': None})()
        hint_window = MainWindow.__new__(MainWindow)
        hint_window.tabs = type('Tabs', (), {'currentWidget': lambda _self: hint_tab})()
        hint_window.claude_pane = type('Pane', (), {'global_mode_btn': type('Btn', (), {'isChecked': lambda _self: False})()})()
        hint_window.settings = type('Settings', (), {'value': lambda _self, _key, _default: 'it'})()
        whole_file_hint = MainWindow._build_ai_context_hint(hint_window)
        assert whole_file_hint == (
            'Contesto implicito: hint.py. Hai accesso in lettura al progetto in questa cartella — se '
            'il messaggio non nomina esplicitamente un file diverso, leggi tu stesso hint.py dal '
            'filesystem per rispondere. Non chiedere all\'utente di incollare il codice.'
        )
        hint_tab.filter_uid = 'hf1'
        element_hint = MainWindow._build_ai_context_hint(hint_window)
        assert element_hint == (
            'Contesto implicito: hint.py::alpha. Hai accesso in lettura al progetto in questa '
            'cartella — se il messaggio non nomina esplicitamente un file diverso, leggi tu stesso '
            'hint.py::alpha dal filesystem per rispondere. Non chiedere all\'utente di incollare il codice.'
        )
        assert 'FN' not in element_hint and 'def alpha' not in element_hint  # no KANT or source payload
        hint_tab.filter_uid = None
        hint_tab._ai_focus_uid = 'hv1'  # focused inner block while the whole module tab stays open
        focused_variable_hint = MainWindow._build_ai_context_hint(hint_window)
        assert 'hint.py::__all__,TEST_USER_EMAIL' in focused_variable_hint
        hint_tab.filter_uid = 'hv1'
        variable_hint = MainWindow._build_ai_context_hint(hint_window)
        assert 'hint.py::__all__,TEST_USER_EMAIL' in variable_hint
        assert 'VAR' not in variable_hint and 'exported package modules' not in variable_hint
        hint_window.settings = type('Settings', (), {'value': lambda _self, _key, _default: 'en'})()
        assert MainWindow._build_ai_context_hint(hint_window).startswith('Implicit context: hint.py::__all__,TEST_USER_EMAIL.')
        hint_window.claude_pane = type('Pane', (), {'global_mode_btn': type('Btn', (), {'isChecked': lambda _self: True})()})()
        hint_window.project_root_path = 'C:/project'
        hint_tab.path = 'C:/project/hint.py'
        global_hint = MainWindow._build_ai_context_hint(hint_window)
        assert global_hint.startswith('Root: C:/project | View: hint.py::__all__,TEST_USER_EMAIL.')
        assert 'Use the whole root' in global_hint
        hint_window.tabs = type('Tabs', (), {'currentWidget': lambda _self: None})()
        hint_window.claude_pane = type('Pane', (), {'global_mode_btn': type('Btn', (), {'isChecked': lambda _self: False})()})()
        assert MainWindow._build_ai_context_hint(hint_window) is None  # no open tab -> nothing to scope to

    def test_tree_label_click_forwarding(self):
        window = MainWindow()
        assert not hasattr(window, 'results_label_btn')
        # tree rows use a rich-HTML label via setItemWidget; it must forward its own clicks to the
        # owning item directly (WA_TransparentForMouseEvents pass-through proved unreliable for this)
        dummy_item = QTreeWidgetItem(window.tree)
        label_clicks, label_dclicks = [], []
        window.tree.itemClicked.connect(lambda it, col: label_clicks.append(it))
        window.tree.itemDoubleClicked.connect(lambda it, col: label_dclicks.append(it))
        tree_label = window._tree_label(dummy_item, 'MOD', 'short')
        tree_label.resize(100, 20)
        QTest.mouseClick(tree_label, Qt.LeftButton)
        assert label_clicks == [dummy_item]
        QTest.mouseDClick(tree_label, Qt.LeftButton)
        assert label_dclicks == [dummy_item]
        assert window.title_bar.file_menu_btn.menu() is not None
        window.close()

    def test_tree_stylesheet_stays_tight_and_consistent_across_theme_toggle(self):
        # regression: the tree's QSS used to be built independently at construction time and again
        # in _apply_theme's theme-toggle refresh, and the two had drifted apart — a real dead-space
        # regression (padding:6px 4px vs. a later padding:14px 10px) plus a hardcoded, night-blind
        # selection color, both only visible after the first day/night toggle. One shared
        # _tree_stylesheet() builder now backs both call sites, so they can't diverge again.
        window = MainWindow()
        before = window.tree.styleSheet()
        assert 'padding:6px 4px' in before  # the tight, boxed style — not the old 14px/10px drift
        window._toggle_theme()
        after = window.tree.styleSheet()
        assert after == window._tree_stylesheet()
        assert 'padding:6px 4px' in after  # still tight after a real theme toggle, not just at boot
        window._toggle_theme()  # back to the original theme, tidy for anything after this test
        window.close()

    def test_kant_view_bar_and_compact_tree_toggle(self):
        with _temp_dir() as tmp:
            project = Path(tmp)
            source = _write_app_py(project)
            source.write_text('\n'.join([
                '# [MOD OPEN #m1] app.py',
                '# [FN OPEN #f1] helper', 'def helper(): pass', '# [FN CLOSED #f1] helper',
                '# [MOD CLOSED #m1] app.py',
            ]), encoding='utf-8')
            window = MainWindow()
            window.project_root_path = str(project)
            window.git_root = None
            window.git_status = {}
            window._rebuild_tree(refresh_git=False)
            window.stack.setCurrentIndex(1)
            window._set_project_chrome_visible(True)
            window.show()
            QApplication.processEvents()

            assert window.code_view_btn.text() == 'KANT'
            assert not any(label.text() == 'Vista' for label in window.view_mode_bar.findChildren(QLabel))
            assert window.groups_view_btn.x() - (window.file_view_btn.x() + window.file_view_btn.width()) >= 14
            assert window.compact_kant_btn.x() > window.groups_view_btn.x()
            assert window.compact_kant_btn.property('kantIcon') == 'grid' and not window.compact_kant_btn.icon().isNull()

            file_item = window.tree.topLevelItem(0)
            assert file_item.data(0, ROLE_PATH) == str(source)
            assert window.tree.itemWidget(file_item, 0).wordWrap()
            assert file_item.childCount() > 0

            window.compact_kant_btn.click()
            QApplication.processEvents()
            compact_item = window.tree.topLevelItem(0)
            assert window.compact_kant_view and window.compact_kant_btn.isChecked()
            assert not window.tree.itemWidget(compact_item, 0).wordWrap()
            assert compact_item.childCount() > 0  # same native expandable hierarchy
            assert 'padding:2px' in window.tree.styleSheet()

            window.compact_kant_btn.click()
            window.close()

    def test_vim_mode_disabled_by_default(self):
        # regression: _VIM_MODE_ENABLED defaulted to True (left over from developing the vim
        # keybindings feature) — a fresh install/launch had modal editing on unasked, on request
        # it's now opt-in like every other non-default editing mode in the app
        assert vim_mode_enabled() is False
        window = MainWindow()
        assert window.title_bar.vim_mode_menu_action.isChecked() is False
        window.close()

    def test_theme_toggle_restyles_python_terminal_and_add_row_buttons(self):
        # regression: _apply_theme's terminal-restyle only ever touched self.terminal, never its
        # sibling self.python_terminal in the same terminal_stack — invisible until
        # is_python_majority_project auto-switches to it, the one case a real user actually sees
        # the terminal dock in night mode with a still-day-colored REPL underneath. add_file_btn/
        # add_grouping_btn had the same gap: styled once at construction, never refreshed on toggle.
        with _temp_dir() as tmp:
            project = Path(tmp)
            (project / 'a.py').write_text('# [MOD OPEN] a.py\n# [MOD CLOSED] a.py\n', encoding='utf-8')
            window = MainWindow()
            window._ide_yes_no = lambda *a, **k: False
            window._open_project_folder(str(project))

            if window.night_mode:
                window._toggle_theme()
            assert window._open_file(str(project / 'a.py'))
            tab = window.open_tabs[str(project / 'a.py')]
            module = next(item for item in tab.tree.body if isinstance(item, Node))
            window._show_element_tab(tab, module.uid)
            element_page = window.tabs.currentWidget()
            window._pin_element_page(element_page)

            window._toggle_theme()
            assert theme.CODE_BG in window.python_terminal.styleSheet()
            assert theme.CODE_BG in window.terminal.styleSheet()
            assert window._add_row_button_style() == window.add_file_btn.styleSheet()
            assert window._add_row_button_style() == window.add_grouping_btn.styleSheet()
            assert theme.ACCENT in window.claude_pane.send_btn.styleSheet()
            assert f'border-bottom:2px solid {theme.ACCENT}' in window.tabs.tabBar().styleSheet()
            assert f'background-color:{theme.NIGHT_TAG_BACKGROUNDS["MOD"]}' in element_page._tab_label.text()

            image = window.action_toolbar_buttons['save'].icon().pixmap(18, 18).toImage()
            assert any(
                image.pixelColor(x, y).name().lower() == theme.ACCENT.lower()
                for x in range(image.width()) for y in range(image.height())
                if image.pixelColor(x, y).alpha()
            )

            if window.night_mode:
                window._toggle_theme()  # back to day, tidy for anything after this test
            window.close()

    def test_welcome_theme_toggle_button_flips_mode_and_stays_pinned_to_corner(self):
        # regression for "metti un selettore notte e giorno in basso a destra nella home": the
        # title bar's own Aspetto -> Notte/Giorno menu is hidden on the welcome screen
        # (_set_project_chrome_visible(False)), so before this there was no way to switch themes
        # before opening a project at all.
        window = MainWindow()
        window.resize(1000, 700)
        window.show()
        QApplication.processEvents()
        QApplication.processEvents()  # let the deferred QTimer.singleShot(0, ...) reposition run

        start_mode = window.night_mode
        QTest.mouseClick(window.welcome_theme_btn, Qt.LeftButton)
        assert window.night_mode != start_mode  # click actually toggled the theme, not just moved

        QApplication.processEvents()
        QApplication.processEvents()
        margin = 18
        expected_x = window.welcome_page.width() - window.welcome_theme_btn.width() - margin
        expected_y = window.welcome_page.height() - window.welcome_theme_btn.height() - margin
        actual_x, actual_y = window.welcome_theme_btn.pos().toTuple()
        # small tolerance for an offscreen-platform layout-settling quirk (a few px), not a real
        # position bug — same reasoning test_mappa_geometry_drag_reorder_and_tab_label_leak already
        # tolerates for this class of geometry check
        assert abs(actual_x - expected_x) <= 6 and abs(actual_y - expected_y) <= 6
        window.close()

    def test_project_tree_build_read_label_and_fs_reload(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            source_dir = root / 'src'
            source_dir.mkdir()
            source = _write_app_py(source_dir)
            label, error = read_top_level_label_result(str(source))
            assert error is None and label[:2] == ('MOD', 'package exports')
            assert label[3].category_desc == 'exposes the server module from the package namespace'
            bad = source_dir / 'bad.py'
            bad.write_text('# [FN OPEN #deadbeef] broken\n', encoding='utf-8')
            label, error = read_top_level_label_result(str(bad))
            assert label is None and error is not None

            tree_window = MainWindow()
            tree_window.project_root_path = str(root)
            tree_window.git_root = None
            tree_window.git_status = {}
            tree_window.tree.clear()
            tree_window._build_project_tree(tree_window.tree.invisibleRootItem(), str(root))
            top_kinds = [tree_window.tree.topLevelItem(i).data(0, ROLE_KIND) for i in range(tree_window.tree.topLevelItemCount())]
            assert 'dir' not in top_kinds and 'file' in top_kinds and 'invalidfile' in top_kinds
            # a file item starts collapsed — compact by default, expand on demand
            file_item = next(
                tree_window.tree.topLevelItem(i) for i in range(tree_window.tree.topLevelItemCount())
                if tree_window.tree.topLevelItem(i).data(0, ROLE_KIND) == 'file'
            )
            assert not file_item.isExpanded()
            assert 'ERRORI' in tree_window._validate_kant_project()
            assert tree_window.kant_errors_view.topLevelItemCount() == 1
            assert tree_window._open_file(str(source))
            opened_tab = tree_window.open_tabs[str(source)]
            source.write_text(source.read_text(encoding='utf-8').replace('print(1)', 'print(9)'), encoding='utf-8')
            tree_window._on_fs_file_changed(str(source))
            assert 'print(9)' in serialize_kant(opened_tab.tree)
            source.write_text(source.read_text(encoding='utf-8').replace('print(9)', 'print(1)'), encoding='utf-8')
            tree_window._on_fs_file_changed(str(source))
            tree_window._update_action_buttons()
            assert tree_window.title_bar.lsp_hover_menu_action.isEnabled()
            assert tree_window._lsp_status_text(str(source), None) == ' | LSP locale'
            assert 'pyright-langserver' in tree_window._lsp_missing_server_message(str(source))
            plain = source_dir / 'plain.py'
            plain.write_text('def helper():\n    return helper()\n', encoding='utf-8')
            assert tree_window._local_definition_locations('helper')[0][2] == 1
            assert len(tree_window._local_reference_locations('helper')) == 2
            tree_window.close()

    def test_legacy_file_uid_fallback_and_shared_coding_tabs(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            source_dir = root / 'src'
            source_dir.mkdir()
            source = _write_app_py(source_dir)
            tree_window = MainWindow()
            tree_window.project_root_path = str(root)
            tree_window.git_root = None
            tree_window.git_status = {}
            assert tree_window._open_file(str(source))
            # pin it: otherwise it's just the unpinned preview slot, and opening legacy.py below
            # would reuse (close) this tab instead of leaving two real, independent open tabs —
            # this test wants a genuine second tab so switching to index 0 later is a real tab change
            tree_window._pin_file_tab(tree_window.open_tabs[str(source)])

            # legacy (no #id) file: parse_kant mints a fresh random uid every reparse, and _open_file
            # always reparses from disk — so a tree item's uid (captured at the last tree rebuild) can
            # silently stop matching tab.tree. _on_tree_item_clicked must fall back to document order
            # (ROLE_ORDER) to still isolate the right section, instead of falling through to the
            # whole-file view.
            legacy = source_dir / 'legacy.py'
            legacy.write_text('\n'.join([
                '# [MOD OPEN] legacy.py',
                '# [FN OPEN] alpha', 'def alpha(): pass', '# [FN CLOSED] alpha',
                '# [FN OPEN] beta', 'def beta(): pass', '# [FN CLOSED] beta',
                '# [MOD CLOSED] legacy.py',
            ]), encoding='utf-8')
            tree_window.tree.clear()
            tree_window._build_project_tree(tree_window.tree.invisibleRootItem(), str(root))
            legacy_file_item = next(
                tree_window.tree.invisibleRootItem().child(i)
                for i in range(tree_window.tree.invisibleRootItem().childCount())
                if tree_window.tree.invisibleRootItem().child(i).data(0, ROLE_PATH) == str(legacy)
            )
            assert not legacy_file_item.isExpanded() and legacy_file_item.childCount() == 2  # collapsed by default, alpha+beta still built underneath
            beta_item = None
            it = tree_window.tree.invisibleRootItem()
            stack = [it.child(i) for i in range(it.childCount())]
            while stack:
                candidate = stack.pop()
                if candidate.data(0, ROLE_KIND) == 'section' and 'beta' in tree_window.tree.itemWidget(candidate, 0).text():
                    beta_item = candidate
                for i in range(candidate.childCount()):
                    stack.append(candidate.child(i))
            assert beta_item is not None and beta_item.data(0, ROLE_ORDER) is not None
            stale_uid = beta_item.data(0, ROLE_UID)
            tree_window._on_tree_item_clicked(beta_item, 0)
            legacy_tab = tree_window.open_tabs[str(legacy)]
            # pin it too: the rest of this test keeps using this exact FileTab instance across
            # several more files being opened, which would otherwise reuse (close) its still-unpinned
            # preview slot the moment a later file opens, re-opening legacy.py as a distinct new object
            tree_window._pin_file_tab(legacy_tab)
            assert legacy_tab.filter_uid is None  # the module remains open in the main page
            beta_page = tree_window.tabs.currentWidget()
            resolved_uid = beta_page._element_key[1]
            assert resolved_uid != stale_uid  # document order recovered the uid minted by the reparse
            assert [c.toPlainText().strip() for c in beta_page.findChildren(CodeEdit)] == ['def beta(): pass']
            tree_window._close_tab(tree_window.tabs.indexOf(beta_page))

            # Exercise isolated-main-view chrome independently; section clicks no longer mutate it.
            tree_window._render_view(legacy_tab, resolved_uid)

            # the main tab's own title (next to its close x) follows the isolated KANT element's
            # identity too, not the filename — matching the element-tab convention. The plain
            # tab text is cleared in favor of a rich-HTML label (_tab_label) using the same
            # colored/bold "[TAG] name" convention as the tree and coding panel.
            legacy_idx = tree_window.tabs.indexOf(legacy_tab)
            assert tree_window.tabs.tabText(legacy_idx) == ''
            tab_label_html = legacy_tab._tab_label.text()
            assert '[FN]' in tab_label_html and '<b>beta</b>' in tab_label_html  # bold name
            assert f'background-color:{theme.TAG_BACKGROUNDS["FN"]}' in tab_label_html  # colored tag badge
            assert tree_window.tabs.tabBar().tabButton(legacy_idx, QTabBar.LeftSide) is legacy_tab._tab_label
            # clicking the label (not the tab strip itself) must still switch to that tab
            tree_window.tabs.setCurrentIndex(0 if tree_window.tabs.currentIndex() == legacy_idx else legacy_idx)
            assert tree_window.tabs.currentWidget() is not legacy_tab
            legacy_tab._tab_label.mousePressEvent(QMouseEvent(
                QEvent.MouseButtonPress, QPointF(1, 1), QPointF(1, 1), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
            ))
            assert tree_window.tabs.currentWidget() is legacy_tab
            # regression: re-registering the SAME _tab_label widget via setTabButton (needed every call
            # to keep the tab sized to fit it) made Qt hide it internally with no matching re-show —
            # every theme refresh (or any second _update_tab_title call) left the tab blank
            assert not legacy_tab._tab_label.isHidden()
            tree_window._update_tab_title(legacy_tab)
            assert not legacy_tab._tab_label.isHidden()

            # the title bar's own slot shows the KANT identity too, not the filename — the filename
            # itself now lives in file_path_label, on the Incoming/Outgoing row
            assert tree_window.filename_label.text() == '[FN] beta'
            assert tree_window.file_path_label.text() == 'legacy.py'
            tree_window._render_view(legacy_tab, None)
            # whole-file view: both the tab and the title bar show the file's own top-level KANT tag,
            # not the raw filename — a module's identity is "[MOD] legacy.py", not just "legacy.py"
            assert tree_window.tabs.tabText(legacy_idx) == ''
            assert '[MOD]' in legacy_tab._tab_label.text() and 'legacy.py' in legacy_tab._tab_label.text()
            assert tree_window.filename_label.text() == '[MOD] legacy.py'
            assert tree_window.file_path_label.text() == 'legacy.py'

            # the outermost element of an isolated view has its "[TAG] name" title suppressed — the
            # tab and title bar already announce that identity, so repeating it inline would
            # be redundant; the panel starts directly with the category description instead. A NESTED
            # element (not the one being isolated) keeps its own header, since nothing else names it.
            nested_source = source_dir / 'nested.py'
            nested_source.write_text('\n'.join([
                '# [CLS CATEGORY] a class that does stuff',
                '# [CLS OPEN] Widget', 'class Widget:',
                '# [FN CATEGORY] initializes the widget',
                '# [FN OPEN] init', '    def init(self): pass', '# [FN CLOSED] init',
                '# [CLS CLOSED] Widget',
            ]), encoding='utf-8')
            assert tree_window._open_file(str(nested_source))
            self.app.processEvents()  # let legacy.py's reused-preview-slot deleteLater() settle first
            nested_tab = tree_window.open_tabs[str(nested_source)]
            cls_node = next(n for n in nested_tab.tree.body if hasattr(n, 'body'))
            fn_node = next(c for c in cls_node.body if getattr(c, 'tag', None) == 'FN')
            tree_window._render_view(nested_tab, cls_node.uid)
            # _open_file above already rendered the full-file view once; this isolates to just the
            # class right after, with no event-loop turn in between — draining it here lets the
            # first render's deleteLater()-pending CodeEdits actually go away before geometry is
            # queried below, instead of leaving orphaned zero/stale-geometry duplicates that can
            # tie (same 0,0 position) with the real ones and make the uid comparison a coin flip.
            self.app.processEvents()
            # _visible_ai_context_uid below picks the CodeEdit with the largest on-screen overlap
            # with the viewport — needs a real layout pass (show + processEvents), not just widget
            # construction, or every edit's mapTo()/width()/height() reports stale/zero geometry
            tree_window.tabs.setCurrentWidget(nested_tab)
            tree_window.show()
            self.app.processEvents()
            cls_widget = nested_tab.section_widgets[cls_node.uid]
            fn_widget = nested_tab.section_widgets[fn_node.uid]
            assert isinstance(cls_widget, CollapsibleSection) and cls_widget.toggle_btn is None
            cls_labels = ' '.join(l.text() for l in cls_widget.findChildren(QLabel))
            assert '[CLS]' not in cls_labels and 'a class that does stuff' in cls_labels  # no title, category kept
            # the "[TAG] name" title is suppressed here (redundant with the tab label), but the
            # metadata-edit (⋮) button must still be reachable — it's the only way to edit this
            # element's tag/name/description, whether or not a title is shown for it
            assert any(btn.text() == '⋮' for btn in cls_widget.findChildren(QToolButton))
            assert isinstance(fn_widget, LeafSection)
            fn_edit = fn_widget.findChild(CodeEdit)
            assert fn_edit.kant_node is fn_node
            assert tree_window._visible_ai_context_uid(nested_tab) == fn_node.uid
            tree_window._on_focus_changed(None, fn_edit)
            assert tree_window._ai_context_target() == (nested_tab, fn_node.uid)
            fn_labels = ' '.join(l.text() for l in fn_widget.findChildren(QLabel))
            assert '[FN]' in fn_labels  # nested element still gets its own header

            # Element views use the same coding tab bar as their parent file: no secondary menu or
            # panel. A different element gets its own tab; reopening the same one reuses it.
            alpha_section = None
            beta_section = None
            it = tree_window.tree.invisibleRootItem()
            stack = [it.child(i) for i in range(it.childCount())]
            while stack:
                candidate = stack.pop()
                if candidate.data(0, ROLE_KIND) == 'section':
                    text = tree_window.tree.itemWidget(candidate, 0).text()
                    if 'alpha' in text:
                        alpha_section = candidate
                    elif 'beta' in text:
                        beta_section = candidate
                for i in range(candidate.childCount()):
                    stack.append(candidate.child(i))
            assert alpha_section is not None and beta_section is not None
            assert not hasattr(tree_window, 'split_tabs')  # one native coding tab bar, no ad-hoc menu
            main_filter_before = legacy_tab.filter_uid
            tab_count_before = tree_window.tabs.count()
            tree_window._on_tree_item_clicked(alpha_section, 0)
            assert tree_window.tabs.count() == tab_count_before  # replaces the unrelated unpinned preview
            assert legacy_tab.filter_uid == main_filter_before  # main pane untouched
            alpha_page = tree_window.tabs.currentWidget()
            assert '[FN]' in alpha_page._tab_label.text() and 'alpha' in alpha_page._tab_label.text()
            assert alpha_page._file_tab is legacy_tab
            split_codes = [c.toPlainText().strip() for c in alpha_page.findChildren(CodeEdit)]
            assert split_codes == ['def alpha(): pass']

            # double-clicking the SAME element again switches to its existing tab, no duplicate
            tree_window._on_tree_item_double_clicked(alpha_section, 0)
            assert tree_window.tabs.count() == tab_count_before
            assert tree_window.tabs.currentWidget() is alpha_page

            # alpha's tab is still the one reusable "preview" tab (VS Code-style: a single click
            # opens a page in a reused, unpinned preview slot instead of piling up a new tab per
            # click) until it's explicitly pinned — pin it here so the next different element
            # below genuinely opens its own tab instead of retargeting this same preview page
            tree_window._pin_element_page(alpha_page)

            # a DIFFERENT element (same parent module) opens its own tab alongside the first
            tree_window._on_tree_item_clicked(beta_section, 0)
            assert tree_window.tabs.count() == tab_count_before + 1
            beta_page = tree_window.tabs.currentWidget()
            assert '[FN]' in beta_page._tab_label.text() and 'beta' in beta_page._tab_label.text()
            assert tree_window.active_tab is legacy_tab and tree_window._active_filter_uid() == beta_page._element_key[1]
            beta_codes = [c.toPlainText().strip() for c in beta_page.findChildren(CodeEdit)]
            assert beta_codes == ['def beta(): pass']
            assert tree_window.tabs.indexOf(alpha_page) != -1  # first tab untouched by the second
            assert 'beta' in tree_window._build_ai_focus_summary()
            assert 'beta' in tree_window._build_ai_context_hint()
            assert tree_window.filename_label.text() == '[FN] beta'

            # Both tabs are views of one document: switching refreshes the destination from the
            # shared tree instead of exposing a stale second copy of the file.
            beta_page.findChildren(CodeEdit)[0].setPlainText('def beta(): return 1')
            tree_window.tabs.setCurrentWidget(legacy_tab)
            assert 'def beta(): return 1' in [c.toPlainText().strip() for c in legacy_tab.findChildren(CodeEdit)]
            tree_window.tabs.setCurrentWidget(beta_page)
            assert beta_page.findChildren(CodeEdit)[0].toPlainText().strip() == 'def beta(): return 1'

            tree_window._close_tab(tree_window.tabs.indexOf(alpha_page))
            assert tree_window.tabs.indexOf(alpha_page) == -1
            assert tree_window.tabs.indexOf(beta_page) != -1

            # closing the tab the remaining split page is showing must close that page too, not
            # leave it pointing at a tab that's been torn down
            tree_window._close_tab(tree_window.tabs.indexOf(legacy_tab), flush=False)
            assert tree_window.tabs.indexOf(beta_page) == -1
            assert not any(page._file_tab is legacy_tab for page in tree_window._element_pages.values())
            tree_window.close()

    def test_mappa_geometry_drag_reorder_and_tab_label_leak(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            source_dir = root / 'src'
            source_dir.mkdir()
            app = self.app
            # the MAPPA window spans the full page: left/right edges of the main window, top just
            # under the action toolbar (Save row), bottom just above the status bar (UTF-8 row) —
            # needs a real shown/laid-out window, unlike other tests, since it reads real on-screen
            # positions via mapToGlobal. Clears any windowGeometry saved by an earlier run first —
            # MainWindow.__init__ restores it, which would otherwise override the resize below once
            # the window is actually realized on screen.
            # wide enough that the MAPPA toolbar's own minimum content width (many buttons) never
            # forces the dialog wider than the window — otherwise Qt can't honor the requested width
            QSettings('KANT', 'KANT Editor').remove('windowGeometry')
            mappa_window = MainWindow()
            mappa_window.resize(2000, 900)
            mappa_window.show()
            app.processEvents()
            window_width_at_show = mappa_window.width()  # same moment showEvent reads parent.width()
            mappa_window.project_root_path = str(root)
            mappa_window._open_xref_window()
            app.processEvents()
            dialog = mappa_window.map_dialog
            toolbar_bottom = mappa_window.action_toolbar.mapToGlobal(mappa_window.action_toolbar.rect().bottomLeft()).y()
            status_top = mappa_window.statusBar().mapToGlobal(mappa_window.statusBar().rect().topLeft()).y()
            assert abs(dialog.geometry().top() - toolbar_bottom) <= 1
            # Qt's offscreen platform plugin can retain a few pixels of invisible top-level frame
            # margin — up to ~5px observed on Windows, up to ~8px on Linux; not the same value
            # across platforms, so the tolerance has to cover the largest observed rather than
            # either one specifically.
            assert abs(dialog.geometry().bottom() - status_top) <= 10, (
                dialog.geometry().bottom(), status_top, dialog.geometry(), mappa_window.geometry()
            )
            assert dialog.width() == window_width_at_show

            # the MAPPA tab sits centered on the dialog's own top edge while open (pointing down
            # at the map content below it), not the shell's bottom edge (already the title/toolbar)
            assert mappa_window.map_tab_btn.parent() is dialog
            assert mappa_window.map_tab_btn.property('kantIcon') == 'arrow-down'
            assert mappa_window.map_tab_btn.y() == 0
            expected_x = (dialog.width() - mappa_window.map_tab_btn.width()) // 2
            assert abs(mappa_window.map_tab_btn.x() - expected_x) <= 1

            # regression: the alignment used to be computed once (a _positioned flag in
            # XrefMapDialog.showEvent) and never redone — resizing the main window, closing MAPPA,
            # and reopening it kept the stale old geometry. Positioning now lives in MainWindow
            # (_position_map_dialog), called on every open, not just the first.
            mappa_window._toggle_xref_window()  # close
            app.processEvents()
            # closed, the tab goes back to the shell's own bottom edge instead
            assert mappa_window.map_tab_btn.parent() is mappa_window.shell
            assert mappa_window.map_tab_btn.property('kantIcon') == 'arrow-up'
            assert mappa_window.map_tab_btn.y() == mappa_window.shell.height() - mappa_window.map_tab_btn.height()
            # must stay above the MAPPA toolbar's own minimum content width (many buttons), same
            # constraint as window_width_at_show above, or Qt can't honor the narrower resize
            mappa_window.resize(2300, 1000)
            app.processEvents()
            mappa_window._open_xref_window()  # reopen
            app.processEvents()
            new_toolbar_bottom = mappa_window.action_toolbar.mapToGlobal(mappa_window.action_toolbar.rect().bottomLeft()).y()
            new_status_top = mappa_window.statusBar().mapToGlobal(mappa_window.statusBar().rect().topLeft()).y()
            assert dialog.width() == mappa_window.width()
            assert abs(dialog.geometry().top() - new_toolbar_bottom) <= 1
            assert abs(dialog.geometry().bottom() - new_status_top) <= 1

            # regression: dragging a tab by its rich-HTML label used to be swallowed entirely (the
            # label accepted every press without forwarding to the QTabBar), breaking setMovable(True)
            # tabs' built-in reorder gesture from the labeled region — now the exact same press/move/
            # release sequence is forwarded to the tab bar so its native drag still fires.
            tag_a = source_dir / 'taga.py'
            tag_b = source_dir / 'tagb.py'
            tag_a.write_text('# [MOD OPEN] taga.py\nx=1\n# [MOD CLOSED] taga.py\n', encoding='utf-8')
            tag_b.write_text('# [MOD OPEN] tagb.py\nx=1\n# [MOD CLOSED] tagb.py\n', encoding='utf-8')
            mappa_window._open_file(str(tag_a))
            # pin it: otherwise it's just the unpinned preview slot, and opening tag_b below would
            # reuse (close) this tab instead of leaving two real, independently draggable tabs
            mappa_window._pin_file_tab(mappa_window.open_tabs[str(tag_a)])
            mappa_window._open_file(str(tag_b))
            app.processEvents()
            tab_a = mappa_window.open_tabs[str(tag_a)]
            tab_b = mappa_window.open_tabs[str(tag_b)]
            idx_a, idx_b = mappa_window.tabs.indexOf(tab_a), mappa_window.tabs.indexOf(tab_b)
            assert idx_a < idx_b  # opened in this order
            label_a = tab_a._tab_label
            start = label_a.rect().center()
            mid = label_a.mapFromGlobal(tab_b._tab_label.mapToGlobal(tab_b._tab_label.rect().center()))
            QTest.mousePress(label_a, Qt.LeftButton, Qt.NoModifier, start)
            app.processEvents()
            QTest.mouseMove(label_a, mid)
            app.processEvents()
            QTest.mouseRelease(label_a, Qt.LeftButton, Qt.NoModifier, mid)
            app.processEvents()
            assert mappa_window.tabs.indexOf(tab_a) > mappa_window.tabs.indexOf(tab_b)  # actually reordered

            # regression: transitioning a tab from tagged to untagged used to drop the old _tab_label
            # widget's only Python reference without deleteLater() — an orphaned hidden QLabel per
            # transition, never freed until the whole window closed
            tag_a.write_text('x = 1\n', encoding='utf-8')  # strip the KANT tag entirely
            mappa_window._on_fs_file_changed(str(tag_a))
            mappa_window._update_tab_title(tab_a)
            assert tab_a._tab_label is None
            final_idx_a = mappa_window.tabs.indexOf(tab_a)  # reorder above may have moved it
            assert mappa_window.tabs.tabBar().tabButton(final_idx_a, QTabBar.LeftSide) is None
            mappa_window.close()

    def test_auto_resize_survives_deleted_cpp_object(self):
        # regression: horizontalScrollBar().rangeChanged defers _auto_resize via
        # QTimer.singleShot(0, self._auto_resize) — a bound-method singleShot callback, unlike a
        # direct signal/slot connection, is NOT auto-disconnected when its target is destroyed. If
        # the tab closes between scheduling and firing, the real crash was "RuntimeError: Internal
        # C++ object (CodeEdit) already deleted" from inside _auto_resize. shiboken6.delete()
        # reproduces that exact state (Python wrapper alive, C++ side gone) without needing a real
        # deferred-timer race.
        edit = CodeEdit('x = 1')
        shiboken6.delete(edit)
        assert not shiboken6.isValid(edit)
        edit._auto_resize()  # must not raise

    def test_undo_redo_and_mark_dirty(self):
        with _temp_dir() as tmp:
            source_dir = Path(tmp) / 'src'
            source_dir.mkdir()
            source = _write_app_py(source_dir)
            tab = FileTab(str(source), parse_kant(source.read_text(encoding='utf-8')))
            top = next(node for node in tab.tree.body if hasattr(node, 'body'))
            run = next(item for item in top.body if isinstance(item, Run))
            tab.remember_undo_state()
            run.lines = ['print(2)']
            assert tab.undo_file() and 'print(1)' in serialize_kant(tab.tree)
            assert tab.redo_file() and 'print(2)' in serialize_kant(tab.tree)

            # mark_dirty() must only emit dirtyChanged on the false->true edge — it's wired to fire on
            # every keystroke (CodeEdit.textChanged -> _on_code_changed -> mark_dirty), and dirtyChanged
            # cascades into a full tab-title HTML rebuild + QTabBar relayout, so re-emitting on every
            # subsequent keystroke while already dirty redid all of that for no actual change
            tab.dirty = False
            dirty_emits = []
            tab.dirtyChanged.connect(lambda: dirty_emits.append(1))
            tab.mark_dirty()
            assert tab.dirty and len(dirty_emits) == 1
            tab.mark_dirty()
            tab.mark_dirty()
            assert len(dirty_emits) == 1  # still dirty from the first call -> no further emits
            tab.autosave_timer.stop()

    def _make_lsp_window(self, root):
        lsp_window = MainWindow()
        lsp_window.project_root_path = str(root)
        lsp_window._render_view = lambda *_args, **_kwargs: None
        lsp_window._update_tab_title = lambda *_args, **_kwargs: None
        lsp_window._update_filename_label = lambda *_args, **_kwargs: None
        lsp_window._update_lsp_diagnostics = lambda *_args, **_kwargs: None
        lsp_window._ide_message = lambda *_args, **_kwargs: None
        return lsp_window

    def test_lsp_local_text_edits_rename_format_and_offset(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            source_dir = root / 'src'
            source_dir.mkdir()
            source = _write_app_py(source_dir)
            lsp_window = self._make_lsp_window(root)
            lsp_tab = FileTab(str(source), parse_kant(source.read_text(encoding='utf-8')))
            lsp_window._apply_lsp_text_edits(lsp_tab, [{
                'range': {'start': {'line': 3, 'character': 6}, 'end': {'line': 3, 'character': 7}},
                'newText': '3',
            }])
            assert 'print(3)' in serialize_kant(lsp_tab.tree)
            lsp_window._local_rename_in_tab(lsp_tab, 'print', 'echo')
            assert 'echo(3)' in serialize_kant(lsp_tab.tree)
            run = next(item for node in lsp_tab.tree.body if hasattr(node, 'body') for item in node.body if isinstance(item, Run))
            run.lines = ['echo(3)   ']
            lsp_window._local_format(lsp_tab)
            assert 'echo(3)   ' not in serialize_kant(lsp_tab.tree)
            lsp_tab.autosave_timer.stop()
            assert lsp_window._offset_for_lsp_position('😀x\n', {'line': 0, 'character': 2}) == 1
            lsp_window.close()

    def test_lsp_workspace_edits(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            source_dir = root / 'src'
            source_dir.mkdir()
            lsp_window = self._make_lsp_window(root)
            rename_source = source_dir / 'rename_source.py'
            rename_source.write_text('old()\n', encoding='utf-8')
            other = source_dir / 'other.py'
            other.write_text('old()\n', encoding='utf-8')
            lsp_window.open_tabs = {}
            lsp_window._invalidate_xref = lambda: None
            lsp_window._apply_lsp_workspace_edits({'changes': {
                file_uri(rename_source): [{
                    'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 3}},
                    'newText': 'new',
                }],
                file_uri(other): [{
                    'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 3}},
                    'newText': 'new',
                }],
            }})
            assert rename_source.read_text(encoding='utf-8') == 'new()\n'
            assert other.read_text(encoding='utf-8') == 'new()\n'
            lsp_window.close()

    def test_autocomplete_local_and_lsp_driven(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            app = self.app
            lsp_window = self._make_lsp_window(root)

            # autocomplete-as-you-type: typing restarts a debounce timer that asks completion_provider
            # (wired by mainwindow to _request_completion) for fresh candidates
            completion_edit = CodeEdit('')
            completion_edit.show()
            completion_edit.setFocus()
            app.processEvents()
            completion_calls = []
            completion_edit.completion_provider = lambda e: completion_calls.append(e)
            QTest.keyClicks(completion_edit, 'ab')
            assert completion_edit._completion_timer.isActive()
            completion_edit._trigger_completion()
            assert completion_calls == [completion_edit]

            # local (no-LSP-server) fallback: candidates are identifiers already in the open file.
            # Shown as inline ghost text (just the remaining suffix), not a popup list — Tab accepts.
            no_lsp_tab_tree = parse_kant('# [FN OPEN] x\ndef alpha_function():\n    pass\n# [FN CLOSED] x\n')
            lsp_window.tabs = type('Tabs', (), {
                'currentWidget': lambda _self: type('T', (), {'tree': no_lsp_tab_tree})(),
            })()
            local_completion_edit = CodeEdit('    alp')
            cursor = local_completion_edit.textCursor()
            cursor.movePosition(QTextCursor.End)
            local_completion_edit.setTextCursor(cursor)
            local_completion_edit.show()
            local_completion_edit.setFocus()
            app.processEvents()
            lsp_window._local_completion(local_completion_edit)
            assert local_completion_edit._ghost_suggestion == 'ha_function'
            local_completion_edit._accept_ghost_suggestion()
            assert local_completion_edit.toPlainText() == '    alpha_function'
            assert local_completion_edit._ghost_suggestion == ''

            # LSP-driven path: _apply_completion_result dedupes items and forwards labels; the first
            # one that actually extends the typed prefix becomes the ghost suggestion
            lsp_completion_edit = CodeEdit('a')
            cursor = lsp_completion_edit.textCursor()
            cursor.movePosition(QTextCursor.End)
            lsp_completion_edit.setTextCursor(cursor)
            lsp_completion_edit.show()
            lsp_completion_edit.setFocus()
            app.processEvents()
            lsp_window._apply_completion_result(lsp_completion_edit, {'items': [
                {'label': 'alpha', 'insertText': 'alpha'}, {'label': 'alpha'}, {'label': 'abc'},
            ]})
            assert lsp_completion_edit._ghost_suggestion == 'lpha'

            # any further typing invalidates a pending suggestion immediately, not just after the
            # next debounced completion request resolves
            QTest.keyClicks(lsp_completion_edit, 'x')
            assert lsp_completion_edit._ghost_suggestion == ''
            lsp_window.close()

    def test_hover_tooltip_and_gesture_vocabulary(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            app = self.app
            lsp_window = self._make_lsp_window(root)
            no_lsp_tab_tree = parse_kant('# [FN OPEN] x\ndef alpha_function():\n    pass\n# [FN CLOSED] x\n')
            lsp_window.tabs = type('Tabs', (), {
                'currentWidget': lambda _self: type('T', (), {'tree': no_lsp_tab_tree})(),
            })()
            local_completion_edit = CodeEdit('    alpha_function')
            cursor = local_completion_edit.textCursor()
            cursor.movePosition(QTextCursor.End)  # symbol lookup below reads the identifier at the cursor
            local_completion_edit.setTextCursor(cursor)
            local_completion_edit.show()
            local_completion_edit.setFocus()
            app.processEvents()

            # quick-doc-on-hover: PyCharm-style tooltip on the symbol under the mouse, no click needed
            assert lsp_window._symbol_at_cursor(local_completion_edit.textCursor()) == 'alpha_function'
            lsp_completion_edit = CodeEdit('a')
            lsp_completion_edit.show()
            lsp_completion_edit.setFocus()
            app.processEvents()
            lsp_window.lsp_hover_requests[999] = (lsp_completion_edit, lsp_completion_edit.mapToGlobal(lsp_completion_edit.rect().center()))
            lsp_window._on_lsp_response(999, 'textDocument/hover', {'contents': {'value': 'def f() -> None'}})
            # the hover popup is a themed widget now (_CodeHoverPopup), not the OS-native QToolTip —
            # see kant/widgets.py's show_code_hover_popup/hide_code_hover_popup
            assert 'def f()' in _code_hover_popup_instance[0].label.text()
            assert 999 not in lsp_window.lsp_hover_requests

            # local fallback: no LSP server configured -> definition-location lookup shown as tooltip
            lsp_window._local_hover(local_completion_edit, local_completion_edit.mapToGlobal(local_completion_edit.rect().center()), 'alpha_function')
            assert 'alpha_function' in _code_hover_popup_instance[0].label.text()

            # a real mouse move restarts the hover debounce, which then calls hover_provider
            hover_calls = []
            local_completion_edit.hover_provider = lambda e, c, p: hover_calls.append(e)
            center = QPointF(local_completion_edit.rect().center())
            move_event = QMouseEvent(
                QEvent.MouseMove, center, local_completion_edit.mapToGlobal(center.toPoint()),
                Qt.NoButton, Qt.NoButton, Qt.NoModifier,
            )
            local_completion_edit.mouseMoveEvent(move_event)
            assert local_completion_edit._hover_timer.isActive()
            local_completion_edit._trigger_hover()
            assert hover_calls == [local_completion_edit]

            # gesture vocabulary: Ctrl+Click jumps to definition, F2 renames — matching the vocabulary
            # every other IDE uses instead of only exposing these through the LSP menu
            gesture_calls = []
            local_completion_edit.definition_provider = lambda e: gesture_calls.append('definition')
            local_completion_edit.rename_provider = lambda e: gesture_calls.append('rename')
            click_pos = QPointF(local_completion_edit.rect().center())
            ctrl_click = QMouseEvent(
                QEvent.MouseButtonPress, click_pos, local_completion_edit.mapToGlobal(click_pos.toPoint()),
                Qt.LeftButton, Qt.LeftButton, Qt.ControlModifier,
            )
            local_completion_edit.mousePressEvent(ctrl_click)
            assert gesture_calls == ['definition']
            gesture_calls.clear()
            plain_click = QMouseEvent(
                QEvent.MouseButtonPress, click_pos, local_completion_edit.mapToGlobal(click_pos.toPoint()),
                Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
            )
            local_completion_edit.mousePressEvent(plain_click)
            assert gesture_calls == []  # a plain click must not also jump to definition
            f2_event = QKeyEvent(QEvent.KeyPress, Qt.Key_F2, Qt.NoModifier)
            local_completion_edit.keyPressEvent(f2_event)
            assert gesture_calls == ['rename']
            lsp_window.close()

    def test_normalize_ai_text_strips_ansi_and_decodes_utf8(self):
        # the claude/codex CLIs are UTF-8 regardless of the OS locale, and colorize their own
        # stdout — decoding with locale.getpreferredencoding() (a Windows ANSI codepage, not UTF-8)
        # corrupted any accented letter, emoji, or box-drawing glyph; raw ANSI codes rendered as
        # garbage glyphs instead of being invisible
        accented = 'caffè è pronto ☕'.encode('utf-8')
        assert _normalize_ai_text(accented) == 'caffè è pronto ☕'

        colored = b'\x1b[32mOK\x1b[0m: \x1b[1mdone\x1b[0m'
        assert _normalize_ai_text(colored) == 'OK: done'

        # a lone \r (progress-bar/spinner overwrite) is dropped; \r\n is normalized to \n
        assert _normalize_ai_text(b'line1\rline2\r\nline3') == 'line1line2\nline3'

    def test_markdown_to_html_renders_chat_formatting(self):
        assert _markdown_to_html('**bold** and *italic* and `code`') == (
            '<b>bold</b> and <i>italic</i> and '
            f'<code style="background:{theme.CODE_BG}; padding:1px 4px; border-radius:3px;">code</code>'
        )
        fenced = _markdown_to_html('before\n```python\nx < 1 and y & 2\n```\nafter')
        assert '<pre' in fenced and 'x &lt; 1 and y &amp; 2' in fenced
        assert 'before<br>' in fenced and '<br>after' in fenced
        # bullet lines get a visible marker; both `-` and `*` list markers are recognized
        bullets = _markdown_to_html('- uno\n* due')
        assert bullets.count('•') == 2
        # raw HTML in the message text must render literally, never as live markup — this is AI
        # output rendered as rich text, so unescaped `<`/`&` must not be interpreted as HTML
        assert _markdown_to_html('<script>alert(1)</script> & "x"') == '&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;'
        # markdown syntax INSIDE a fenced/inline code span must render literally, not be
        # re-interpreted as bold/italic
        assert '<b>' not in _markdown_to_html('```\n**not bold**\n```')
        assert '<i>' not in _markdown_to_html('`*not italic*`')
        # a single _ inside an identifier (add_item, cart_id — extremely common in any code-related
        # message) must never be read as italic emphasis; a real _word_ still is
        assert '<i>' not in _markdown_to_html('add_item(cart_id, quantity)')
        assert _markdown_to_html('_italic_') == '<i>italic</i>'
        # a GFM pipe table (header + |---|---| separator + body rows) renders as a real <table>,
        # with bold/inline-code inside cells still applied (these substitutions already ran on the
        # whole segment before table rows are split out)
        table = _markdown_to_html('| Fn | Does |\n|---|---|\n| add_item(a, b) | **adds** stock |')
        assert table.count('<table') == 1 and table.count('<th') == 2 and table.count('<td') == 2
        assert '<b>adds</b>' in table and 'add_item(a, b)' in table

    def test_app_icon_loads_from_bundled_asset(self):
        # guards kant/assets/app_icon.png staying present and readable — make_app_icon/pixmap load
        # the user's own image file directly (QIcon(path)/QPixmap(path)), not a generated fallback,
        # so a missing/renamed file would otherwise fail silently (QIcon.isNull()) with no test to
        # catch it before someone notices the taskbar/home icon went blank
        assert not make_app_icon().isNull()
        pixmap = make_app_pixmap(76)
        assert not pixmap.isNull() and pixmap.width() == 76 and pixmap.height() == 76

        splash = kant_editor._make_splash()
        splash.show()
        QApplication.processEvents()
        assert splash.isVisible() and splash.pixmap().width() == 320
        splash.close()

    def test_xref_edges_ignore_comments_and_strings(self):
        xref_tree = parse_kant('\n'.join([
            '# [FN OPEN] alpha', 'def alpha():', '    """beta()"""',
            '    /* beta() */', '# [FN CLOSED] alpha',
            '# [FN OPEN] beta', 'def beta(): pass', '# [FN CLOSED] beta',
        ]))
        xref = build_xref({'sample.py': xref_tree})
        alpha = next(element for element in xref.values() if element.name == 'alpha')
        assert alpha.outgoing == []

    def test_incoming_outgoing_aggregation(self):
        with _temp_dir() as tmp:
            io_dir = Path(tmp) / 'io-project'
            io_dir.mkdir()
            module_tree = parse_kant('\n'.join([
                '# [MOD OPEN #m1] module.py',
                '# [FN OPEN #f1] alpha', 'def alpha():', '    helper()', '# [FN CLOSED #f1] alpha',
                '# [FN OPEN #f2] beta', 'def beta(): pass', '# [FN CLOSED #f2] beta',
                '# [MOD CLOSED #m1] module.py',
            ]))
            external_tree = parse_kant('\n'.join([
                '# [FN OPEN #f3] helper', 'def helper():', '    alpha()', '# [FN CLOSED #f3] helper',
            ]))
            io_xref = build_xref({'module.py': module_tree, 'external.py': external_tree})
            io_window = MainWindow.__new__(MainWindow)
            io_window.project_root_path = str(io_dir)
            io_fake_tab = type('Tab', (), {'path': str(io_dir / 'module.py'), 'tree': module_tree})()
            io_window.tabs = type('Tabs', (), {'currentWidget': lambda _self: io_fake_tab})()
            io_window._get_xref = lambda: io_xref
            io_window.incoming_view = QListWidget()
            io_window.outgoing_view = QListWidget()
            module_uid = next(item.uid for item in module_tree.body if isinstance(item, Node))
            MainWindow._update_io_tabs(io_window, module_uid)
            # selecting the module aggregates its children's (alpha's) references, not just the
            # module's own empty direct incoming/outgoing. The item itself carries no text (see
            # _update_io_tabs's fill()) — only the widget set via setItemWidget is drawn, so
            # visible content is checked there, not on the item.
            assert io_window.incoming_view.count() == 1 and not io_window.incoming_view.item(0).text()
            assert io_window.outgoing_view.count() == 1 and not io_window.outgoing_view.item(0).text()
            incoming_label = io_window.incoming_view.itemWidget(io_window.incoming_view.item(0))
            outgoing_label = io_window.outgoing_view.itemWidget(io_window.outgoing_view.item(0))
            assert 'helper' in incoming_label.text() and 'helper' in outgoing_label.text()
            assert '[FN]' in incoming_label.text() and 'border-bottom:2px solid' in incoming_label.styleSheet()
            # the whole-file view (uid=None — file tree item, or a tab with no section filter) is the
            # same module element, not "nothing selected"; it must aggregate the same way
            io_window.incoming_view.clear()
            io_window.outgoing_view.clear()
            MainWindow._update_io_tabs(io_window, None)
            assert io_window.incoming_view.count() == 1 and not io_window.incoming_view.item(0).text()
            assert io_window.outgoing_view.count() == 1 and not io_window.outgoing_view.item(0).text()
            incoming_label = io_window.incoming_view.itemWidget(io_window.incoming_view.item(0))
            outgoing_label = io_window.outgoing_view.itemWidget(io_window.outgoing_view.item(0))
            assert 'helper' in incoming_label.text() and 'helper' in outgoing_label.text()

    def test_xref_map_view_and_dialog_interactions(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            app = self.app
            graph = {
                'a': XrefElement('a', 'a', 'FN', 'a', 'A', 'a.py', 0, outgoing=['b']),
                'b': XrefElement('b', 'b', 'FN', 'b', 'B', 'b.py', 0, incoming=['a']),
            }
            assert _force_layout_positions(graph) == _force_layout_positions(graph)
            flow_graph = {
                'source': XrefElement('source', 'source', 'FN', 'source', 'Source', 'source.py', 0, outgoing=['middle']),
                'middle': XrefElement('middle', 'middle', 'FN', 'middle', 'Middle', 'middle.py', 0, incoming=['source'], outgoing=['target']),
                'target': XrefElement('target', 'target', 'FN', 'target', 'Target', 'target.py', 0, incoming=['middle']),
            }
            flow_positions = _force_layout_positions(flow_graph)
            assert flow_positions['source'][0] < flow_positions['middle'][0] < flow_positions['target'][0]
            map_view = XrefMapView()
            moved = []
            map_view.nodeMoved.connect(lambda *args: moved.append(args))
            map_view.set_data(graph)
            containment_graph = {
                'parent': XrefElement('parent', 'parent', 'CLS', 'parent', 'Parent', 'a.py', 0),
                'child': XrefElement('child', 'child', 'FN', 'child', 'Child', 'a.py', 1, parent='parent'),
                'other': XrefElement('other', 'other', 'FN', 'other', 'Other', 'b.py', 0),
            }
            map_view.set_data(containment_graph)
            map_view.select('child')
            assert map_view._node_items['parent'].opacity() == 1.0
            assert map_view._node_items['other'].opacity() == 0.18
            map_view.set_data(graph)
            assert all(item.flags() & QGraphicsItem.ItemIsMovable for item in map_view._node_items.values())
            old_edge = map_view._edges[0][2].path().boundingRect()
            map_view._node_items['a'].moveBy(100, 60)
            assert moved and map_view._edges[0][2].path().boundingRect() != old_edge
            saved_positions = map_view.positions()
            map_view.set_data(graph, saved_positions)
            assert map_view.positions() == saved_positions
            hovered_edges, pinned_edges = [], []
            map_view.edgeHovered.connect(lambda *args: hovered_edges.append(args))
            map_view.edgePinned.connect(lambda *args: pinned_edges.append(args))
            map_view.resize(800, 500)
            map_view.show()
            map_view.fit()
            app.processEvents()
            edge_point = map_view.mapFromScene(map_view._edges[0][2].path().pointAtPercent(0.5))
            QTest.mouseMove(map_view.viewport(), edge_point)
            QTest.mouseClick(map_view.viewport(), Qt.LeftButton, Qt.NoModifier, edge_point)
            app.processEvents()
            assert hovered_edges and pinned_edges
            map_view.close()
            map_dialog = XrefMapDialog()
            map_dialog.set_graph(graph, 'test', str(root / 'map-project'))
            map_dialog.resize(900, 650)
            map_dialog.show()
            app.processEvents()
            edge_scene_point = map_dialog.view._edges[0][2].path().pointAtPercent(0.5)
            map_dialog._on_edge_hovered('a', 'b', edge_scene_point, True)
            assert map_dialog._pending_hover == ('edge', ('a', 'b', edge_scene_point))  # shows only after a delay
            map_dialog._show_pending_hover()  # simulate the delay timer firing
            assert 'INCOMING' in map_dialog.edge_popup.incoming.text()
            assert 'OUTGOING' in map_dialog.edge_popup.outgoing.text()
            # hovering a node shows that element's own incoming/outgoing, same popup mechanism
            node_scene_point = map_dialog.view._node_items['a'].sceneBoundingRect().center()
            map_dialog._on_node_hovered('a', node_scene_point, True)
            map_dialog._show_pending_hover()
            assert 'OUTGOING' in map_dialog.edge_popup.outgoing.text()
            map_dialog._on_edge_pinned('a', 'b', edge_scene_point)
            assert map_dialog._pinned_edge == ('a', 'b')
            map_dialog._on_edge_pinned('a', 'b', edge_scene_point)
            assert map_dialog._pinned_edge is None and map_dialog.edge_popup.isHidden()
            map_dialog.view._node_items['a'].moveBy(45, 25)
            persisted = map_dialog.view.positions()['a']
            map_dialog._save_positions()
            restored_dialog = XrefMapDialog()
            restored_dialog.set_graph(graph, 'test', str(root / 'map-project'))
            assert restored_dialog.view.positions()['a'] == persisted
            map_dialog.close()
            restored_dialog.close()
            QSettings('KANT', 'KANT Editor').remove(map_dialog._position_key)

    def test_force_layout_positions_no_overlap(self):
        # the median-Y edge-crossing-reduction pass (added alongside the higher iteration floor)
        # nudges nodes toward their neighbours' median y — on a moderately connected graph that can
        # crowd unrelated, x-overlapping nodes into the same y band; resolve_overlaps() must always
        # clean that up afterward so the "boxes never actually overlap" guarantee still holds
        import random
        rng = random.Random(7)
        count = 60
        graph = {
            f'e{i}': XrefElement(f'e{i}', f'e{i}', 'FN', f'e{i}', f'E{i}', f'file{i % 8}.py', 0)
            for i in range(count)
        }
        for i in range(count):
            source = graph[f'e{i}']
            for _ in range(rng.randint(0, 3)):
                target_key = f'e{rng.randint(0, count - 1)}'
                if target_key != f'e{i}':
                    source.outgoing.append(target_key)
                    graph[target_key].incoming.append(f'e{i}')
        positions = _force_layout_positions(graph)
        max_degree = max(_element_degree(e, graph, None) for e in graph.values())
        sizes = {key: _element_size(el, max_degree, graph, None) for key, el in graph.items()}
        keys = list(positions)
        for i, left in enumerate(keys):
            for right in keys[i + 1:]:
                lx, ly = positions[left]
                lw, lh = sizes[left]
                rx, ry = positions[right]
                rw, rh = sizes[right]
                overlapping = lx < rx + rw and lx + lw > rx and ly < ry + rh and ly + lh > ry
                assert not overlapping, f'{left} and {right} overlap: {positions[left]} vs {positions[right]}'
                gap_x = max(rx - lx - lw, lx - rx - rw, 0)
                gap_y = max(ry - ly - lh, ly - ry - rh, 0)
                assert gap_x ** 2 + gap_y ** 2 >= (MIN_NODE_GAP - 0.02) ** 2

    def test_xref_map_dialog_expand_collapse(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            app = self.app
            module_graph = {
                'module': XrefElement('module', 'module', 'MOD', 'module.py', 'Modulo', 'module.py', 0),
                'child': XrefElement('child', 'child', 'FN', 'work', 'Funzione', 'module.py', 1),
            }
            expand_dialog = XrefMapDialog()
            expand_dialog.resize(900, 650)
            expand_dialog.set_graph(module_graph, 'expand', str(root / 'expand-project'))
            expand_dialog.show()
            app.processEvents()
            assert len(expand_dialog._display) == 2  # every open starts fully expanded
            node = expand_dialog.view._node_items['module']
            click = expand_dialog.view.mapFromScene(node.sceneBoundingRect().center())
            scale_before = expand_dialog.view.transform().m11()
            center_before = expand_dialog.view.mapToScene(expand_dialog.view.viewport().rect().center())
            QTest.mouseDClick(expand_dialog.view.viewport(), Qt.LeftButton, Qt.NoModifier, click)
            app.processEvents()
            assert len(expand_dialog._display) == 1  # double-click on an expanded module collapses it
            assert abs(expand_dialog.view.transform().m11() - scale_before) < 0.001
            center_after = expand_dialog.view.mapToScene(expand_dialog.view.viewport().rect().center())
            assert abs(center_after.x() - center_before.x()) < 2 and abs(center_after.y() - center_before.y()) < 2
            node = expand_dialog.view._node_items['module']
            click = expand_dialog.view.mapFromScene(node.sceneBoundingRect().center())
            QTest.mouseDClick(expand_dialog.view.viewport(), Qt.LeftButton, Qt.NoModifier, click)
            app.processEvents()
            assert len(expand_dialog._display) == 2  # double-click again re-expands it
            expand_dialog.close()
            QSettings('KANT', 'KANT Editor').remove(expand_dialog._position_key)

    def test_xref_map_loading_spinner_shows_until_real_data_arrives(self):
        # regression: opening MAPPA before its background xref build ever finished used to just
        # show an empty graph with no explanation — set_graph(loading=True) now shows a spinner
        # instead, and any later call (real data) implicitly clears it
        with _temp_dir() as tmp:
            app = self.app
            dialog = XrefMapDialog()
            dialog.resize(700, 500)
            dialog.show()
            app.processEvents()
            assert not dialog.loading_spinner.isVisible()
            dialog.set_graph({}, 'loading-project', str(Path(tmp) / 'loading-project'), loading=True)
            app.processEvents()
            assert dialog.loading_spinner.isVisible()
            module_graph = {'m': XrefElement('m', 'm', 'MOD', 'm.py', 'Modulo', 'm.py', 0)}
            dialog.set_graph(module_graph, 'loading-project', str(Path(tmp) / 'loading-project'))
            app.processEvents()
            assert not dialog.loading_spinner.isVisible()
            dialog.close()
            QSettings('KANT', 'KANT Editor').remove(dialog._position_key)

    def test_xref_map_module_edges_aggregate_but_hidden_by_default(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            app = self.app
            # two files, each with a module root and two children; only the CHILDREN reference
            # across files (the module roots themselves have no direct edge of their own) — this is
            # exactly the case that needs aggregation: a real module-to-module connection only
            # exists once the underlying elements' in/out are summed onto the collapsed roots.
            graph = {
                'mod_a': XrefElement('mod_a', 'mod_a', 'MOD', 'a.py', 'Modulo A', 'a.py', 0),
                'a1': XrefElement('a1', 'a1', 'FN', 'a1', 'A uno', 'a.py', 1, outgoing=['b1']),
                'a2': XrefElement('a2', 'a2', 'FN', 'a2', 'A due', 'a.py', 2, outgoing=['b2']),
                'mod_b': XrefElement('mod_b', 'mod_b', 'MOD', 'b.py', 'Modulo B', 'b.py', 0),
                'b1': XrefElement('b1', 'b1', 'FN', 'b1', 'B uno', 'b.py', 1, incoming=['a1']),
                'b2': XrefElement('b2', 'b2', 'FN', 'b2', 'B due', 'b.py', 2, incoming=['a2']),
            }
            dialog = XrefMapDialog()
            dialog.resize(900, 650)
            # MOD connections must start disabled — the map should open showing individual
            # element-level references, not module-to-module summary arrows
            assert 'MOD' not in dialog._active_edge_tags
            assert dialog.edge_tag_buttons['MOD'].isChecked() is False
            dialog.set_graph(graph, 'agg', str(root / 'agg-project'))
            dialog.show()
            app.processEvents()
            assert len(dialog._display) == 6  # every open starts fully expanded

            # collapse both modules (double-click each root), aggregating their children's edges
            for key in ('mod_a', 'mod_b'):
                node = dialog.view._node_items[key]
                click = dialog.view.mapFromScene(node.sceneBoundingRect().center())
                QTest.mouseDClick(dialog.view.viewport(), Qt.LeftButton, Qt.NoModifier, click)
                app.processEvents()
            assert set(dialog._display) == {'mod_a', 'mod_b'}
            # the DATA must show the full sum of both children's connections (a1->b1, a2->b2), not
            # just one of them and not a dropped/partial aggregate
            assert dialog._display['mod_a'].outgoing == ['mod_b']
            assert sorted(dialog._display['mod_a'].outgoing_detail) == ['b1', 'b2']
            assert sorted(dialog._display['mod_b'].incoming_detail) == ['a1', 'a2']
            # but the RENDERED edge between the two collapsed (MOD-tagged) roots must stay hidden
            # since the MOD edge-tag toggle defaults off
            assert dialog.view._edges == []

            # re-enabling the toggle reveals the very same aggregated connection
            dialog.edge_tag_buttons['MOD'].setChecked(True)
            app.processEvents()
            assert any(source == 'mod_a' and target == 'mod_b' for source, target, *_ in dialog.view._edges)
            dialog.close()
            QSettings('KANT', 'KANT Editor').remove(dialog._position_key)

    def test_xref_map_drill_down(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            app = self.app
            # drill-down: a class with 2 methods that call each other is drillable; a lone leaf isn't
            drill_graph = {
                'cls': XrefElement('cls', 'cls', 'CLS', 'Foo', 'classe', 'd.py', 0),
                'm1': XrefElement('m1', 'm1', 'FN', 'bar', 'metodo bar', 'd.py', 1, outgoing=['m2'], parent='cls'),
                'm2': XrefElement('m2', 'm2', 'FN', 'baz', 'metodo baz', 'd.py', 2, incoming=['m1'], parent='cls'),
                'lone': XrefElement('lone', 'lone', 'FN', 'solo', 'funzione sola', 'd.py', 3),
            }
            drill_dialog = XrefMapDialog()
            drill_dialog.resize(900, 650)
            drill_dialog.set_graph(drill_graph, 'drill', str(root / 'drill-project'))
            drill_dialog.show()
            app.processEvents()
            assert drill_dialog._is_drillable('cls') is True
            assert drill_dialog._is_drillable('lone') is False
            # real clicks end-to-end, not a direct _enter_drill_mode() call: the eye sits on top of its
            # node with no item-data of its own, so a naive itemAt()-based click handler reads it back
            # as "clicked empty canvas" and wipes the pin before the eye ever sees the press — exercise
            # the actual pin-then-click-eye path to catch that regression.
            cls_node = drill_dialog.view._node_items['cls']
            cls_viewport_pos = drill_dialog.view.mapFromScene(cls_node.sceneBoundingRect().center())
            QTest.mouseClick(drill_dialog.view.viewport(), Qt.LeftButton, Qt.NoModifier, cls_viewport_pos)
            app.processEvents()
            eye = drill_dialog.view._eye_badges['cls']
            assert eye.isVisible()
            eye_viewport_pos = drill_dialog.view.mapFromScene(eye.mapToScene(eye.boundingRect().center()))
            QTest.mouseClick(drill_dialog.view.viewport(), Qt.LeftButton, Qt.NoModifier, eye_viewport_pos)
            app.processEvents()
            assert drill_dialog._drill_key == 'cls'
            # the parent ('cls') is detached from the graph entirely — only its children remain,
            # 'lone' stays excluded (not a child of 'cls')
            assert set(drill_dialog._display) == {'m1', 'm2'}
            assert drill_dialog._display['m1'].outgoing == ['m2']
            assert 'cls' not in drill_dialog.view._node_items
            # the parent becomes the fixed title card instead of a graph node
            assert drill_dialog.drill_title_card.isVisible()
            assert drill_dialog.drill_title_tag.text() == '[CLS]'
            assert drill_dialog.drill_title_name.text() == 'classe'
            assert drill_dialog.drill_back_btn.isVisible()
            drill_dialog._exit_drill_mode()
            app.processEvents()
            assert set(drill_dialog._display) == {'cls', 'm1', 'm2', 'lone'}
            assert not drill_dialog.drill_back_btn.isVisible()
            assert not drill_dialog.drill_title_card.isVisible()
            drill_dialog.close()
            QSettings('KANT', 'KANT Editor').remove(drill_dialog._position_key)

    def test_replace_project_and_delete_tree_item(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            replace_target = root / 'replace.txt'
            replace_target.write_text('old needle', encoding='utf-8')
            replace_window = MainWindow.__new__(MainWindow)
            replace_window.project_root_path = str(root)
            answers = iter([('needle', True), ('replacement', True)])
            replace_window._ide_text = lambda *_args, **_kwargs: next(answers)
            replace_window._ide_yes_no = lambda *_args, **_kwargs: True
            replace_window._flush_all_tabs = lambda: replace_target.write_text('new needle', encoding='utf-8') is not None
            replace_window._iter_project_text_files = lambda project_root=None: MainWindow._iter_project_text_files(replace_window, project_root)
            replace_window._run_background = lambda work, done: done(work(), None)
            replace_window.open_tabs = {}
            replace_window._refresh_after_fs_change = lambda: None
            replace_window.terminal = LabelStub()
            MainWindow._replace_project(replace_window)
            assert replace_target.read_text(encoding='utf-8') == 'new replacement'

            delete_target = root / 'delete.txt'
            delete_target.write_text('old', encoding='utf-8')
            events = []

            class DirtyTab:
                def flush_pending_save(self):
                    events.append('flush')
                    delete_target.write_text('new', encoding='utf-8')
                    return True

            delete_tab = DirtyTab()
            delete_window = MainWindow.__new__(MainWindow)
            delete_window.open_tabs = {str(delete_target): delete_tab}
            delete_window.tabs = type('Tabs', (), {'indexOf': lambda _self, _tab: 0})()
            delete_window._ide_yes_no = lambda *_args, **_kwargs: True
            delete_window._close_tab = lambda _idx, flush=True: events.append(('close', flush)) or True

            def move_after_save(path):
                events.append('move')
                assert Path(path).read_text(encoding='utf-8') == 'new'
                return 'trash'

            delete_window._move_to_trash = move_after_save
            delete_window._refresh_after_fs_change = lambda: None
            delete_window.terminal = LabelStub()
            delete_item = QTreeWidgetItem()
            delete_item.setData(0, ROLE_PATH, str(delete_target))
            MainWindow._delete_tree_item(delete_window, delete_item, 'file')
            assert events == ['flush', ('close', False), 'move']

    def test_git_menu_more_action_opens_full_panel(self):
        # Git is a normal QMenuBar entry like File/Cerca/Aspetto/LSP (no more special "click
        # bypasses the dropdown" QToolButton behavior) — "Altro..." (the last item, after a
        # separator) is the one action that reaches the full Git panel.
        window = MainWindow()
        opened = []
        window._open_git_panel = lambda: opened.append(True)
        window.title_bar.git_more_menu_action.trigger()
        assert opened == [True]
        window.close()

    def test_git_commit_and_branch_switch(self):
        # real repo, real `git` subprocess calls (_run_git shells out), only the modal dialogs are
        # stubbed to bypass .exec()
        git_root = Path(_mkdtemp_safe())
        subprocess.run(['git', 'init', '-q'], cwd=git_root, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@test.local'], cwd=git_root, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=git_root, check=True)
        (git_root / 'a.txt').write_text('one', encoding='utf-8')
        subprocess.run(['git', 'add', 'a.txt'], cwd=git_root, check=True)

        git_window = MainWindow.__new__(MainWindow)
        git_window.git_root = str(git_root)
        git_window._run_background = lambda work, done: done(work(), None)
        git_window.terminal = LabelStub()
        git_window._refresh_after_fs_change = lambda: None
        git_window._run_git = lambda args, root=None: MainWindow._run_git(git_window, args, root)
        git_window._ide_git_commit_form = lambda staged: 'test commit' if staged else None
        MainWindow._git_commit(git_window)
        log = subprocess.run(
            ['git', 'log', '--oneline', '--format=%s'], cwd=git_root, capture_output=True, text=True, check=True,
        )
        assert log.stdout.strip() == 'test commit'

        subprocess.run(['git', 'branch', 'feature-x'], cwd=git_root, check=True)
        git_window._ide_item = lambda *_args, **_kwargs: ('feature-x', True)
        MainWindow._git_switch_branch(git_window)
        branch = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=git_root, capture_output=True, text=True, check=True,
        )
        assert branch.stdout.strip() == 'feature-x'

    def test_git_panel_dialog(self):
        # the one-window Git panel: real repo, real git subprocess calls throughout (refresh,
        # stage-via-checkbox, diff-on-click, commit) — nothing here is mocked except the window
        # it's attached to, matching the other git tests' style
        git_root = Path(_mkdtemp_safe())
        subprocess.run(['git', 'init', '-q'], cwd=git_root, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@test.local'], cwd=git_root, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=git_root, check=True)
        (git_root / 'tracked.txt').write_text('one\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'tracked.txt'], cwd=git_root, check=True)
        subprocess.run(['git', 'commit', '-m', 'initial'], cwd=git_root, check=True)
        (git_root / 'tracked.txt').write_text('two\n', encoding='utf-8')  # unstaged modification
        (git_root / 'new.txt').write_text('brand new\n', encoding='utf-8')  # untracked

        # GitPanelDialog(window) parents itself to window (a real QDialog(parent) call) — needs an
        # actually-constructed MainWindow, not a bare MainWindow.__new__ stub, for shiboken to accept it
        window = MainWindow()
        window.git_root = str(git_root)
        window._refresh_after_fs_change = lambda: None

        panel = GitPanelDialog(window)
        panel.refresh()
        branches = [panel.branch_combo.itemText(i) for i in range(panel.branch_combo.count())]
        assert branches and panel.branch_combo.currentText() in branches

        assert panel.files_list.topLevelItemCount() == 2
        items_by_rel = {}
        for i in range(panel.files_list.topLevelItemCount()):
            item = panel.files_list.topLevelItem(i)
            items_by_rel[item.data(0, kant_gitops_module._ROLE_PATH)] = item
        assert set(items_by_rel) == {'tracked.txt', 'new.txt'}
        assert all(item.checkState(0) == Qt.Unchecked for item in items_by_rel.values())  # nothing staged yet

        panel._on_item_clicked(items_by_rel['tracked.txt'], 0)
        assert 'two' in panel.diff_view.toPlainText()

        # checking the box stages the file for real (real `git add`)
        items_by_rel['new.txt'].setCheckState(0, Qt.Checked)
        status = subprocess.run(
            ['git', 'status', '--porcelain=v1'], cwd=git_root, capture_output=True, text=True, check=True,
        )
        assert 'A  new.txt' in status.stdout

        panel.message_field.setPlainText('add new file')
        panel._commit()
        log = subprocess.run(
            ['git', 'log', '--oneline', '--format=%s'], cwd=git_root, capture_output=True, text=True, check=True,
        )
        assert log.stdout.strip().splitlines()[0] == 'add new file'
        panel.close()
        window.close()

    def test_git_init_flow_from_button_click(self):
        # clicking Git with no repository routes into a real `git init` instead of just refusing —
        # GitPanelDialog is swapped for a lightweight fake so this doesn't need a real Qt-parented
        # MainWindow just to reach the init logic (same shiboken constraint test_git_panel_dialog
        # hit — GitPanelDialog(window) needs an actually-constructed parent)
        project_root = Path(_mkdtemp_safe())
        window = MainWindow.__new__(MainWindow)
        window.git_root = None
        window.git_status = {}
        window.project_root_path = str(project_root)
        window.git_panel = None
        window._ide_yes_no = lambda *_args, **_kwargs: True
        messages = []
        window._ide_message = lambda title, msg: messages.append(msg)
        window._refresh_after_fs_change = lambda: None
        window._refresh_git_status = lambda: None

        opened = []

        class FakePanel:
            def __init__(self, _window):
                opened.append(True)

            def refresh(self):
                pass

            def show(self):
                pass

            def raise_(self):
                pass

            def activateWindow(self):
                pass

        original_dialog_cls = kant_gitops_module.GitPanelDialog
        kant_gitops_module.GitPanelDialog = FakePanel
        try:
            MainWindow._open_git_panel(window)
        finally:
            kant_gitops_module.GitPanelDialog = original_dialog_cls

        assert (project_root / '.git').is_dir()  # a real `git init` actually ran
        assert window.git_root == str(project_root)
        assert opened == [True]
        assert not messages  # no error message on the success path

        # no project open at all -> a message, not a crash trying to init nothing
        none_window = MainWindow.__new__(MainWindow)
        none_window.git_root = None
        none_window.project_root_path = None
        none_messages = []
        none_window._ide_message = lambda title, msg: none_messages.append(msg)
        MainWindow._open_git_panel(none_window)
        assert none_messages

    def test_run_tests_pytest_integration(self):
        # a real pytest subprocess against an isolated temp project with one passing and one failing
        # test, checking the FAILED-line parser feeds results_view with a clickable entry
        test_project = Path(_mkdtemp_safe())
        (test_project / 'test_sample.py').write_text(
            'def test_ok():\n    assert True\n\n\ndef test_should_fail():\n    assert False\n',
            encoding='utf-8',
        )
        test_window = MainWindow.__new__(MainWindow)
        test_window.project_root_path = str(test_project)
        test_window.git_root = None
        test_window.open_tabs = {}
        test_window._test_run_pending = False
        test_window._run_background = lambda work, done: done(work(), None)
        test_window.terminal = LabelStub()
        test_window.results_view = QTreeWidget()
        test_window._toggle_info_popup = lambda *_args, **_kwargs: None
        MainWindow._run_tests(test_window)
        assert test_window._test_run_pending is False
        assert test_window.results_view.topLevelItemCount() == 1
        root_item = test_window.results_view.topLevelItem(0)
        assert 'failed' in root_item.text(0), root_item.text(0)
        fail_labels = [root_item.child(i).text(0) for i in range(root_item.childCount())]
        assert any('test_sample.py::test_should_fail' in label for label in fail_labels)
        fail_item = next(root_item.child(i) for i in range(root_item.childCount()) if 'test_should_fail' in root_item.child(i).text(0))
        assert fail_item.data(0, ROLE_PATH) == str(test_project / 'test_sample.py')
        assert fail_item.data(0, ROLE_TEXT) == 'def test_should_fail'

    def test_format_with_external_tool(self):
        # black/ruff formatting: neither tool is installed in this environment, so shutil.which and
        # subprocess.run are monkeypatched (module-level, restored in finally) to simulate black being
        # on PATH and confirm the formatted stdout actually reaches _apply_local_text
        with _temp_dir() as tmp:
            fmt_source = Path(tmp) / 'fmt.py'
            fmt_source.write_text('# [MOD OPEN] fmt.py\nx=1\n# [MOD CLOSED] fmt.py\n', encoding='utf-8')
            fmt_tab = FileTab(str(fmt_source), parse_kant(fmt_source.read_text(encoding='utf-8')))
            fmt_window = MainWindow.__new__(MainWindow)
            fmt_window.tabs = type('Tabs', (), {'currentWidget': lambda _self: fmt_tab})()
            fmt_window.project_root_path = None
            applied = []
            fmt_window._apply_local_text = lambda tab, text, message: applied.append((text, message))
            fmt_window._ide_message = lambda title, msg: applied.append(('MESSAGE', msg))

            original_which = kant_mainwindow_module.shutil.which
            original_has_module = kant_mainwindow_module.has_module
            original_run = kant_mainwindow_module.subprocess.run

            def fake_which(name):
                return '/usr/bin/black' if name == 'black' else None

            def fake_run(args, input=None, capture_output=None, text=None, timeout=None):
                assert args[:3] == [sys.executable, '-m', 'black']
                return subprocess.CompletedProcess(args, 0, stdout=input.replace('x=1', 'x = 1'), stderr='')

            kant_mainwindow_module.shutil.which = fake_which
            kant_mainwindow_module.has_module = lambda _python, module: module == 'black'
            kant_mainwindow_module.subprocess.run = fake_run
            try:
                MainWindow._format_with_external_tool(fmt_window)
            finally:
                kant_mainwindow_module.shutil.which = original_which
                kant_mainwindow_module.has_module = original_has_module
                kant_mainwindow_module.subprocess.run = original_run
            assert len(applied) == 1
            assert 'x = 1' in applied[0][0]

            # neither tool actually installed in this real environment -> the fallback message path
            none_window = MainWindow.__new__(MainWindow)
            none_window.tabs = type('Tabs', (), {'currentWidget': lambda _self: fmt_tab})()
            none_window.project_root_path = None
            none_messages = []
            none_window._ide_message = lambda title, msg: none_messages.append(msg)
            MainWindow._format_with_external_tool(none_window)
            assert any('black' in m and 'ruff' in m for m in none_messages)

    def test_command_palette(self):
        # entries come from introspecting the title bar's own menus, disabled actions are excluded,
        # and picking an entry just triggers the real QAction (real Qt signal dispatch, not a stub)
        # so it reuses every menu action's existing wiring untouched
        fired = []

        class FakeMenuBtn:
            def __init__(self, specs):
                menu = QMenu()
                for label, enabled in specs:
                    action = menu.addAction(label)
                    action.setEnabled(enabled)
                    action.triggered.connect(lambda _checked=False, l=label: fired.append(l))
                self._menu = menu

            def menu(self):
                return self._menu

        palette_window = MainWindow.__new__(MainWindow)
        palette_window.title_bar = type('TB', (), {
            'file_menu_btn': FakeMenuBtn([('Salva', True), ('Disabilitato', False)]),
            'search_menu_btn': FakeMenuBtn([('Cerca nel progetto', True)]),
            'appearance_menu_btn': FakeMenuBtn([('Notte', True)]),
            'lsp_menu_btn': FakeMenuBtn([('Formatta documento', True)]),
            'git_menu_btn': FakeMenuBtn([('Commit...', True)]),
        })()
        captured = {}

        def fake_palette(entries):
            captured['entries'] = entries
            return next(action for label, action in entries if label == 'Git: Commit...')

        palette_window._ide_command_palette = fake_palette
        MainWindow._show_command_palette(palette_window)
        palette_labels = [label for label, _action in captured['entries']]
        assert 'File: Disabilitato' not in palette_labels
        assert 'File: Salva' in palette_labels
        assert fired == ['Commit...']

    def test_welcome_page_recent_folder_cards(self):
        # RecentFolderCard has its own mousePressEvent (QPushButton can't bold just one line of its
        # own text), so a real click needs exercising end-to-end rather than trusting a signal exists
        recent_a = Path(_mkdtemp_safe())
        recent_b = Path(_mkdtemp_safe())
        QSettings('KANT', 'KANT Editor').setValue('recentFolders', [str(recent_a), str(recent_b)])
        window = MainWindow()
        assert window.recent_layout.count() == 2
        opened = []
        window._open_project_folder = lambda path: opened.append(path)
        card = window.recent_layout.itemAt(0).widget()
        assert isinstance(card, RecentFolderCard)
        QTest.mouseClick(card, Qt.LeftButton)
        assert opened == [str(recent_a)]
        window.close()
        QSettings('KANT', 'KANT Editor').remove('recentFolders')

    def test_confirm_before_close(self):
        # declining the confirmation must actually keep the window open (event.ignore()), not just
        # skip past it — real closeEvent() call, not a direct _confirm_close() check in isolation.
        # _closing only gets set True inside closeEvent, after the confirm+flush checks pass — the
        # one reliable signal here, since isVisible()/isHidden() are meaningless on a never-shown window
        window = MainWindow()
        window._confirm_close = lambda: False
        window.close()
        assert not window._closing  # declined -> real cleanup never ran

        window._confirm_close = lambda: True
        window.close()
        assert window._closing  # accepted -> closeEvent actually proceeded
        window.deleteLater()

    def test_project_chrome_hidden_on_welcome_shown_after_open(self):
        # The title-bar menus belong to the project workspace, so none appear on the welcome screen.
        # These 5 are QMenuBar entries (QAction, via menuAction()) now, not QWidgets — QAction has
        # isVisible() but no isHidden(), unlike action_toolbar (a real QWidget, isHidden() is fine
        # there since the window is never shown in this test, so isVisible() would read False
        # regardless, via the whole unshown ancestor chain).
        window = MainWindow()
        project_menus = (
            window.title_bar.file_menu_btn, window.title_bar.search_menu_btn,
            window.title_bar.appearance_menu_btn, window.title_bar.lsp_menu_btn,
            window.title_bar.git_menu_btn,
        )
        assert all(not action.isVisible() for action in project_menus)
        assert window.action_toolbar.isHidden()

        project_root = Path(_mkdtemp_safe())
        window._ide_yes_no = lambda *_args, **_kwargs: False  # decline the KANT-tagging prompts
        window._open_project_folder(str(project_root))
        assert all(action.isVisible() for action in project_menus)
        assert not window.action_toolbar.isHidden()

        window._go_back_to_welcome()
        assert all(not action.isVisible() for action in project_menus)
        assert window.action_toolbar.isHidden()
        window.close()

    def test_crash_handler_writes_log_and_shows_dialog(self):
        # PySide6 routes an uncaught exception from a Qt slot through sys.excepthook same as any
        # other uncaught exception, so installing the hook and invoking it directly with a synthetic
        # exception exercises the real code path without needing to actually crash the app.
        # QMessageBox.critical is monkeypatched (module-level, restored in finally) since it's a
        # blocking modal that would otherwise hang an offscreen test run.
        original_excepthook = sys.excepthook
        original_critical = kant_editor.QMessageBox.critical
        critical_calls = []
        kant_editor.QMessageBox.critical = lambda *args, **kwargs: critical_calls.append(args)
        try:
            kant_editor._install_crash_handler()
            try:
                raise ValueError('synthetic crash for the regression check')
            except ValueError:
                sys.excepthook(*sys.exc_info())
        finally:
            sys.excepthook = original_excepthook
            kant_editor.QMessageBox.critical = original_critical
        assert len(critical_calls) == 1
        assert 'synthetic crash' in critical_calls[0][2]
        log_files = sorted(kant_editor.CRASH_LOG_DIR.glob('crash_*.log'), key=lambda p: p.stat().st_mtime)
        assert log_files, 'no crash log file was written'
        assert 'ValueError' in log_files[-1].read_text(encoding='utf-8')
        assert 'synthetic crash' in log_files[-1].read_text(encoding='utf-8')

    def test_validate_kant_project_auto_resyncs_stale_map(self):
        # regression for the "the KANT board should fix itself when it finds a discrepancy" request:
        # a KANT_*.md map that's fallen behind the source (missing an entry validate_kant_project's
        # own coherence check would flag) is auto-regenerated by _validate_kant_project itself,
        # instead of just reporting an error the user has to notice and manually re-sync.
        with _temp_dir() as tmp:
            root = Path(tmp)
            source_dir = root / 'src'
            source_dir.mkdir()
            _write_app_py(source_dir)  # tags one MOD: src/app.py

            map_path = root / f'KANT_{root.name}.md'
            stale_map = '# KANT Code Map - stale\n\n## Struttura\n\n```\n```\n'
            map_path.write_text(stale_map, encoding='utf-8')

            w = MainWindow.__new__(MainWindow)
            w.project_root_path = str(root)
            w.kant_map_path = None
            w.kant_map_label = LabelStub()
            w._xref_cache = None
            w._xref_generation = 0
            w._xref_pending_generation = None
            w.map_dialog = None
            w._map_sync_generation = 0
            w._map_sync_running = False
            w._map_sync_rerun_needed = False
            w._run_background = lambda work, done: done(work(), None)

            result = MainWindow._validate_kant_project(w)
            assert 'mancano' not in result  # not surfaced as an unresolved "missing X" error
            assert 'rigenerata automaticamente' in result  # noted as self-healed instead
            assert 'src/app.py' in map_path.read_text(encoding='utf-8')  # actually rewritten on disk

    def test_kant_map_sync_trash_restore_and_ai_review(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            source_dir = root / 'src'
            source_dir.mkdir()
            source = _write_app_py(source_dir)
            (source_dir / 'bad.py').write_text('# [FN OPEN #deadbeef] broken\n', encoding='utf-8')

            w = MainWindow.__new__(MainWindow)
            w.project_root_path = str(root)
            w.kant_map_path = None
            w.kant_map_label = LabelStub()
            w._xref_cache = None
            w._xref_generation = 0
            w._xref_pending_generation = None
            w.map_dialog = None
            w._map_sync_generation = 0
            w._map_sync_running = False
            w._map_sync_rerun_needed = False
            w._run_background = lambda work, done: done(work(), None)
            MainWindow._sync_kant_map(w)
            assert (root / f'KANT_{root.name}.md').exists()
            assert 'ERRORI' in MainWindow._validate_kant_project(w)

            trash_me = source_dir / 'trash.txt'
            trash_me.write_text('x', encoding='utf-8')
            trashed = Path(MainWindow._move_to_trash(w, str(trash_me)))
            assert trashed.exists()
            assert Path(str(trashed) + '.restore').read_text(encoding='utf-8') == 'src/trash.txt'
            candidates = MainWindow._restore_candidates(w)
            assert any(candidate[2] == 'src/trash.txt' for candidate in candidates)
            try:
                safe_project_path(str(root), '../escape.txt')
                assert False, 'restore path traversal accepted'
            except ValueError:
                pass

            snapshot = create_snapshot(str(root), {'.kant-trash'})
            rollback_target = source_dir / 'rollback.txt'
            rollback_target.write_text('created later', encoding='utf-8')
            source.write_text(source.read_text(encoding='utf-8').replace('print(1)', 'changed(1)'), encoding='utf-8')
            review = build_ai_review(str(root), snapshot, {'.kant-trash'})
            assert review and any('app.py' in item['path'] for item in review)
            rollback_snapshot(str(root), snapshot, {'.kant-trash'})
            assert not rollback_target.exists() and 'print(1)' in source.read_text(encoding='utf-8')
            discard_snapshot(snapshot)

            review_root = root / 'review'
            review_root.mkdir()
            review_file = review_root / 'sample.txt'
            original_lines = [f'line {index}\n' for index in range(20)]
            review_file.write_text(''.join(original_lines), encoding='utf-8')
            review_snapshot = create_snapshot(str(review_root))
            changed_lines = original_lines[:]
            changed_lines[1] = 'first change\n'
            changed_lines[18] = 'second change\n'
            review_file.write_text(''.join(changed_lines), encoding='utf-8')
            review = build_ai_review(str(review_root), review_snapshot)
            assert len(review) == 1 and len(review[0]['hunks']) == 2
            review_card = _AiReviewCard(review, render_review_text)
            assert review_card.details.objectName() == 'aiReviewDetails'
            assert review_card.accepted_hunks('sample.txt') == {0, 1}
            review_card.file_items['sample.txt'].child(1).setCheckState(0, Qt.Unchecked)
            assert review_card.accepted_hunks('sample.txt') == {0}
            resolved = []
            review_card.resolved.connect(resolved.append)
            review_card._show_details('sample.txt')
            review_card.resolved.emit('cancel')
            assert resolved == ['cancel']
            review_card.set_resolved()
            assert all(not button.isEnabled() for button in review_card._action_buttons)
            review_card.close()

            window = MainWindow()
            chat_rows_before = window.claude_pane.chat_layout.count()
            review_outcomes = []
            window.claude_pane.show_ai_review(review, render_review_text, lambda *args: review_outcomes.append(args))
            # a separate window now (QDialog(self) still registers as a Qt child of claude_pane for
            # ownership, findChildren finds it across that window boundary), not inserted into chat
            assert window.claude_pane.chat_layout.count() == chat_rows_before
            dialog_cards = window.claude_pane.findChildren(_AiReviewCard)
            assert len(dialog_cards) == 1
            dialog_card = dialog_cards[0]
            assert dialog_card.in_dialog is True
            assert not dialog_card.details.isHidden()  # shown immediately, no "Controllo" click needed
            # DiffHighlighter colors +/- lines instead of the diff being flat plain text
            assert isinstance(dialog_card.diff_highlighter, DiffHighlighter)
            assert dialog_card.diff_view.toPlainText().strip()
            dialog_card.resolved.emit('apply')
            assert review_outcomes and review_outcomes[0][0] == 'apply'
            assert all(not button.isEnabled() for button in dialog_card._action_buttons)  # locks after resolving
            window.close()
            apply_ai_review(str(review_root), review, {'sample.txt': {0}})
            partial = review_file.read_text(encoding='utf-8')
            assert 'first change' in partial and 'line 18' in partial and 'second change' not in partial
            review_file.write_text(''.join(changed_lines), encoding='utf-8')
            apply_ai_review(str(review_root), review, {'sample.txt': set()}, {'sample.txt': 'manually edited\n'})
            assert review_file.read_text(encoding='utf-8') == 'manually edited\n'
            review_file.write_text('external change\n', encoding='utf-8')
            try:
                apply_ai_review(str(review_root), review, {'sample.txt': {0, 1}})
                assert False, 'external edit during AI review was overwritten'
            except OSError:
                assert review_file.read_text(encoding='utf-8') == 'external change\n'
            discard_snapshot(review_snapshot)

    def test_conflict_save_undo_coalesce_lsp_error_and_tree_click_routing(self):
        with _temp_dir() as tmp:
            source_dir = Path(tmp) / 'src'
            source_dir.mkdir()
            source = _write_app_py(source_dir)

            conflict_tab = FileTab(str(source), parse_kant(source.read_text(encoding='utf-8')))
            conflict_run = next(item for node in conflict_tab.tree.body if hasattr(node, 'body') for item in node.body if isinstance(item, Run))
            conflict_run.lines = ['local()']
            conflict_tab.dirty = True
            source.write_text(source.read_text(encoding='utf-8').replace('print(1)', 'external(1)'), encoding='utf-8')
            conflicts = []
            save_failures = []
            conflict_tab.saveConflict.connect(lambda: conflicts.append(True))
            conflict_tab.saveFailed.connect(save_failures.append)
            assert not conflict_tab.save() and conflicts and 'external(1)' in source.read_text(encoding='utf-8')
            assert conflict_tab.save(force=True), save_failures
            assert 'local()' in source.read_text(encoding='utf-8')

            undo_tab = FileTab(str(source), parse_kant(source.read_text(encoding='utf-8')))
            undo_tab.remember_undo_state(coalesce=True)
            undo_tab.remember_undo_state(coalesce=True)
            assert len(undo_tab.undo_stack) == 1

            lsp = LspClient()
            errors = []
            lsp.serverError.connect(errors.append)
            lsp.init_id = 7
            lsp._handle_message({'id': 7, 'error': {'message': 'bad initialize'}})
            assert errors == ['bad initialize'] and not lsp.ready

            w = MainWindow.__new__(MainWindow)
            calls = []
            item = QTreeWidgetItem()
            item.setData(0, ROLE_KIND, 'file')
            item.setData(0, ROLE_PATH, str(source))
            w.open_tabs = {str(source): object()}
            w._open_file = lambda path: calls.append(('open', path)) or True
            w._render_view = lambda tab, uid=None: calls.append(('render', uid))
            w._update_io_tabs = lambda uid: calls.append(('io', uid))
            MainWindow._on_tree_item_clicked(w, item, 0)
            assert ('render', None) in calls and ('io', None) in calls

    def test_pyenv_venv_detection_config_roundtrip_and_majority_heuristic(self):
        with _temp_dir() as tmp:
            project = Path(tmp)
            scripts_dir = project / '.venv' / ('Scripts' if os.name == 'nt' else 'bin')
            scripts_dir.mkdir(parents=True)
            venv_python = scripts_dir / ('python.exe' if os.name == 'nt' else 'python')
            venv_python.write_text('', encoding='utf-8')

            assert detect_venvs(str(project)) == [str(venv_python)]
            assert load_interpreter(str(project)) is None  # nothing configured yet

            save_interpreter(str(project), str(venv_python))
            config = json.loads((project / '.kant' / 'python.json').read_text(encoding='utf-8'))
            assert config['python'] == venv_python.relative_to(project).as_posix()  # portable/relative
            assert load_interpreter(str(project)) == str(venv_python)

            assert interpreter_label(str(venv_python)) == '.venv'
            assert interpreter_version(sys.executable) is not None
            assert has_module(sys.executable, 'this_module_does_not_exist_xyz') is False

            assert dependency_file(str(project)) is None
            (project / 'requirements.txt').write_text('pytest\n', encoding='utf-8')
            assert dependency_file(str(project)) == 'requirements.txt'

            # 3 .py vs 2 .txt (requirements.txt from above + c.txt here) — a clear majority, not a
            # 2-vs-2 tie: is_python_majority_project's max(counts, key=counts.get) breaks a tie by
            # dict insertion order, which follows os.walk's directory-listing order — unspecified,
            # and observed to differ between Windows and Linux, making a tied count flaky per-OS
            (project / 'a.py').write_text('x = 1\n', encoding='utf-8')
            (project / 'b.py').write_text('y = 2\n', encoding='utf-8')
            (project / 'd.py').write_text('z = 3\n', encoding='utf-8')
            (project / 'c.txt').write_text('text\n', encoding='utf-8')
            assert is_python_majority_project(str(project)) is True

    def test_active_python_auto_select_interpreter_and_status_label(self):
        with _temp_dir() as tmp:
            project = Path(tmp)
            scripts_dir = project / '.venv' / ('Scripts' if os.name == 'nt' else 'bin')
            scripts_dir.mkdir(parents=True)
            venv_python = scripts_dir / ('python.exe' if os.name == 'nt' else 'python')
            venv_python.write_text('', encoding='utf-8')

            window = MainWindow.__new__(MainWindow)
            window.project_root_path = str(project)
            window.python_env_label = _StatusButtonStub()
            assert MainWindow._active_python(window) == sys.executable  # unconfigured -> fallback

            MainWindow._auto_select_interpreter(window)
            assert MainWindow._active_python(window) == str(venv_python)
            assert window.python_env_label.isVisible()
            assert '.venv' in window.python_env_label.text()

            window.project_root_path = None
            MainWindow._refresh_python_env_label(window)
            assert not window.python_env_label.isVisible()

    def test_select_python_interpreter_and_install_dependencies(self):
        with _temp_dir() as tmp:
            project = Path(tmp)
            (project / 'requirements.txt').write_text('pytest\n', encoding='utf-8')
            chosen_python = sys.executable

            class _TerminalStub:
                def __init__(self):
                    self.commands = []
                    self.messages = []

                def run_command(self, command, cwd=None):
                    self.commands.append((command, cwd))
                    return True

                def write_info(self, text):
                    self.messages.append(text)

            window = MainWindow.__new__(MainWindow)
            window.project_root_path = str(project)
            window.python_env_label = _StatusButtonStub()
            window.terminal = _TerminalStub()
            window._ide_python_interpreter_form = lambda candidates, current: chosen_python

            MainWindow._select_python_interpreter(window)
            assert MainWindow._active_python(window) == chosen_python
            assert any('Interprete Python' in m for m in window.terminal.messages)

            MainWindow._install_dependencies(window)
            assert window.terminal.commands
            command, cwd = window.terminal.commands[-1]
            assert 'pip' in command and 'install' in command and 'requirements.txt' in command
            assert cwd == str(project)

    def test_run_lint_check_ruff_integration(self):
        with _temp_dir() as tmp:
            project = Path(tmp)
            window = MainWindow.__new__(MainWindow)
            window.project_root_path = str(project)
            window.terminal = LabelStub()
            window.results_view = QTreeWidget()
            window._toggle_info_popup = lambda *_args, **_kwargs: None
            window._run_background = lambda work, done: done(work(), None)

            original_has_module = kant_mainwindow_module.has_module
            original_run = kant_mainwindow_module.subprocess.run

            def fake_has_module(_python_path, module):
                return module == 'ruff'

            def fake_run(args, cwd=None, capture_output=None, text=None, timeout=None):
                assert args[:3] == [sys.executable, '-m', 'ruff']
                return subprocess.CompletedProcess(args, 1, stdout='sample.py:3:1: F401 unused import\n', stderr='')

            kant_mainwindow_module.has_module = fake_has_module
            kant_mainwindow_module.subprocess.run = fake_run
            try:
                MainWindow._run_lint_check(window)
            finally:
                kant_mainwindow_module.has_module = original_has_module
                kant_mainwindow_module.subprocess.run = original_run

            assert window.results_view.topLevelItemCount() == 1
            root_item = window.results_view.topLevelItem(0)
            assert '1 problema' in root_item.text(0)
            finding = root_item.child(0)
            assert 'F401' in finding.text(0)
            assert finding.data(0, ROLE_KIND) == 'diagnostic-result'
            assert finding.data(0, ROLE_LINE) == 3

    def test_run_single_test_from_tree_context_menu(self):
        with _temp_dir() as tmp:
            project = Path(tmp)
            test_file = project / 'test_single.py'
            test_file.write_text('\n'.join([
                '# [MOD CATEGORY] sample module with one passing, one failing test',
                '# [MOD OPEN #mod1] test_single.py',
                '# [FN CATEGORY] trivially passes',
                '# [FN OPEN #fn1] test_ok',
                'def test_ok():',
                '    assert True',
                '# [FN CLOSED #fn1] test_ok',
                '# [FN CATEGORY] trivially fails',
                '# [FN OPEN #fn2] test_should_fail',
                'def test_should_fail():',
                '    assert False',
                '# [FN CLOSED #fn2] test_should_fail',
                '# [MOD CLOSED #mod1] test_single.py',
            ]), encoding='utf-8')

            tree = parse_kant(test_file.read_text(encoding='utf-8'))
            mod_node = next(n for n in tree.body if isinstance(n, Node))
            fail_node = next(c for c in mod_node.body if isinstance(c, Node) and c.name == 'test_should_fail')

            item = QTreeWidgetItem()
            item.setData(0, ROLE_KIND, 'section')
            item.setData(0, ROLE_PATH, str(test_file))
            item.setData(0, ROLE_UID, fail_node.uid)

            window = MainWindow.__new__(MainWindow)
            window.open_tabs = {}
            window.project_root_path = str(project)
            assert MainWindow._section_test_name(window, item) == 'test_should_fail'

            window.git_root = None
            window._test_run_pending = False
            window._run_background = lambda work, done: done(work(), None)
            window.terminal = LabelStub()
            window.results_view = QTreeWidget()
            window._toggle_info_popup = lambda *_args, **_kwargs: None
            MainWindow._run_single_test(window, str(test_file), 'test_should_fail')

            assert window._test_run_pending is False
            root_item = window.results_view.topLevelItem(0)
            assert 'failed' in root_item.text(0), root_item.text(0)
            assert root_item.childCount() == 1  # only the targeted test ran, not test_ok
            assert 'test_should_fail' in root_item.child(0).text(0)

    def test_vim_j_crosses_to_next_element(self):
        # regression for two bugs found together: (1) _open_file used to render the tab twice
        # (once explicitly, once via the setCurrentWidget-triggered currentChanged), leaving stale
        # deleteLater()-pending CodeEdits mixed into findChildren(CodeEdit) results; (2) a CRLF
        # source file leaves a literal trailing \r on each parsed line (by design — see
        # fileio.detect_line_ending), which QPlainTextEdit turns into an extra trailing empty
        # block, so the vim 'j' motion's last-block boundary check never fired.
        with _temp_dir() as tmp:
            project = Path(tmp)
            source = project / 'sample.py'
            with open(source, 'w', encoding='utf-8', newline='') as f:
                f.write('\r\n'.join([
                    '# [MOD OPEN] sample.py',
                    '# [FN OPEN] alpha', 'def alpha(): pass', '# [FN CLOSED] alpha',
                    '# [FN OPEN] beta', 'def beta(): pass', '# [FN CLOSED] beta',
                    '# [MOD CLOSED] sample.py',
                ]))

            app = self.app
            window = MainWindow()
            window.show()
            window.project_root_path = str(project)
            assert window._open_file(str(source))

            edits = window.active_page.findChildren(CodeEdit)
            assert len(edits) == 2, f'expected 2 CodeEdits, got {len(edits)} (stale duplicates from a double render?)'

            # vim mode defaults off (test_vim_mode_disabled_by_default) — the 'j'/'k' motions this
            # test exercises only fire in vim mode, so enable it here and restore the default
            # afterward, or the flag leaks into whichever test runs next
            set_vim_mode(True)
            try:
                edits[0].setFocus()
                app.processEvents()
                QTest.keyClicks(edits[0], 'j')
                app.processEvents()
                assert QApplication.focusWidget() is edits[1]

                QTest.keyClicks(edits[1], 'k')
                app.processEvents()
                assert QApplication.focusWidget() is edits[0]
            finally:
                set_vim_mode(False)
            window.close()

    def test_file_and_element_preview_tabs_survive_only_when_pinned(self):
        # One visible preview slot is shared by files and elements. Edits are flushed on replacement;
        # only pressing the pin button may leave a visible tab behind.
        with _temp_dir() as tmp:
            project = Path(tmp)
            paths = {}
            for name in ('f1', 'f2', 'f3', 'f4', 'f5', 'f6'):
                path = project / f'{name}.py'
                path.write_text('\n'.join([
                    f'# [MOD OPEN] {name}.py',
                    f'# [FN OPEN] {name}_alpha', f'def {name}_alpha(): pass', f'# [FN CLOSED] {name}_alpha',
                    f'# [FN OPEN] {name}_beta', f'def {name}_beta(): pass', f'# [FN CLOSED] {name}_beta',
                    f'# [MOD CLOSED] {name}.py',
                ]), encoding='utf-8')
                paths[name] = path

            window = MainWindow()
            window.project_root_path = str(project)
            window.git_root = None
            window.git_status = {}
            window._build_project_tree(window.tree.invisibleRootItem(), str(project))
            visible_tabs = lambda: sum(window.tabs.isTabVisible(i) for i in range(window.tabs.count()))

            def find_section(name):
                it = window.tree.invisibleRootItem()
                stack = [it.child(i) for i in range(it.childCount())]
                while stack:
                    c = stack.pop()
                    if c.data(0, ROLE_KIND) == 'section' and name in window.tree.itemWidget(c, 0).text():
                        return c
                    for i in range(c.childCount()):
                        stack.append(c.child(i))
                return None

            # a clean file preview is reused (closed, then reopened) for the next file clicked
            assert window._open_file(str(paths['f1']))
            count_before = window.tabs.count()
            assert window._open_file(str(paths['f2']))
            assert window.tabs.count() == count_before
            assert str(paths['f1']) not in window.open_tabs

            # ...and again for a third file, since f2 was itself never pinned
            count_before = window.tabs.count()
            assert window._open_file(str(paths['f3']))
            assert window.tabs.count() == count_before
            assert str(paths['f2']) not in window.open_tabs

            # pinning the current preview stops it from being reused
            f3_tab = window.open_tabs[str(paths['f3'])]
            window._pin_file_tab(f3_tab)
            count_before = window.tabs.count()
            assert window._open_file(str(paths['f1']))
            assert window.tabs.count() == count_before + 1
            assert str(paths['f3']) in window.open_tabs

            # A child replaces its unpinned parent visually; a sibling then replaces that child,
            # even after an edit. The shared FileTab retains and later flushes the source change.
            alpha_item = find_section('f1_alpha')
            beta_item = find_section('f1_beta')
            assert alpha_item is not None and beta_item is not None
            f1_tab = window.open_tabs[str(paths['f1'])]
            visible_before = visible_tabs()
            window._on_tree_item_clicked(alpha_item, 0)
            alpha_page = window.tabs.currentWidget()
            assert not window.tabs.isTabVisible(window.tabs.indexOf(f1_tab))
            assert visible_tabs() == visible_before
            alpha_page.findChildren(CodeEdit)[0].setPlainText('def f1_alpha(): return 1')
            count_before = window.tabs.count()
            window._on_tree_item_clicked(beta_item, 0)
            assert window.tabs.count() == count_before
            assert window.tabs.indexOf(alpha_page) == -1
            assert visible_tabs() == visible_before

            # A dirty whole-file preview is saved before replacement, not silently auto-pinned.
            assert window._open_file(str(paths['f4']))
            f4_tab = window.open_tabs[str(paths['f4'])]
            f4_tab.findChildren(CodeEdit)[0].setPlainText('# edited')
            count_before = window.tabs.count()
            assert window._open_file(str(paths['f5']))
            assert window.tabs.count() == count_before
            assert str(paths['f4']) not in window.open_tabs
            assert '# edited' in paths['f4'].read_text(encoding='utf-8')

            # Pinning only the child keeps that child visible. Its unpinned parent remains merely
            # a hidden backing model when another parent is opened.
            f5_alpha_item = find_section('f5_alpha')
            assert f5_alpha_item is not None
            window._on_tree_item_clicked(f5_alpha_item, 0)
            f5_alpha_page = window.tabs.currentWidget()
            window._pin_element_page(f5_alpha_page)
            f5_tab = window.open_tabs[str(paths['f5'])]
            assert str(paths['f5']) in window.open_tabs
            window._open_file(str(paths['f6']))
            assert str(paths['f5']) in window.open_tabs
            assert not window.tabs.isTabVisible(window.tabs.indexOf(f5_tab))
            assert window.tabs.indexOf(f5_alpha_page) != -1

            # unpinning a pinned tab closes it outright — the pin-button slot becomes a close
            # button instead of falling back to a plain "now reusable again" state
            f3_index = window.tabs.indexOf(f3_tab)
            close_btn = window.tabs.tabBar().tabButton(f3_index, QTabBar.RightSide)
            assert close_btn is not None
            close_btn.click()
            assert str(paths['f3']) not in window.open_tabs
            window.close()

    def test_preview_and_pin_tab_buttons_are_not_left_hidden_by_setTabButton(self):
        # regression: QTabBar.setTabButton() re-registering a corner widget can leave it internally
        # hidden with no matching re-show — a real, already-reproduced-and-fixed Qt quirk in this
        # exact codebase for the LeftSide _tab_label (_update_tab_title has the same .show() workaround
        # with the same explanation). The RightSide pin/close buttons this mirrors were missing it:
        # QTest.mouseClick(button, ...) calling the widget directly still "worked" in tests (it
        # bypasses hit-testing), which is exactly why this went unnoticed until a real mouse click
        # on the actual running app didn't register anything.
        with _temp_dir() as tmp:
            project = Path(tmp)
            source = project / 'f1.py'
            source.write_text('# [MOD OPEN] f1.py\nx=1\n# [MOD CLOSED] f1.py\n', encoding='utf-8')
            window = MainWindow()
            window.project_root_path = str(project)
            assert window._open_file(str(source))
            tab = window.open_tabs[str(source)]
            index = window.tabs.indexOf(tab)
            bar = window.tabs.tabBar()

            pin_btn = bar.tabButton(index, QTabBar.RightSide)
            assert pin_btn is not None and not pin_btn.isHidden()

            window._pin_file_tab(tab)
            close_btn = bar.tabButton(index, QTabBar.RightSide)
            assert close_btn is not None and not close_btn.isHidden()
            window.close()

    def test_add_element_block_appends_language_correct_node(self):
        # regression for the "+" card at the bottom of the KANT outline: confirms the button
        # exists, the dialog's answers actually land in tab.tree (not just some scratch buffer),
        # and the generated marker/code round-trips through serialize_kant/parse_kant.
        with _temp_dir() as tmp:
            project = Path(tmp)
            source = project / 'sample.py'
            source.write_text('\n'.join([
                '# [MOD OPEN] sample.py',
                '# [FN OPEN] alpha', 'def alpha(): pass', '# [FN CLOSED] alpha',
                '# [MOD CLOSED] sample.py',
            ]), encoding='utf-8')
            window = MainWindow()
            window.project_root_path = str(project)
            assert window._open_file(str(source))
            tab = window.open_tabs[str(source)]

            add_buttons = [
                b for b in tab.view_container.findChildren(type(window.add_file_btn))
                if 'Aggiungi' in b.text()
            ]
            assert len(add_buttons) == 1

            window._ide_new_element_form = lambda **kwargs: ('FN', 'nuova_funzione', 'fa qualcosa', 'Python')
            body_count_before = len(tab.tree.body)
            add_buttons[0].click()
            assert len(tab.tree.body) == body_count_before + 1
            new_node = tab.tree.body[-1]
            assert new_node.tag == 'FN' and new_node.name == 'nuova_funzione'
            # regression: the left "Codice" tree reads from disk (_rebuild_tree re-parses files),
            # never from in-memory tab state — _prompt_add_element used to only mark_dirty() and
            # wait up to 2s for autosave, so the tree wouldn't show a just-added element for a
            # while (or not at all, if the fs-watcher missed the eventual autosave's directory
            # event). It now saves immediately, so the tab is clean again and the file on disk
            # already has the new element.
            assert not tab.dirty
            assert 'nuova_funzione' in source.read_text(encoding='utf-8')
            # regression: build_new_element_node used to write the CATEGORY line as just the bare
            # description (no name — e.g. "[FN CATEGORY] fa qualcosa" instead of "... nuova_funzione
            # — fa qualcosa") and never emitted a tag line at all, so every element created through
            # this UI silently violated the KANT convention from the moment it was inserted
            assert new_node.category_raw is not None and 'nuova_funzione' in new_node.category_raw
            assert new_node.tag_raw is not None and 'nuova_funzione' in new_node.tag_raw

            text = serialize_kant(tab.tree)
            generated = text[text.index(new_node.category_raw):]
            assert generated.index('[FN CATEGORY]') < generated.index('[FN]') < generated.index('[FN OPEN')
            assert generated.index('[FN OPEN') < generated.index('def nuova_funzione') < generated.index('[FN CLOSED')
            assert 'def nuova_funzione():' in text
            reparsed = parse_kant(text)
            assert reparsed.body[-1].name == 'nuova_funzione'
            # the file's pre-existing 'alpha'/MOD markers are a deliberately bare legacy-style
            # fixture (no CATEGORY/tagline at all) and still warn on their own — only assert the
            # newly generated element itself introduces no errors or warnings
            audit = audit_kant_headers(text)
            assert audit['errors'] == []
            assert not any('nuova_funzione' in w['message'] for w in audit['warnings'])
            window._show_element_tab(tab, new_node.uid)
            QApplication.processEvents()
            leaf_page = window.tabs.currentWidget()
            assert not any('Aggiungi un elemento' in button.text() for button in leaf_page.findChildren(type(window.add_file_btn)))
            window.close()

    def test_add_file_block_creates_and_opens_language_aware_file(self):
        # regression for the "+ Nuovo file" button under the project tree
        with _temp_dir() as tmp:
            project = Path(tmp)
            window = MainWindow()
            window.project_root_path = str(project)
            window.git_root = None
            window.git_status = {}

            window._ide_new_file_form = lambda **kwargs: ('utils.py', 'x = 1\n')
            window.add_file_btn.click()
            created = project / 'utils.py'
            assert created.exists()
            assert created.read_text(encoding='utf-8') == 'x = 1\n'
            assert str(created) in window.open_tabs
            window.close()

    def test_ai_review_offered_as_chat_card_and_skipped_in_automatic_mode(self):
        # regression: the AI review used to pop a full window unasked on every AI turn that
        # touched files. Non-automatic mode now gets a small inline chat card instead
        # (ClaudePane.offer_ai_review); automatic mode skips asking entirely and auto-accepts.
        with _temp_dir() as tmp:
            project = Path(tmp)
            source = project / 'app.py'
            source.write_text('x = 1\n', encoding='utf-8')
            window = MainWindow()
            window.project_root_path = str(project)
            window._ai_snapshot = str(project.parent / 'snap')
            window._closing = False
            import shutil as _shutil
            _shutil.copytree(str(project), window._ai_snapshot)
            window._clear_ai_snapshot_marker = lambda: None
            source.write_text('x = 2\n', encoding='utf-8')

            offered = []
            window.claude_pane.offer_ai_review = lambda review, render_text, resolved: offered.append((review, resolved))
            window.claude_pane.auto_permissions.setChecked(False)
            window._finish_ai_review()
            assert len(offered) == 1  # non-automatic mode: offered as a card, not applied yet
            assert source.read_text(encoding='utf-8') == 'x = 2\n'  # already on disk, review is just bookkeeping

            # automatic mode: no card offered at all, changes silently accepted (snapshot cleared)
            _shutil.rmtree(window._ai_snapshot, ignore_errors=True)
            _shutil.copytree(str(project), window._ai_snapshot)
            source.write_text('x = 3\n', encoding='utf-8')
            offered.clear()
            window.claude_pane.auto_permissions.setChecked(True)
            window._finish_ai_review()
            assert offered == []
            assert window._ai_snapshot is None
            window.close()

    def test_lsp_client_backoff_after_crash_resets_on_successful_init(self):
        # regression: a server that crashes right after starting used to be retried on every single
        # keystroke (update_document's "process is None -> restart" check has no cooldown of its
        # own). _start now refuses to launch while inside the backoff window; the window is cleared
        # once a real 'initialize' response comes back, not just once a process object exists.
        client = LspClient()
        assert client._crash_count == 0 and client._restart_backoff_until == 0.0

        client._record_crash()
        assert client._crash_count == 1
        assert client._restart_backoff_until > time.monotonic()
        # still within backoff: _start must refuse without even trying to spawn a process
        assert client._start('nonexistent-binary', [], 'fake', os.getcwd()) is False
        assert client.process is None

        client._record_crash()
        assert client._crash_count == 2
        second_backoff = client._restart_backoff_until
        assert second_backoff > time.monotonic()  # exponential: still growing, not reset

        # simulate a real successful initialize response
        client._restart_backoff_until = 0.0
        client._handle_message({'id': 'stamp-init-id', 'result': {}})  # id mismatch -> ignored
        assert client._crash_count == 2  # confirms the mismatched-id message above was a no-op
        client.init_id = 'real-id'
        client._handle_message({'id': 'real-id', 'result': {'capabilities': {}}})
        assert client.ready is True
        assert client._crash_count == 0

    def test_refresh_after_fs_change_dedupes_pending_watcher_debounce(self):
        # regression: a direct _refresh_after_fs_change() call (create/rename/delete) and the
        # QFileSystemWatcher's own ~400ms-debounced follow-up for that same disk change used to
        # both fire, doing the full os.walk project-tree rebuild twice for one operation.
        with _temp_dir() as tmp:
            window = MainWindow()
            window.project_root_path = str(tmp)
            window.fs_refresh_timer.start(400)
            assert window.fs_refresh_timer.isActive()
            window._refresh_after_fs_change()
            assert not window.fs_refresh_timer.isActive()
            window.close()

    def test_saving_a_tab_arms_the_left_tree_refresh_timer(self):
        # regression: the left "Codice" tree only ever repopulated via _rebuild_tree, which itself
        # only ran through the QFileSystemWatcher noticing a directory change — _on_tab_saved never
        # triggered a refresh directly, so an in-place atomic save (write_file_atomic: mkstemp +
        # os.replace over the SAME filename) could leave the tree stale if the OS/Qt watcher backend
        # didn't treat that as a directory change. Saving now arms the same debounce timer directly,
        # exactly like create/rename/delete already do for their own filesystem mutations.
        with _temp_dir() as tmp:
            project = Path(tmp)
            source = project / 'sample.py'
            source.write_text('# [MOD OPEN] sample.py\npass\n# [MOD CLOSED] sample.py\n', encoding='utf-8')
            window = MainWindow()
            window.project_root_path = str(project)
            assert window._open_file(str(source))
            tab = window.open_tabs[str(source)]
            assert not window.fs_refresh_timer.isActive()
            tab.tree.body.append(build_new_element_node('FN', 'added_later', 'desc', 'Python'))
            tab.mark_dirty()
            assert tab.save()
            assert window.fs_refresh_timer.isActive()  # armed directly by the save, not left to chance
            window.close()

    def test_rollback_snapshot_reports_undeletable_directories_instead_of_swallowing(self):
        # regression: rollback_snapshot's directory-cleanup pass used to `except OSError: pass`
        # silently — a leftover directory nobody could see any trace of. It now returns the list of
        # directories it couldn't remove, and _finish_ai_review's resolved() reports them.
        with _temp_dir() as tmp:
            root = Path(tmp) / 'root'
            snapshot = Path(tmp) / 'snap'
            root.mkdir()
            snapshot.mkdir()
            (root / 'leftover_dir').mkdir()
            (root / 'leftover_dir' / 'file.txt').write_text('x', encoding='utf-8')

            original_rmdir = os.rmdir

            def failing_rmdir(path):
                if 'leftover_dir' in path:
                    raise OSError('simulated: directory not empty')
                original_rmdir(path)

            os.rmdir = failing_rmdir
            try:
                skipped = rollback_snapshot(str(root), str(snapshot), set())
            finally:
                os.rmdir = original_rmdir
            assert skipped == ['leftover_dir']

    def test_ai_chat_stream_render_is_throttled_but_never_drops_the_tail(self):
        # regression: every stdout chunk re-rendered the ENTIRE accumulated response through
        # _markdown_to_html — O(n^2) over a long streamed reply. Rendering is now throttled to
        # ~25/s via _stream_render_timer, but the final chunk must still land: _reset_process force-
        # flushes any pending throttled render instead of dropping it when the process finishes.
        pane = ClaudePane(os.getcwd())
        try:
            pane._add_message('domanda', 'user')
            pane._append_stream('parte 1 ')
            pane._append_stream('parte 2 ')
            pane._append_stream('parte 3')
            stream_label = pane._stream_label
            # nothing rendered yet — still inside the 40ms throttle window
            assert 'parte 3' not in stream_label.text()
            pane._reset_process()  # simulates the process finishing right after a burst
            assert 'parte 1' in stream_label.text() and 'parte 3' in stream_label.text()
        finally:
            pane.deleteLater()

    def test_ai_chat_attachments_show_as_chips_and_ride_along_in_the_prompt(self):
        # regression for "implementa la possibilità di allegare documenti e immagini": neither
        # claude -p nor codex exec has a multimodal-upload flag, so an attachment is just a path
        # named in the (visible, not hidden like context_hint) prompt text — the CLI's own Read
        # tool opens it, image or text, the same way context_hint already tells it to read the
        # focused file itself.
        pane = ClaudePane(os.getcwd())
        try:
            assert pane.attachments_row.isHidden()  # nothing attached yet

            pane._pending_attachments = [
                ('C:/tmp/screenshot.png', 'C:/tmp/screenshot.png'),
                ('C:/tmp/spec.pdf', 'C:/tmp/spec.pdf'),
            ]
            pane._refresh_attachment_chips()
            assert not pane.attachments_row.isHidden()
            assert pane.attachments_layout.count() == 3  # 2 chips + trailing stretch

            pane._remove_attachment('C:/tmp/screenshot.png')
            assert pane._pending_attachments == [('C:/tmp/spec.pdf', 'C:/tmp/spec.pdf')]
            assert not pane.attachments_row.isHidden()  # one still left

            pane.prompt.setPlainText('cosa vedi in questo file?')
            captured = {}
            pane.run_prompt = lambda prompt, **kwargs: captured.setdefault('prompt', prompt) or True
            pane._send()
            assert 'cosa vedi in questo file?' in captured['prompt']
            assert 'C:/tmp/spec.pdf' in captured['prompt']  # the path rides along in the same text
            assert pane._pending_attachments == []  # cleared after a successful send
            assert pane.attachments_row.isHidden()
        finally:
            pane.deleteLater()

    def test_compress_attached_image_downscales_and_reencodes_as_jpeg(self):
        # regression for the "risparmio token ma lossy" toggle: a real image gets scaled down and
        # re-encoded as JPEG at reduced quality — smaller on disk, a different (temp) path — while
        # a non-image extension and an unreadable/corrupt path both pass through unchanged rather
        # than silently dropping the attachment.
        with _temp_dir() as tmp:
            big = QImage(2000, 1500, QImage.Format_RGB32)
            big.fill(0xFFEECC11)
            big_path = os.path.join(tmp, 'big.png')
            big.save(big_path)
            original_size = os.path.getsize(big_path)

            compressed = compress_attached_image(big_path, max_dimension=800, quality=50)
            assert compressed != big_path
            assert os.path.isfile(compressed)
            result_image = QImage(compressed)
            assert max(result_image.width(), result_image.height()) <= 800
            assert os.path.getsize(compressed) < original_size

            # not a recognized image extension -> unchanged, no attempt to open it as one
            text_path = os.path.join(tmp, 'notes.txt')
            with open(text_path, 'w', encoding='utf-8') as f:
                f.write('plain text')
            assert compress_attached_image(text_path) == text_path

            # a .png that isn't actually a valid image -> QImage.isNull() -> unchanged, not dropped
            fake_png = os.path.join(tmp, 'fake.png')
            with open(fake_png, 'wb') as f:
                f.write(b'not a real png')
            assert compress_attached_image(fake_png) == fake_png

    def test_convert_attached_document_falls_back_when_markitdown_unavailable_or_text_free(self):
        # regression for "implementa il framework markitdown... se non rilevi testo non attivarlo
        # e pushalo direttamente così": three fallback-to-original paths, all correct per the ask —
        # (1) an extension convert_attached_document doesn't even try to convert, (2) markitdown
        # not installed (a real, un-mocked ImportError in this environment — it's an optional
        # dependency, same as an optional language server), (3) markitdown installed but the
        # document has no extractable text (a scanned/image-only PDF).
        with _temp_dir() as tmp:
            txt_path = os.path.join(tmp, 'notes.txt')
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write('already plain text')
            assert convert_attached_document(txt_path) == txt_path  # extension not routed at all

            pdf_path = os.path.join(tmp, 'doc.pdf')
            with open(pdf_path, 'wb') as f:
                f.write(b'%PDF-1.4 not a real pdf')
            assert convert_attached_document(pdf_path) == pdf_path  # markitdown not installed here

            # simulate markitdown being installed but finding no text (scanned/image-only PDF)
            class _FakeResult:
                text_content = '   '  # whitespace-only -> "no text detected"

            class _FakeMarkItDown:
                def convert(self, _path):
                    return _FakeResult()

            fake_module = type(sys)('markitdown')
            fake_module.MarkItDown = _FakeMarkItDown
            sys.modules['markitdown'] = fake_module
            try:
                assert convert_attached_document(pdf_path) == pdf_path
            finally:
                del sys.modules['markitdown']

    def test_convert_attached_document_writes_markdown_when_text_is_found(self):
        # the success path: markitdown installed and returns real text -> a new .md temp file is
        # written and returned, named after the original so the attachment chip stays readable
        with _temp_dir() as tmp:
            pdf_path = os.path.join(tmp, 'spec.pdf')
            with open(pdf_path, 'wb') as f:
                f.write(b'%PDF-1.4 not a real pdf')

            class _FakeResult:
                text_content = '# Spec\n\nThis is the extracted content.'

            class _FakeMarkItDown:
                def convert(self, _path):
                    return _FakeResult()

            fake_module = type(sys)('markitdown')
            fake_module.MarkItDown = _FakeMarkItDown
            sys.modules['markitdown'] = fake_module
            try:
                result = convert_attached_document(pdf_path)
            finally:
                del sys.modules['markitdown']
            assert result != pdf_path
            assert result.endswith('.md')
            assert 'spec' in os.path.basename(result)
            with open(result, encoding='utf-8') as f:
                assert f.read() == '# Spec\n\nThis is the extracted content.'
            os.remove(result)

    def test_attach_files_applies_lossy_toggle_only_to_images(self):
        # wiring check for ClaudePane._attach_files: compress_attached_image only runs when
        # lossy_images is checked, and never for a document (convert_attached_document already
        # resolved — or intentionally left alone — before the lossy check is even reached)
        with _temp_dir() as tmp:
            img_path = os.path.join(tmp, 'shot.png')
            QImage(400, 300, QImage.Format_RGB32).save(img_path)

            pane = ClaudePane(os.getcwd())
            try:
                pane.lossy_images.setChecked(False)
                original_get_names = kant_widgets_module.QFileDialog.getOpenFileNames
                kant_widgets_module.QFileDialog.getOpenFileNames = lambda *a, **k: ([img_path], '')
                try:
                    pane._attach_files()
                    assert pane._pending_attachments == [(img_path, img_path)]  # toggle off -> unchanged

                    pane._pending_attachments = []
                    pane.lossy_images.setChecked(True)
                    pane._attach_files()
                    original, resolved = pane._pending_attachments[0]
                    assert original == img_path and resolved != img_path  # toggle on -> compressed
                finally:
                    kant_widgets_module.QFileDialog.getOpenFileNames = original_get_names
            finally:
                pane.deleteLater()

    def test_grouping_create_persist_and_navigate(self):
        # regression for the new "raggruppamento" feature: an arbitrary named bundle of elements
        # from different files/parents (kant/groupings.py), created via the "+ Nuovo gruppo" button,
        # shown in "Gruppi" view mode, and navigable by clicking a member (reuses
        # _navigate_to_element, the same the Incoming/Outgoing panel already uses).
        with _temp_dir() as tmp:
            project = Path(tmp)
            (project / 'auth.py').write_text('\n'.join([
                '# [MOD OPEN] auth.py',
                '# [FN OPEN] login', 'def login(): pass', '# [FN CLOSED] login',
                '# [MOD CLOSED] auth.py',
            ]), encoding='utf-8')
            (project / 'server.py').write_text('\n'.join([
                '# [MOD OPEN] server.py',
                '# [FN OPEN] handle', 'def handle(): pass', '# [FN CLOSED] handle',
                '# [MOD CLOSED] server.py',
            ]), encoding='utf-8')
            window = MainWindow()
            window.project_root_path = str(project)
            window.git_root = None
            window.git_status = {}
            window._build_project_tree(window.tree.invisibleRootItem(), str(project))

            trees = {
                'auth.py': parse_kant((project / 'auth.py').read_text(encoding='utf-8')),
                'server.py': parse_kant((project / 'server.py').read_text(encoding='utf-8')),
            }
            window._xref_cache = build_xref(trees)
            fn_keys = [key for key, el in window._xref_cache.items() if el.tag == 'FN']
            assert len(fn_keys) == 2  # one from each file — a grouping mixing files/parents

            window._ide_new_grouping_form = lambda elements, preselected=(): ('Auth flow', fn_keys)
            assert window.add_grouping_btn is not None
            window.add_grouping_btn.click()

            assert window.view_mode == 'groups'  # switched automatically to show the new group
            saved = load_groupings(str(project))
            assert len(saved) == 1 and saved[0].name == 'Auth flow' and set(saved[0].members) == set(fn_keys)

            # the tree shows one 'grouping' row with two 'grouping_member' children
            it = window.tree.invisibleRootItem()
            group_items = [it.child(i) for i in range(it.childCount()) if it.child(i).data(0, ROLE_KIND) == 'grouping']
            assert len(group_items) == 1
            member_items = [group_items[0].child(i) for i in range(group_items[0].childCount())]
            assert len(member_items) == 2
            assert {item.data(0, ROLE_KIND) for item in member_items} == {'grouping_member'}

            # clicking a member navigates to its file via the existing xref-key navigation path
            target_key = member_items[0].data(0, ROLE_KEY)
            target_rel = target_key.split('::', 1)[0]
            window._on_tree_item_clicked(member_items[0], 0)
            assert window.active_tab is not None
            assert os.path.basename(window.active_tab.path) == target_rel
            window.close()

    def test_rename_file_migrates_grouping_and_mappa_position(self):
        # regression for the rename-stability gap: _rename_tree_item used to touch neither
        # .kant/groupings.json nor MAPPA's QSettings-persisted node coordinates, so renaming a file
        # silently orphaned any grouping referencing it and lost its manually dragged MAPPA position
        with _temp_dir() as tmp:
            project = Path(tmp)
            (project / 'auth.py').write_text('\n'.join([
                '# [MOD OPEN] auth.py',
                '# [FN OPEN #abc123] login', 'def login(): pass', '# [FN CLOSED #abc123] login',
                '# [MOD CLOSED] auth.py',
            ]), encoding='utf-8')
            window = MainWindow()
            window.project_root_path = str(project)

            old_key = 'auth.py::abc123'
            grouping = new_grouping('Auth flow')
            grouping.members = [old_key]
            save_groupings(str(project), [grouping])

            position_key = _position_settings_key(str(project))
            settings = QSettings('KANT', 'KANT Editor')
            settings.setValue(position_key, json.dumps({old_key: [12.0, 34.0]}))

            item = QTreeWidgetItem()
            item.setData(0, ROLE_PATH, str(project / 'auth.py'))
            window._ide_text = lambda *a, **k: ('auth2.py', True)
            window._rename_tree_item(item, 'file')

            assert (project / 'auth2.py').is_file() and not (project / 'auth.py').exists()
            saved = load_groupings(str(project))
            assert saved[0].members == ['auth2.py::abc123']
            data = json.loads(settings.value(position_key, '{}'))
            assert data == {'auth2.py::abc123': [12.0, 34.0]}
            settings.remove(position_key)
            window.close()

    def test_rename_folder_migrates_multiple_grouped_members(self):
        with _temp_dir() as tmp:
            project = Path(tmp)
            (project / 'utils').mkdir()
            (project / 'utils' / 'a.py').write_text(
                '# [FN OPEN #aaa] a\npass\n# [FN CLOSED #aaa] a\n', encoding='utf-8',
            )
            (project / 'utils' / 'b.py').write_text(
                '# [FN OPEN #bbb] b\npass\n# [FN CLOSED #bbb] b\n', encoding='utf-8',
            )
            (project / 'outside.py').write_text(
                '# [FN OPEN #ccc] c\npass\n# [FN CLOSED #ccc] c\n', encoding='utf-8',
            )
            window = MainWindow()
            window.project_root_path = str(project)

            grouping = new_grouping('Utils bundle')
            grouping.members = ['utils/a.py::aaa', 'utils/b.py::bbb', 'outside.py::ccc']
            save_groupings(str(project), [grouping])

            item = QTreeWidgetItem()
            item.setData(0, ROLE_PATH, str(project / 'utils'))
            window._ide_text = lambda *a, **k: ('helpers', True)
            window._rename_tree_item(item, 'dir')

            assert (project / 'helpers').is_dir() and not (project / 'utils').exists()
            saved = load_groupings(str(project))
            assert set(saved[0].members) == {'helpers/a.py::aaa', 'helpers/b.py::bbb', 'outside.py::ccc'}
            window.close()

    def test_audit_kant_headers_flags_tag_and_name_inconsistencies(self):
        tag_mismatch = '\n'.join([
            '# [CLS CATEGORY] load_user - reads a user',
            '# [FN] load_user - fetch by id',
            '# [FN OPEN #abc] load_user',
            'def load_user(): pass',
            '# [FN CLOSED #abc] load_user',
        ])
        result = audit_kant_headers(tag_mismatch)
        assert any('incoerente' in e['message'] and e['line'] == 1 for e in result['errors'])

        name_mismatch = '\n'.join([
            '# [FN CATEGORY] other_name - reads a user',
            '# [FN] load_user - fetch by id',
            '# [FN OPEN #abc] load_user',
            'def load_user(): pass',
            '# [FN CLOSED #abc] load_user',
        ])
        result2 = audit_kant_headers(name_mismatch)
        assert any('incoerente' in e['message'] and e['line'] == 1 for e in result2['errors'])

        tagline_name_mismatch = '\n'.join([
            '# [FN CATEGORY] load_user - reads a user',
            '# [FN] other_name - fetch by id',
            '# [FN OPEN #abc] load_user',
            'def load_user(): pass',
            '# [FN CLOSED #abc] load_user',
        ])
        result3 = audit_kant_headers(tagline_name_mismatch)
        assert any('riga descrittiva' in e['message'] and e['line'] == 2 for e in result3['errors'])

    def test_audit_kant_headers_flags_orphaned_pending_header(self):
        src = '\n'.join([
            '# [FN CATEGORY] load_user - reads a user',
            'x = 1  # never followed by an OPEN',
        ])
        result = audit_kant_headers(src)
        assert any('pendente' in e['message'] and e['line'] == 1 for e in result['errors'])

    def test_check_kant_markers_reports_real_line_for_duplicate_id(self):
        # regression for a confirmed bug: the duplicate-#id check used to hardcode 'line': 1
        # instead of pointing at the actual duplicate
        src = '\n'.join([
            '# [FN OPEN #aaa] a', 'pass', '# [FN CLOSED #aaa] a',
            '# [FN OPEN #aaa] b', 'pass', '# [FN CLOSED #aaa] b',
        ])
        result = check_kant_markers(src)
        assert result['ok'] is False
        assert result['line'] == 4  # the second OPEN's real line, not the old hardcoded 1

    def test_audit_kant_headers_warns_without_erroring_on_missing_or_empty_headers(self):
        # non-strict mode: missing CATEGORY/tagline are warnings, never hard errors — and a legacy
        # file that was always valid (no CATEGORY/tagline at all) must not gain a NEW hard error
        src = '# [FN OPEN #abc] load_user\ndef load_user(): pass\n# [FN CLOSED #abc] load_user\n'
        result = audit_kant_headers(src)
        assert result['errors'] == []
        messages = [w['message'] for w in result['warnings']]
        assert any('CATEGORY mancante' in m for m in messages)
        assert any('tagline mancante' in m for m in messages)
        assert check_kant_markers(src)['ok'] is True  # the live/save-path check stays permissive too

        empty_tagline = '\n'.join([
            '# [FN CATEGORY] load_user - reads a user',
            '# [FN] load_user —',
            '# [FN OPEN #abc] load_user',
            'def load_user(): pass',
            '# [FN CLOSED #abc] load_user',
        ])
        result2 = audit_kant_headers(empty_tagline)
        assert result2['errors'] == []
        assert any('tagline vuota' in w['message'] for w in result2['warnings'])

        # CATEGORY has no length cap, but a present-yet-empty one ("Name —", nothing after the
        # dash) still needs to be flagged as needing content — mirrors the tagline check above,
        # which already covered this case while CATEGORY silently did not
        empty_category = '\n'.join([
            '# [FN CATEGORY] load_user —',
            '# [FN] load_user - reads a user',
            '# [FN OPEN #abc] load_user',
            'def load_user(): pass',
            '# [FN CLOSED #abc] load_user',
        ])
        result3 = audit_kant_headers(empty_category)
        assert result3['errors'] == []
        assert any('CATEGORY vuota' in w['message'] for w in result3['warnings'])

    def test_audit_kant_headers_warns_on_unknown_tag_without_erroring(self):
        # a tag outside the fixed 8-tag set is a warning, not a hard error — an existing legacy file
        # using a nonstandard tag must still open and validate without a new blocking failure
        src = '# [XYZ OPEN] thing\npass\n# [XYZ CLOSED] thing\n'
        result = audit_kant_headers(src)
        assert result['errors'] == []
        assert any('non appartiene' in w['message'] for w in result['warnings'])

    def test_scan_python_exact_tag_nesting_and_cst_vs_var(self):
        src = '\n'.join([
            'NIGHT = False',
            '',
            'TAG_COLORS = {}',
            '',
            'def set_theme(night):',
            '    global NIGHT',
            '    NIGHT = night',
            '',
            'class Foo:',
            '    def method_a(self):',
            '        pass',
            '',
            'def test_something():',
            '    assert True',
        ])
        elements = {(e.tag, e.name, e.depth): e for e in skeleton.scan_python(src, 'mod.py')}
        assert ('VAR', 'NIGHT', 0) in elements  # reassigned via `global` in set_theme -> VAR
        assert ('CST', 'TAG_COLORS', 0) in elements  # assigned once, never rebound -> CST
        assert ('FN', 'set_theme', 0) in elements
        assert ('CLS', 'Foo', 0) in elements
        assert ('FN', 'method_a', 1) in elements  # nested inside Foo, depth 1
        assert ('TST', 'test_something', 0) in elements  # test_ prefix -> TST, not FN

    def test_scan_regex_handles_braces_inside_strings_and_comments(self):
        src = '\n'.join([
            'function alpha(a) {',
            '  // a comment with a brace } in it',
            '  const s = "a { fake brace";',
            '  return a;',
            '}',
            '',
            'class Beta {',
            '  constructor() {}',
            '}',
        ])
        elements = skeleton.scan_regex(src, 'JavaScript', 'app.js')
        by_name = {e.name: e for e in elements}
        assert by_name['alpha'].tag == 'FN' and by_name['alpha'].end_line == 5
        assert by_name['Beta'].tag == 'CLS' and by_name['Beta'].end_line == 9

    def test_insert_skeleton_keeps_nesting_valid_when_spans_share_an_end_line(self):
        # Foo's own closing line is the same original line method_b's body ends on (no trailing
        # blank line) -- a real regression this insertion order once got backwards, closing the
        # outer class before its own still-open inner method
        src = '\n'.join([
            'class Foo:',
            '    def method_a(self):',
            '        pass',
            '',
            '    def method_b(self):',
            '        pass',
        ])
        elements = skeleton.scan_python(src, 'mod.py')
        new_text, count = skeleton.insert_skeleton(src, elements, 'Python')
        assert count == len(elements)
        tree = parse_kant(new_text)  # raises KantParseError on any crossed/mismatched span
        audit = audit_kant_headers(new_text)
        assert audit['errors'] == []
        assert all(w['message'] in ('CATEGORY vuota', 'tagline vuota') for w in audit['warnings'])

    def test_apply_skeleton_skips_already_marked_elements_and_is_idempotent(self):
        src = '\n'.join([
            '# [FN CATEGORY] alpha — already documented',
            '# [FN] alpha — does the first thing',
            '# [FN OPEN] alpha',
            'def alpha():',
            '    pass',
            '# [FN CLOSED] alpha',
            '',
            'def beta():',
            '    pass',
        ])
        result = skeleton.apply_skeleton(src, 'mod.py')
        assert result is not None
        new_text, count = result
        assert count == 1  # only beta was unmarked
        assert 'already documented' in new_text  # alpha's existing marker untouched
        assert '[FN OPEN] beta' in new_text
        parse_kant(new_text)  # round-trips cleanly
        assert skeleton.apply_skeleton(new_text, 'mod.py') is None  # nothing left to insert

    def test_apply_skeleton_refuses_when_existing_markers_are_broken(self):
        broken = '# [FN OPEN] alpha\ndef alpha(): pass\n# [FN CLOSED] beta\n'
        assert skeleton.unmarked_elements(broken, skeleton.scan_python(broken, 'mod.py')) is None

    def test_apply_skeleton_to_project_walks_and_writes_every_file(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'a.py').write_text('def alpha():\n    pass\n', encoding='utf-8')
            (root / 'b.py').write_text(
                '# [FN CATEGORY] beta — already documented\n'
                '# [FN] beta — does a thing\n'
                '# [FN OPEN] beta\n'
                'def beta():\n    pass\n'
                '# [FN CLOSED] beta\n',
                encoding='utf-8',
            )
            (root / 'broken.py').write_text('# [FN OPEN] x\ndef x(): pass\n# [FN CLOSED] y\n', encoding='utf-8')
            (root / 'readme.txt').write_text('not code', encoding='utf-8')
            changed, skipped = skeleton.apply_skeleton_to_project(str(root))
            changed_files = dict(changed)
            assert changed_files == {'a.py': 1}  # b.py already fully marked, readme.txt unsupported
            assert skipped == ['broken.py']
            assert '[FN OPEN] alpha' in (root / 'a.py').read_text(encoding='utf-8')

    def test_validate_kant_project_distinguishes_all_five_map_states(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text(
                '# [MOD OPEN #abc] app.py\nprint(1)\n# [MOD CLOSED #abc] app.py\n', encoding='utf-8',
            )
            # 1. mappa assente
            _summary, _errors, _visual, state, _warnings = validate_kant_project(str(root), None)
            assert state == 'assente'

            # 2. mappa sincronizzata (write the real canonical output)
            canonical = build_kant_map(str(root), root.name)
            map_path = root / f'KANT_{root.name}.md'
            map_path.write_text(canonical, encoding='utf-8')
            _summary, _errors, _visual, state, _warnings = validate_kant_project(str(root), str(map_path))
            assert state == 'sincronizzata'

            # 3. mappa non sincronizzata — reorder/alter content the old substring check wouldn't
            # have caught (the [MOD app.py] tag+path substring is still present verbatim)
            map_path.write_text(canonical.replace('- ', '  '), encoding='utf-8')
            _summary, _errors, _visual, state, _warnings = validate_kant_project(str(root), str(map_path))
            assert state == 'non_sincronizzata'

            # 4. marker invalidi: mappa non rigenerata — even though the map itself is byte-identical
            # to canonical, a structural marker error elsewhere takes precedence over the comparison
            (root / 'bad.py').write_text('# [FN CLOSED] never_opened\n', encoding='utf-8')
            map_path.write_text(canonical, encoding='utf-8')
            _summary, errors, _visual, state, _warnings = validate_kant_project(str(root), str(map_path))
            assert state == 'marker_invalidi'
            assert any('KANT map non coerente' in e for e in errors) is False  # comparison was skipped, not just failed

            # 5. errore durante la generazione della mappa (unreadable map path)
            (root / 'bad.py').unlink()
            os.chmod(map_path, 0o000)
            try:
                _summary, _errors, _visual, state, _warnings = validate_kant_project(str(root), str(map_path))
                assert state in ('errore_generazione', 'sincronizzata')  # unreadable on this OS, or ignored (root/CI)
            finally:
                os.chmod(map_path, 0o644)

    def test_validate_kant_project_flags_duplicate_uid_across_files_as_warning(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'a.py').write_text('# [FN OPEN #dupe123] f\npass\n# [FN CLOSED #dupe123] f\n', encoding='utf-8')
            (root / 'b.py').write_text('# [FN OPEN #dupe123] g\npass\n# [FN CLOSED #dupe123] g\n', encoding='utf-8')
            summary, errors, _visual, _state, warnings = validate_kant_project(str(root), None)
            assert any('UID duplicato' in w and 'dupe123' in w for w in warnings)
            assert not any('UID duplicato' in e for e in errors)  # warning, never a hard error
            assert 'UID duplicato' not in summary  # doesn't leak into the ERRORI block

    def test_tagline_edit_and_file_rename_are_reflected_in_generated_map(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            path = root / 'app.py'
            path.write_text(
                '# [MOD CATEGORY] app.py - original purpose\n# [MOD app.py] — original purpose\n'
                '# [MOD OPEN #abc] app.py\nprint(1)\n# [MOD CLOSED #abc] app.py\n', encoding='utf-8',
            )
            before = build_kant_map(str(root), root.name)
            assert '[MOD app.py]' in before and 'original purpose' in before

            # tagline-only edit, same file/name — the description in the generated map must follow
            path.write_text(
                '# [MOD CATEGORY] app.py - updated purpose\n# [MOD app.py] — updated purpose\n'
                '# [MOD OPEN #abc] app.py\nprint(1)\n# [MOD CLOSED #abc] app.py\n', encoding='utf-8',
            )
            after_edit = build_kant_map(str(root), root.name)
            assert 'updated purpose' in after_edit and 'original purpose' not in after_edit

            path.write_text('# [MOD OPEN #abc] renamed.py\nprint(1)\n# [MOD CLOSED #abc] renamed.py\n', encoding='utf-8')
            os.rename(path, root / 'renamed.py')
            after_rename = build_kant_map(str(root), root.name)
            assert '[MOD renamed.py]' in after_rename and '[MOD app.py]' not in after_rename

    def test_sync_kant_map_skips_write_when_canonical_content_unchanged(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text(
                '# [MOD OPEN #abc] app.py\nprint(1)\n# [MOD CLOSED #abc] app.py\n', encoding='utf-8',
            )
            w = MainWindow.__new__(MainWindow)
            w.project_root_path = str(root)
            w.kant_map_path = None
            w.kant_map_label = LabelStub()
            w._xref_cache = None
            w._xref_generation = 0
            w._xref_pending_generation = None
            w.map_dialog = None
            w._map_sync_generation = 0
            w._map_sync_running = False
            w._map_sync_rerun_needed = False
            w._run_background = lambda work, done: done(work(), None)
            MainWindow._sync_kant_map(w)
            map_path = root / f'KANT_{root.name}.md'
            mtime_before = map_path.stat().st_mtime_ns
            # same canonical content, only line-ending/trailing-newline differences — must not rewrite.
            # newline='' disables Path.write_text's own universal-newline translation, so the literal
            # \r\n below actually lands on disk instead of being doubled by write-side translation.
            existing = map_path.read_text(encoding='utf-8')
            modified = existing.rstrip('\n').replace('\n', '\r\n') + '\r\n\r\n'
            with open(map_path, 'w', encoding='utf-8', newline='') as f:
                f.write(modified)
            mtime_touched = map_path.stat().st_mtime_ns
            MainWindow._sync_kant_map(w)
            assert map_path.stat().st_mtime_ns == mtime_touched  # untouched by the no-op sync

    def test_ai_review_manual_resolution_gates_definitive_refresh_and_map_sync(self):
        # the core regression this hardening pass targets: the CLI process finishing must NOT be
        # treated as the transaction's end while a manual review card is still unresolved — no
        # definitive validation/map-sync/tree-refresh/xref-rebuild before the user actually decides
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text(
                '# [MOD OPEN #abc] app.py\nprint(1)\n# [MOD CLOSED #abc] app.py\n', encoding='utf-8',
            )
            window = MainWindow()
            window.project_root_path = str(root)
            window._ai_snapshot = create_snapshot(str(root), set())
            (root / 'app.py').write_text(
                '# [MOD OPEN #abc] app.py\nprint(2)\n# [MOD CLOSED #abc] app.py\n', encoding='utf-8',
            )
            window.claude_pane.auto_permissions.setChecked(False)
            offered = []
            window.claude_pane.offer_ai_review = lambda review, render_text, resolved: offered.append((review, resolved))
            sync_calls = []
            window._sync_kant_map = lambda: sync_calls.append(True)

            window._finish_ai_review()
            assert len(offered) == 1
            assert sync_calls == []  # nothing definitive ran yet — the review card is still pending

            _review, resolved = offered[0]
            resolved('apply', {'app.py': set()}, {})  # reject every hunk, but resolve the review
            assert sync_calls == [True]  # only now, after resolution, did the map-sync path run
            window.close()

    def test_ai_review_apply_generates_first_map_and_surfaces_warnings(self):
        # regression for a real bug found in code review: resolved()'s apply branch used to call
        # validate_kant_project() directly with a stale self.kant_map_path (never refreshed via
        # _check_kant_map first) and never appended `warnings` to the chat message — so a first-ever
        # AI-driven tagging run showed a confusing "manca KANT_*.md" error in chat even while
        # silently creating the file in the background, and every audit_kant_headers warning
        # (missing headers, unknown tags, cross-file duplicate ids) was dropped after every apply.
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text(
                '# [MOD OPEN #abc] app.py\nprint(1)\n# [MOD CLOSED #abc] app.py\n', encoding='utf-8',
            )
            # a second tagged file with no CATEGORY/tagline at all — a real audit_kant_headers
            # warning, present from the start so it's still there after the (unrelated) AI edit
            (root / 'warn.py').write_text(
                '# [FN OPEN #def] helper\npass\n# [FN CLOSED #def] helper\n', encoding='utf-8',
            )
            window = MainWindow()
            window.project_root_path = str(root)
            window._run_background = lambda work, done: done(work(), None)  # deterministic, no race
            assert not (root / f'KANT_{root.name}.md').exists()  # no map yet — starts 'assente'
            window._ai_snapshot = create_snapshot(str(root), set())
            (root / 'app.py').write_text(
                '# [MOD OPEN #abc] app.py\nprint(2)\n# [MOD CLOSED #abc] app.py\n', encoding='utf-8',
            )
            window.claude_pane.auto_permissions.setChecked(False)
            offered = []
            window.claude_pane.offer_ai_review = lambda review, render_text, resolved: offered.append((review, resolved))
            messages = []
            window.claude_pane.write_info = lambda text: messages.append(text)

            window._finish_ai_review()
            _review, resolved = offered[0]
            resolved('apply', {'app.py': {0}}, {})

            assert (root / f'KANT_{root.name}.md').exists()  # generated even though state was 'assente'
            combined = '\n'.join(messages)
            assert 'manca KANT' not in combined  # stale error text no longer shown alongside the fix
            assert 'CATEGORY mancante' in combined or 'tagline mancante' in combined  # warnings now surface
            window.close()

    def test_ai_review_rollback_does_not_normalize_ids_or_touch_map(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'app.py').write_text('# [MOD OPEN] app.py\nprint(1)\n# [MOD CLOSED] app.py\n', encoding='utf-8')
            window = MainWindow()
            window.project_root_path = str(root)
            window._ai_snapshot = create_snapshot(str(root), set())
            (root / 'app.py').write_text('# [MOD OPEN] app.py\nprint(2)\n# [MOD CLOSED] app.py\n', encoding='utf-8')
            window.claude_pane.auto_permissions.setChecked(False)
            offered = []
            window.claude_pane.offer_ai_review = lambda review, render_text, resolved: offered.append((review, resolved))
            sync_calls = []
            window._sync_kant_map = lambda: sync_calls.append(True)

            window._finish_ai_review()
            _review, resolved = offered[0]
            resolved('reject', {}, {})
            assert sync_calls == []  # rollback never syncs the map — the snapshot restored it already
            assert 'print(1)' in (root / 'app.py').read_text(encoding='utf-8')  # restored, unmodified further
            window.close()

    def test_normalize_missing_ids_stamps_only_kept_files_and_skips_invalid_or_binary(self):
        with _temp_dir() as tmp:
            root = Path(tmp)
            legacy = root / 'legacy.py'
            legacy.write_text('# [FN OPEN] f\npass\n# [FN CLOSED] f\n', encoding='utf-8')
            already_id = root / 'has_id.py'
            already_id.write_text('# [FN OPEN #existing] g\npass\n# [FN CLOSED #existing] g\n', encoding='utf-8')
            broken = root / 'broken.py'
            broken.write_text('# [FN CLOSED] never_opened\n', encoding='utf-8')

            normalized, skipped = normalize_missing_ids(str(root), ['legacy.py', 'has_id.py', 'broken.py', 'missing.py'])
            assert normalized == ['legacy.py']  # only the file that actually lacked an id was rewritten
            assert '#' in legacy.read_text(encoding='utf-8').splitlines()[0]
            assert already_id.read_text(encoding='utf-8') == '# [FN OPEN #existing] g\npass\n# [FN CLOSED #existing] g\n'
            skipped_reasons = dict(skipped)
            assert 'broken.py' in skipped_reasons and 'marker' in skipped_reasons['broken.py']
            assert 'missing.py' in skipped_reasons

    def test_ai_context_page_cleared_on_tab_close_does_not_crash(self):
        # regression for a real user-reported crash: _ai_context_page kept pointing at a FileTab
        # after it closed; a background callback landing afterward (e.g. a git-status refresh
        # finishing) walked _update_action_buttons -> refresh_focus_label -> _build_ai_focus_summary
        # -> _visible_ai_context_uid, which touched the closed tab's (also closed) scroll_area and
        # crashed with "RuntimeError: libshiboken: Internal C++ object (QScrollArea) already deleted".
        with _temp_dir() as tmp:
            project = Path(tmp)
            source = project / 'a.py'
            source.write_text('\n'.join([
                '# [MOD OPEN] a.py',
                '# [FN OPEN] alpha', 'def alpha(): pass', '# [FN CLOSED] alpha',
                '# [MOD CLOSED] a.py',
            ]), encoding='utf-8')
            window = MainWindow()
            window.project_root_path = str(project)
            window.git_root = None
            window.git_status = {}
            window._build_project_tree(window.tree.invisibleRootItem(), str(project))
            assert window._open_file(str(source))
            tab = window.open_tabs[str(source)]
            window._set_ai_context_page(tab)
            assert window._ai_context_page is tab

            window._close_tab(window.tabs.indexOf(tab))
            assert window._ai_context_page is None  # cleared, not left dangling on the closed tab

            # would previously raise RuntimeError here: _ai_context_page pointed at the now-closed
            # (and C++-deleted) tab, whose scroll_area is deleted right along with it
            window._update_action_buttons()
            window.close()

    def test_context_menu_add_kant_element_to_group(self):
        # regression for "right-click a KANT element -> add to group (or create the first one with
        # it preselected)": exercises _xref_key_for_tree_item for both a top-level 'file' row (a
        # MOD) and a nested 'section' row (an FN), then the two right-click paths in
        # _show_tree_context_menu — no groups yet vs. an existing group to add into.
        # QMenu.exec() is a real blocking native call — unlike QMessageBox.critical (a plain Python
        # staticmethod the crash-handler test overrides directly on the class), overriding
        # QMenu.exec at the class level does NOT intercept it (verified: it still blocks forever
        # offscreen). QMenu.__init__ is patched instead, to stamp an instance-level .exec override
        # onto every menu as it's constructed — same effect as this file's own working precedent
        # (test_git_button_hover_does_not_show_dropdown patches a specific menu INSTANCE's .exec),
        # just applied at creation time since _show_tree_context_menu's menu is a fresh local, not
        # one already reachable from the test.
        with _temp_dir() as tmp:
            root = Path(tmp)
            (root / 'auth.py').write_text('\n'.join([
                '# [MOD OPEN] auth.py',
                '# [FN OPEN] login', 'def login(): pass', '# [FN CLOSED] login',
                '# [MOD CLOSED] auth.py',
            ]), encoding='utf-8')
            (root / 'server.py').write_text('\n'.join([
                '# [MOD OPEN] server.py',
                '# [FN OPEN] handle', 'def handle(): pass', '# [FN CLOSED] handle',
                '# [MOD CLOSED] server.py',
            ]), encoding='utf-8')

            window = MainWindow()
            window.project_root_path = str(root)
            window.git_root = None
            window.git_status = {}
            window._build_project_tree(window.tree.invisibleRootItem(), str(root))
            trees = {
                'auth.py': parse_kant((root / 'auth.py').read_text(encoding='utf-8')),
                'server.py': parse_kant((root / 'server.py').read_text(encoding='utf-8')),
            }
            window._xref_cache = build_xref(trees)

            auth_file_item = next(
                window.tree.topLevelItem(i) for i in range(window.tree.topLevelItemCount())
                if window.tree.topLevelItem(i).data(0, ROLE_PATH) == str(root / 'auth.py')
            )
            assert auth_file_item.data(0, ROLE_KIND) == 'file'
            auth_key = window._xref_key_for_tree_item(auth_file_item)
            assert auth_key is not None and window._xref_cache[auth_key].tag == 'MOD'

            login_item = auth_file_item.child(0)
            assert login_item.data(0, ROLE_KIND) == 'section'
            login_key = window._xref_key_for_tree_item(login_item)
            assert login_key is not None and window._xref_cache[login_key].tag == 'FN'

            server_file_item = next(
                window.tree.topLevelItem(i) for i in range(window.tree.topLevelItemCount())
                if window.tree.topLevelItem(i).data(0, ROLE_PATH) == str(root / 'server.py')
            )
            server_key = window._xref_key_for_tree_item(server_file_item)

            captured = {}
            window._ide_new_grouping_form = lambda elements, preselected=(): (
                captured.__setitem__('preselected', preselected) or ('Backend', list(preselected))
            )

            # stamps every QMenu (main menu and any submenu) with an instance-level .exec override
            # at construction time, picking whichever action `picker[0]` currently selects
            picker = [None]
            original_init = QMenu.__init__

            def patched_init(menu_self, *a, **k):
                original_init(menu_self, *a, **k)
                menu_self.exec = lambda *_a, **_k: picker[0](menu_self)

            QMenu.__init__ = patched_init
            try:
                picker[0] = lambda menu_self: next(
                    a for a in menu_self.actions() if a.text() == 'Aggiungi a un nuovo gruppo…'
                )
                window._show_tree_context_menu(window.tree.visualItemRect(auth_file_item).center())

                assert captured['preselected'] == (auth_key,)
                groupings = load_groupings(str(root))
                assert len(groupings) == 1 and groupings[0].members == [auth_key]

                # creating the group switched view_mode to 'groups', which cleared and rebuilt the
                # whole tree — server_file_item's underlying QTreeWidgetItem is gone; re-resolve it
                # against a fresh 'code' rebuild instead of the now-deleted C++ object
                window._set_view_mode('code')
                server_file_item = next(
                    window.tree.topLevelItem(i) for i in range(window.tree.topLevelItemCount())
                    if window.tree.topLevelItem(i).data(0, ROLE_PATH) == str(root / 'server.py')
                )
                assert window._xref_key_for_tree_item(server_file_item) == server_key

                def pick_existing_group(menu_self):
                    for action in menu_self.actions():
                        submenu = action.menu()
                        if submenu is not None:
                            return submenu.actions()[0]
                    raise AssertionError('no "Aggiungi a un gruppo" submenu offered')

                picker[0] = pick_existing_group
                window._show_tree_context_menu(window.tree.visualItemRect(server_file_item).center())
            finally:
                QMenu.__init__ = original_init

            groupings = load_groupings(str(root))
            assert len(groupings) == 1
            assert set(groupings[0].members) == {auth_key, server_key}
            window.close()

    def test_new_project_creates_folder_with_starter_module_and_opens(self):
        with _temp_dir() as tmp:
            window = MainWindow()
            window._ide_yes_no = lambda *a, **k: False  # decline the "generate KANT map?" follow-up
            window._ide_new_project_form = lambda *a, **k: {
                'name': 'my-project', 'parent_dir': tmp, 'language': 'Python',
                'create_starter': True, 'init_git': False,
            }
            window._prompt_new_project()
            target = Path(tmp) / 'my-project'
            assert target.is_dir()
            main_py = target / 'main.py'
            assert main_py.is_file()
            content = main_py.read_text(encoding='utf-8')
            assert '[MOD OPEN' in content and 'my-project' in content
            assert window.project_root_path == str(target)
            window.close()

    def test_kant_comment_standard_and_code_map_skills_agree_on_incoming_outgoing(self):
        skills_dir = Path(__file__).resolve().parent / '.claude' / 'skills'
        comment_standard = (skills_dir / 'kant-comment-standard' / 'SKILL.md').read_text(encoding='utf-8')
        code_map = (skills_dir / 'kant-code-map' / 'SKILL.md').read_text(encoding='utf-8')
        # neither skill may instruct writing new INCOMING/OUTGOING marker lines — the old
        # kant-code-map closing template ("up to three lines: ... INCOMING ... OUTGOING") and its
        # "seven places" rename rule are exactly the contradiction that was removed
        assert 'up to three lines' not in code_map
        assert 'seven places' not in code_map
        assert 'Never add' in comment_standard and 'INCOMING' in comment_standard
        assert 'Never add' in code_map and 'INCOMING' in code_map
# [TST CLOSED] KantSmokeTest


if __name__ == '__main__':
    unittest.main()
