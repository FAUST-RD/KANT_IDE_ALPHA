"""Qt widgets: editor, terminal, Claude pane, sections, tree, title bar, tabs."""
import json
import hashlib
import locale
import math
import os
import re
import shutil
import tempfile
import time
from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QPointF, QProcess, QRect, Qt, QSettings, QSize, Signal, QTimer
from PySide6.QtGui import (
    QBrush, QColor, QFont, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
    QPainterPathStroker, QShortcut, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGraphicsItem,
    QGraphicsPathItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
    QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSizeGrip, QSplitter, QStackedWidget, QTabWidget, QToolButton,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from kant import theme
from kant.aipermissions import PermissionBridge, write_permission_config
from kant.model import Node, parse_kant, serialize_kant, KantParseError
from kant.fileio import file_fingerprint, write_file_atomic
from kant.syntax import KEYWORDS, TOKEN_RE
from kant.xref import XrefElement


# [FN CATEGORY] KantHighlighter — QSyntaxHighlighter subclass approximating PyCharm's Darcula palette:
# comments, strings, numbers and the cross-language keyword set each get their own QTextCharFormat.
# ponytail: not a real per-language parser (same tradeoff as the JS version); block comments (/* */)
# are only recognized within a single line since true multi-line tracking needs per-block state.
# [FN] KantHighlighter — colors code tokens inside a QPlainTextEdit
# [FN OPEN] KantHighlighter
class KantHighlighter(QSyntaxHighlighter):
    TOKEN_RE = TOKEN_RE
    # a KANT marker only ever shows up as literal text inside a comment token above (markers are
    # normally parsed out into their own Node fields, never into a Run's code) — this is a fallback
    # for the rare case one leaks through unparsed; it re-highlights the `[TAG OPEN #id]` bracket
    # itself over the base comment color so #id reads as part of the marker, not as plain comment
    # text or part of Name
    MARKER_RE = re.compile(r'\[(\w+)\s+(OPEN|CLOSED)(?:\s+#(\S+))?\]')

    def __init__(self, document):
        super().__init__(document)
        self.fmt_comment = self._fmt(theme.HL_COMMENT, italic=True)
        self.fmt_string = self._fmt(theme.HL_STRING)
        self.fmt_number = self._fmt(theme.HL_NUMBER)
        self.fmt_keyword = self._fmt(theme.HL_KEYWORD)

    @staticmethod
    def _fmt(color, italic=False, bold=False):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if italic:
            fmt.setFontItalic(True)
        if bold:
            fmt.setFontWeight(QFont.Bold)
        return fmt

    def highlightBlock(self, text):
        for m in TOKEN_RE.finditer(text):
            comment, block_comment, string, number, word = m.groups()
            start, length = m.start(), m.end() - m.start()
            if comment or block_comment:
                self.setFormat(start, length, self.fmt_comment)
                marker = self.MARKER_RE.search(m.group())
                if marker:
                    color = theme.TAG_COLORS.get(marker.group(1), theme.HL_KEYWORD)
                    self.setFormat(
                        start + marker.start(), marker.end() - marker.start(), self._fmt(color, bold=True),
                    )
            elif string:
                self.setFormat(start, length, self.fmt_string)
            elif number:
                self.setFormat(start, length, self.fmt_number)
            elif word and word in KEYWORDS:
                self.setFormat(start, length, self.fmt_keyword)
# [FN CLOSED] KantHighlighter


# [FN CATEGORY] LineNumberArea — thin companion widget painted in CodeEdit's left viewport margin;
# the standard Qt pattern for QPlainTextEdit line numbers (there's no built-in gutter)
# [FN] LineNumberArea — paints line numbers for a CodeEdit
# [FN OPEN] LineNumberArea
class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)
# [FN CLOSED] LineNumberArea


# [FN CATEGORY] CodeEdit — an editable, syntax-highlighted code block that auto-grows to fit its
# content instead of scrolling internally, mirroring the HTML version's contenteditable blocks
# [FN] CodeEdit — QPlainTextEdit wired with the highlighter and auto-resize
# [FN OPEN] CodeEdit
class CodeEdit(QPlainTextEdit):
    def __init__(self, text):
        super().__init__()
        self.setPlainText(text)
        self.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFrameStyle(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:8px; padding:12px;'
        )
        self.highlighter = KantHighlighter(self.document())
        # QSyntaxHighlighter's first pass is deferred to the next paint, which fires textChanged
        # with no real edit — forcing it now (signals blocked) keeps that pass from reaching the
        # dirty-tracking connection the caller wires up right after this constructor returns.
        self.blockSignals(True)
        self.highlighter.rehighlight()
        self.blockSignals(False)

        self.line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self._update_line_number_area_width()

        self.textChanged.connect(self._auto_resize)
        self._auto_resize()

    def _auto_resize(self):
        lines = max(self.blockCount(), 1)
        padding = 24
        scrollbar = self.horizontalScrollBar().sizeHint().height()
        self.setFixedHeight(lines * self.fontMetrics().lineSpacing() + padding + scrollbar)

    # [FN CATEGORY] line_number_area_width — sizes the gutter to fit the largest line number in this
    # block, so a 3-line snippet gets a narrow gutter and a 400-line untagged file gets a wider one
    # [FN] line_number_area_width — computes the gutter width in pixels
    # [FN OPEN] line_number_area_width
    def line_number_area_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 14 + self.fontMetrics().horizontalAdvance('9') * digits
    # [FN CLOSED] line_number_area_width

    def _update_line_number_area_width(self):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def line_number_area_paint_event(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor(theme.CODE_BG))
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor(theme.DIM))
                painter.drawText(
                    0, top, self.line_number_area.width() - 8, self.fontMetrics().height(),
                    Qt.AlignRight, str(block_number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1
# [FN CLOSED] CodeEdit


# [FN CATEGORY] TerminalPane — small bottom command pane: keeps a cwd, handles cd/clear locally,
# and runs other commands through the platform shell with QProcess.
# [FN] TerminalPane — lightweight PyCharm-style terminal panel
# [FN OPEN] TerminalPane
class TerminalPane(QPlainTextEdit):
    def __init__(self, cwd):
        super().__init__()
        self.cwd = cwd
        self.prompt_start = 0
        self.process = None
        self.encoding = locale.getpreferredencoding(False)
        self.setFont(QFont('Consolas', 8))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border-top:1px solid {theme.BORDER}; '
            f'padding:12px;'
        )
        self._show_prompt()

    def set_cwd(self, cwd):
        self.cwd = cwd
        self._append(f'\n# cwd: {cwd}\n')
        self._show_prompt()

    def run_command(self, command, cwd=None):
        if self.process is not None:
            self._append('\n# terminal busy: stop the running command first\n')
            return False
        if cwd:
            self.cwd = cwd
        if len(self.toPlainText()) > self.prompt_start:
            self._append('\n')
        self._append(command + '\n')
        self._run(command)
        return True

    def keyPressEvent(self, event):
        if self.process is not None:
            if event.key() == Qt.Key_C and event.modifiers() & Qt.ControlModifier:
                self.process.kill()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            command = self.toPlainText()[self.prompt_start:].strip()
            self._append('\n')
            self._run(command)
            return
        if event.key() in (Qt.Key_Backspace, Qt.Key_Left) and self.textCursor().position() <= self.prompt_start:
            return
        if event.key() == Qt.Key_Home:
            cursor = self.textCursor()
            cursor.setPosition(self.prompt_start)
            self.setTextCursor(cursor)
            return
        if self.textCursor().selectionStart() < self.prompt_start:
            return
        super().keyPressEvent(event)

    def _prompt(self):
        return f'{self.cwd}> '

    def _append(self, text):
        self.moveCursor(QTextCursor.End)
        self.insertPlainText(text)
        self.moveCursor(QTextCursor.End)

    def _show_prompt(self):
        if self.toPlainText() and not self.toPlainText().endswith('\n'):
            self._append('\n')
        self._append(self._prompt())
        self.prompt_start = len(self.toPlainText())

    def write_info(self, text):
        if self.process is not None:
            self._append(text)
            return
        if self.toPlainText() and not self.toPlainText().endswith('\n'):
            self._append('\n')
        self._append(text)
        if not text.endswith('\n'):
            self._append('\n')
        self._show_prompt()

    def _run(self, command):
        if not command:
            self._show_prompt()
            return
        if command.lower() in ('clear', 'cls'):
            self.setPlainText('')
            self._show_prompt()
            return
        if command.lower() == 'pwd':
            self._append(self.cwd + '\n')
            self._show_prompt()
            return
        if command.lower().startswith('cd'):
            self._cd(command[2:].strip())
            self._show_prompt()
            return

        self.process = QProcess(self)
        self.process.setWorkingDirectory(self.cwd)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._error)
        self.process.finished.connect(self._finished)
        self.setReadOnly(True)
        if os.name == 'nt':
            self.process.start(os.environ.get('COMSPEC', 'cmd.exe'), ['/c', command])
        else:
            self.process.start('/bin/sh', ['-lc', command])

    def _cd(self, target):
        path = os.path.expandvars(os.path.expanduser(target or os.path.expanduser('~')))
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        path = os.path.abspath(path)
        if os.path.isdir(path):
            self.cwd = path
        else:
            self._append(f'cd: directory not found: {path}\n')

    def _read_stdout(self):
        self._append(bytes(self.process.readAllStandardOutput()).decode(self.encoding, errors='replace'))

    def _read_stderr(self):
        self._append(bytes(self.process.readAllStandardError()).decode(self.encoding, errors='replace'))

    def _finished(self, exit_code, _status):
        self.setReadOnly(False)
        if exit_code:
            self._append(f'\n[exit code {exit_code}]\n')
        self.process = None
        self._show_prompt()
# [FN CLOSED] TerminalPane


# [CST] KANT_SKILLS_DIR — skills live next to kant_editor.py itself, not inside whatever project
# folder happens to be open: claude -p runs with that project's folder as cwd (e.g. some unrelated
# "snake" project), which has no .claude/skills of its own and wouldn't discover them there
KANT_SKILLS_DIR = Path(__file__).resolve().parent.parent / '.claude' / 'skills'


# [FN CATEGORY] _load_skill_prompt — strips the YAML frontmatter off a KANT_IDE skill and returns
# the instructional body, for injection as a hidden system prompt on a claude -p call. Reading the
# skill's own file directly (rather than relying on claude to discover/trigger it by name) is what
# makes it work regardless of the target project's cwd — and regardless of whether "/name" would
# even be a recognized command there. Re-read on every call (cheap relative to spawning claude) so
# edits to the skill file take effect immediately.
# [FN] _load_skill_prompt — loads one KANT_IDE skill's body text, or None if it's missing
# [FN OPEN] _load_skill_prompt
def _load_skill_prompt(skill_name):
    try:
        text = (KANT_SKILLS_DIR / skill_name / 'SKILL.md').read_text(encoding='utf-8')
    except OSError:
        return None
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip() or None
# [FN CLOSED] _load_skill_prompt


def _write_system_prompt_file(text, directory=None):
    fd, path = tempfile.mkstemp(prefix='.kant-ai-system-', suffix='.md', dir=directory)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return path


def _agent_executable(agent):
    return 'codex' if agent == 'codex' else 'claude'


def _agent_command(agent, prompt, auto_permissions=False):
    if agent == 'codex':
        return 'codex', ['exec', *(('--full-auto',) if auto_permissions else ()), prompt]
    return 'claude', ['-p', prompt]


def _agent_label(agent):
    return 'Codex' if agent == 'codex' else 'Claude Code'


class ClaudePane(QWidget):
    finished = Signal()

    def __init__(self, cwd):
        super().__init__()
        self.cwd = cwd
        self.process = None
        self.system_prompt_file = None
        self.permission_config_file = None
        self.current_agent = 'claude'
        self.validate_after_finish = False
        self.before_run = None
        self._messages = []
        self._stream_label = None
        self._stream_text = ''
        self._session_allowed_tools = set()
        self._auto_permissions_once = False
        self._permission_cards = []
        self.permission_bridge = PermissionBridge(self)
        self.permission_bridge.requested.connect(self._permission_requested)
        self.destroyed.connect(lambda *_: self.permission_bridge.stop())
        self.log_path = os.path.join(tempfile.gettempdir(), 'kant-ai-terminal.log')
        self.encoding = locale.getpreferredencoding(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.title = QLabel('CHAT AI')
        self.title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        header.addWidget(self.title)
        header.addStretch(1)
        self.agent_select = QComboBox()
        self.agent_select.addItem('Claude Code', 'claude')
        self.agent_select.addItem('Codex', 'codex')
        header.addWidget(self.agent_select)
        self.auto_permissions = QCheckBox('Automatico')
        self.auto_permissions.setToolTip('Approva i permessi Claude; le modifiche restano soggette alla revisione finale.')
        self.auto_permissions.toggled.connect(self._automatic_permissions_changed)
        header.addWidget(self.auto_permissions)
        layout.addLayout(header)

        self.output = QScrollArea()
        self.output.setWidgetResizable(True)
        self.output.setFrameShape(QFrame.NoFrame)
        self.chat = QWidget()
        self.chat_layout = QVBoxLayout(self.chat)
        self.chat_layout.setContentsMargins(6, 10, 6, 10)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch(1)
        self.output.setWidget(self.chat)
        layout.addWidget(self.output, 1)

        self.prompt = QPlainTextEdit()
        self.prompt.setFixedHeight(90)
        self.prompt.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        self.prompt.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:8px; padding:6px;'
        )
        composer = QHBoxLayout()
        composer.addWidget(self.prompt, 1)
        self.send_btn = QPushButton('Invia')
        self.send_btn.clicked.connect(self._send)
        self.send_btn.setStyleSheet(theme.BUTTON_STYLE + f'QPushButton {{ color:{theme.WARN}; border-color:{theme.WARN}; }}')
        self.send_btn.setFixedHeight(42)
        composer.addWidget(self.send_btn, 0, Qt.AlignBottom)
        layout.addLayout(composer)

        self.agent_select.currentIndexChanged.connect(self._agent_changed)
        self._agent_changed()
        self.apply_style()

    def apply_style(self):
        self.setStyleSheet(f'background:{theme.PANEL}; border-left:1px solid {theme.BORDER};')
        self.title.setStyleSheet(f'color:{theme.WARN}; letter-spacing:2px;')
        self.output.setStyleSheet(f'QScrollArea {{ background:{theme.CODE_BG}; border:none; border-radius:12px; }}')
        self.chat.setStyleSheet(f'background:{theme.CODE_BG};')
        self.prompt.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:8px; padding:6px;'
        )
        self.agent_select.setStyleSheet(theme.BUTTON_STYLE)
        self.auto_permissions.setStyleSheet(f'color:{theme.WARN}; spacing:6px;')
        self.send_btn.setStyleSheet(theme.BUTTON_STYLE + f'QPushButton {{ color:{theme.WARN}; border-color:{theme.WARN}; }}')
        for role, frame, name, label in self._messages:
            self._style_message(role, frame, name, label)

    def _agent(self):
        return self.agent_select.currentData() or 'claude'

    def set_agent(self, agent):
        idx = self.agent_select.findData(agent)
        if idx != -1:
            self.agent_select.setCurrentIndex(idx)

    def _agent_changed(self):
        prompt = 'Prompt per codex exec...' if self._agent() == 'codex' else 'Prompt per claude -p...'
        self.prompt.setPlaceholderText(prompt)
        self.auto_permissions.setEnabled(self._agent() == 'claude')

    def _automatic_permissions_changed(self, enabled):
        if enabled:
            for request, status, buttons in list(self._permission_cards):
                if not request['event'].is_set():
                    self._decide_permission(request, status, buttons, 'auto')

    def _permission_requested(self, request):
        tool_name = request['tool_name'] or 'strumento sconosciuto'
        if self.auto_permissions.isChecked() or self._auto_permissions_once or tool_name in self._session_allowed_tools:
            self.permission_bridge.resolve(request, True)
            return
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setMaximumWidth(540)
        frame.setStyleSheet(f'QFrame {{ background:{theme.PANEL}; border:1px solid {theme.WARN}; border-radius:12px; }}')
        content = QVBoxLayout(frame)
        content.setContentsMargins(12, 9, 12, 10)
        title = QLabel(f'Permesso richiesto: {tool_name}')
        title.setStyleSheet(f'color:{theme.WARN}; font-weight:600; border:none;')
        details = QLabel(json.dumps(request['input'], ensure_ascii=False, indent=2)[:3000])
        details.setTextFormat(Qt.PlainText)
        details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        details.setWordWrap(True)
        details.setStyleSheet(f'color:{theme.TEXT}; border:none;')
        status = QLabel('Claude è in attesa della tua scelta.')
        status.setStyleSheet(f'color:{theme.DIM}; border:none;')
        content.addWidget(title)
        content.addWidget(details)
        content.addWidget(status)
        actions = QHBoxLayout()
        buttons = [QPushButton(label) for label in ('Rifiuta', 'Consenti una volta', 'Consenti per la sessione')]
        for button in buttons:
            button.setStyleSheet(theme.BUTTON_STYLE)
            actions.addWidget(button)
        buttons[0].clicked.connect(lambda: self._decide_permission(request, status, buttons, 'deny'))
        buttons[1].clicked.connect(lambda: self._decide_permission(request, status, buttons, 'once'))
        buttons[2].clicked.connect(lambda: self._decide_permission(request, status, buttons, 'session'))
        content.addLayout(actions)
        row_layout.addWidget(frame, 0, Qt.AlignLeft)
        row_layout.addStretch(1)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
        self._permission_cards.append((request, status, buttons))
        QTimer.singleShot(0, lambda: self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum()))

    def _decide_permission(self, request, status, buttons, decision):
        if request['event'].is_set():
            return
        allow = decision != 'deny'
        if decision == 'session':
            self._session_allowed_tools.add(request['tool_name'])
        self.permission_bridge.resolve(request, allow)
        labels = {
            'deny': 'Rifiutato', 'once': 'Consentito una volta',
            'session': 'Consentito per la sessione', 'auto': 'Consentito automaticamente',
        }
        status.setText(labels[decision])
        status.setStyleSheet(f"color:{theme.OK if allow else '#ef4444'}; border:none;")
        for button in buttons:
            button.setEnabled(False)

    def _cancel_pending_permissions(self):
        for request, status, buttons in list(self._permission_cards):
            if not request['event'].is_set():
                self._decide_permission(request, status, buttons, 'deny')

    def set_cwd(self, cwd):
        self.cwd = cwd
        self._append(f'Cartella di lavoro: {cwd}')

    def _write_log(self, text):
        try:
            with open(self.log_path, 'a', encoding='utf-8', newline='') as f:
                f.write(text)
        except OSError:
            pass

    def _style_message(self, role, frame, name, label):
        if role == 'user':
            bg, fg, border = theme.ACCENT, '#ffffff', theme.ACCENT
        elif role == 'assistant':
            bg, fg, border = theme.PANEL, theme.TEXT, theme.BORDER
        else:
            bg, fg, border = theme.CODE_BG, theme.DIM, theme.BORDER
        frame.setStyleSheet(f'QFrame {{ background:{bg}; border:1px solid {border}; border-radius:12px; }}')
        name.setStyleSheet(f'color:{fg}; border:none; font-weight:600; background:transparent;')
        label.setStyleSheet(f'color:{fg}; border:none; background:transparent;')

    def _add_message(self, text, role='system', name=None):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setMaximumWidth(520)
        bubble = QVBoxLayout(frame)
        bubble.setContentsMargins(12, 8, 12, 9)
        bubble.setSpacing(3)
        name_label = QLabel(name or {'user': 'Tu', 'assistant': _agent_label(self.current_agent)}.get(role, 'Sistema'))
        label = QLabel(text.strip())
        label.setTextFormat(Qt.PlainText)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setWordWrap(True)
        label.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        bubble.addWidget(name_label)
        bubble.addWidget(label)
        alignment = Qt.AlignRight if role == 'user' else Qt.AlignLeft
        if role == 'user':
            row_layout.addStretch(1)
        row_layout.addWidget(frame, 0, alignment)
        if role != 'user':
            row_layout.addStretch(1)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
        self._messages.append((role, frame, name_label, label))
        self._style_message(role, frame, name_label, label)
        QTimer.singleShot(0, lambda: self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum()))
        return label

    def _append(self, text):
        self._add_message(text, 'system')
        self._write_log(text)

    def _append_stream(self, text):
        if not text:
            return
        if self._stream_label is None:
            self._stream_label = self._add_message('', 'assistant')
            self._stream_text = ''
        self._stream_text += text
        self._stream_label.setText(self._stream_text.strip())
        self._write_log(text)
        QTimer.singleShot(0, lambda: self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum()))

    def write_info(self, text):
        self._append(text)

    def _send(self):
        if self.process is not None:
            self._cancel_pending_permissions()
            self.process.kill()
            self._append(f'\n[{_agent_label(self.current_agent)} interrotto]\n')
            return
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            return
        if self.run_prompt(prompt):
            self.prompt.clear()

    # [FN CATEGORY] run_prompt — the actual `claude -p` launch, shared by the prompt box's Invia
    # button and any caller that needs to drive this pane programmatically (e.g. MainWindow forcing
    # the kant-code-map task on project open). Always forces kant-comment-standard, plus whichever
    # extra skill bodies the caller passes — injected via --append-system-prompt so the instructions
    # apply no matter what project's folder this pane's cwd currently points at, and never depend on
    # claude discovering/recognizing a "/name" command there.
    # [FN] run_prompt — runs one prompt through `claude -p` in this pane's cwd
    # [FN OPEN] run_prompt
    def run_prompt(self, prompt, extra_skills=(), agent=None, auto_permissions_once=False):
        if agent is not None:
            self.set_agent(agent)
        agent = self._agent()
        agent_label = _agent_label(agent)
        if self.process is not None:
            self._append(f'\n# {agent_label} occupato: attendi la fine del comando corrente\n')
            return False
        self.validate_after_finish = 'kant-code-map' in extra_skills or '/kant-code-map' in prompt or 'KANT_' in prompt
        command = _agent_executable(agent)
        executable = shutil.which(command)
        self.current_agent = agent
        self._session_allowed_tools.clear()
        self._add_message(prompt, 'user')
        self._write_log(f'\n[{agent_label}]> {prompt}\n')
        if not executable:
            self._append(f'{command} non trovato nel PATH.\n')
            return False
        self._auto_permissions_once = bool(auto_permissions_once and agent == 'claude')
        system_prompt = '\n\n'.join(
            p for p in (_load_skill_prompt(name) for name in ('kant-comment-standard', *extra_skills)) if p
        )
        if agent == 'codex' and system_prompt:
            try:
                self.system_prompt_file = _write_system_prompt_file(system_prompt)
            except OSError as e:
                self._append(f'Impossibile preparare le istruzioni KANT per Codex: {e}\n')
                return False
            name = self.system_prompt_file
            prompt = (
                f'/kant-code-map\n'
                f'Questa e una richiesta esplicita di eseguire /kant-code-map sul progetto corrente. '
                f'Prima leggi e segui il file temporaneo {name} come istruzioni KANT. '
                f'Non modificare, non taggare e non includere il file temporaneo {name}; applica invece la convenzione KANT ai file sorgente '
                f'e crea o aggiorna KANT_<nome-progetto>.md. Richiesta originale: {prompt}'
            )
        _, args = _agent_command(agent, prompt, auto_permissions_once)
        if agent == 'claude':
            try:
                self.permission_config_file = write_permission_config(self.permission_bridge)
                permission_tool = 'mcp__kant_permissions__approve'
                args += [
                    '--mcp-config', self.permission_config_file,
                    '--allowedTools', permission_tool,
                    '--permission-prompt-tool', permission_tool,
                ]
                if system_prompt:
                    if len(system_prompt) > 6000:
                        self.system_prompt_file = _write_system_prompt_file(system_prompt)
                        args += ['--append-system-prompt-file', self.system_prompt_file]
                    else:
                        args += ['--append-system-prompt', system_prompt]
            except OSError as error:
                self._cleanup_temp_files()
                self._auto_permissions_once = False
                self._append(f'Impossibile preparare la sessione Claude: {error}')
                return False
        if self.before_run is not None and not self.before_run():
            self._cleanup_temp_files()
            self._auto_permissions_once = False
            self._append('[avvio annullato: snapshot non disponibile]\n')
            return False
        self.process = QProcess(self)
        self.process.setWorkingDirectory(self.cwd)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._error)
        self.process.finished.connect(self._finished)
        self.send_btn.setText('Stop')
        self.send_btn.setEnabled(True)
        self._stream_label = None
        self._stream_text = ''
        self.process.start(executable, args)
        # claude -p reads its prompt from -p, never stdin; without this it waits ~3s for piped input
        # that never comes ("no stdin data received in 3s") before proceeding — closing the write
        # channel signals EOF immediately so it starts right away
        self.process.closeWriteChannel()
        return True
    # [FN CLOSED] run_prompt

    def _read_stdout(self):
        self._append_stream(bytes(self.process.readAllStandardOutput()).decode(self.encoding, errors='replace'))

    def _read_stderr(self):
        self._append_stream(bytes(self.process.readAllStandardError()).decode(self.encoding, errors='replace'))

    def _cleanup_temp_files(self):
        for attribute in ('system_prompt_file', 'permission_config_file'):
            path = getattr(self, attribute)
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
            setattr(self, attribute, None)

    def _reset_process(self):
        self._cancel_pending_permissions()
        self._cleanup_temp_files()
        self._auto_permissions_once = False
        self._stream_label = None
        self.process = None
        self.send_btn.setText('Invia')
        self.send_btn.setEnabled(True)
        self.finished.emit()

    def _error(self, _error):
        if self.process is not None:
            self._append(f'\n[{_agent_label(self.current_agent)} errore: {self.process.errorString()}]\n')
        self._reset_process()

    def _finished(self, exit_code, _status):
        if self.process is None:
            return
        if exit_code:
            self._append(f'\n[{_agent_label(self.current_agent)} exit code {exit_code}]\n')
        else:
            self._append(f'\n[{_agent_label(self.current_agent)} completato]\n')
        self._reset_process()


def _tag_header_html(tag, name, desc, bold_name=False):
    color = theme.TAG_COLORS.get(tag, theme.TEXT)
    bg = theme.TAG_BACKGROUNDS.get(tag, '#eef2f7')
    label = desc or name
    html = (
        f'<span style="color:{color}; background-color:{bg}; font-weight:700; '
        f'padding:2px 6px; border-radius:5px">[{tag}]</span> '
    )
    html += f'<b>{html_escape(label)}</b>' if bold_name else html_escape(label)
    return html


def _build_header_row(owner, node):
    """Shared tag/name label + Meta button row for CollapsibleSection/LeafSection."""
    header = QLabel(_tag_header_html(node.tag, node.name, node.desc))
    header.setTextFormat(Qt.RichText)
    header.setFont(QFont('Consolas', theme.CODE_FONT_PT))
    header.setWordWrap(True)
    header_row = QHBoxLayout()
    header_row.setContentsMargins(0, 0, 0, 0)
    header_row.addWidget(header, 1)
    meta_btn = QPushButton('Meta')
    meta_btn.setStyleSheet(theme.BUTTON_STYLE + 'QPushButton { padding:3px 8px; }')
    meta_btn.clicked.connect(lambda _checked=False, n=node: owner.editMetadata.emit(n))
    header_row.addWidget(meta_btn)
    return header_row, header


# [FN CATEGORY] CollapsibleSection — a tagged element that has nested tagged children: a header you
# can fold, with a left accent bar echoing the HTML version's border-left indent
# [FN] CollapsibleSection — collapsible container for a non-leaf KANT node
# [FN OPEN] CollapsibleSection
class CollapsibleSection(QWidget):
    editMetadata = Signal(object)

    def __init__(self, node: Node):
        super().__init__()
        self.setObjectName('collapsible')
        self.setStyleSheet(
            f'#collapsible {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; '
            f'border-left:4px solid {theme.ACCENT}; border-radius:10px; }}'
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 10, 8)
        outer.setSpacing(6)

        self.toggle_btn = QToolButton()
        self.toggle_btn.setArrowType(Qt.DownArrow)
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setChecked(True)
        self.toggle_btn.setStyleSheet(f'border:none; color:{theme.TEXT}; background:transparent;')
        self.toggle_btn.clicked.connect(self._on_toggle)

        header_row, header = _build_header_row(self, node)
        header.setCursor(Qt.PointingHandCursor)
        header.mousePressEvent = lambda _event: self.toggle_btn.click()
        header_row.insertWidget(0, self.toggle_btn)
        outer.addLayout(header_row)

        if node.category_desc:
            cat = QLabel(html_escape(node.category_desc))
            cat.setWordWrap(True)
            cat.setStyleSheet(f'color:{theme.DIM}; margin-left: 22px;')
            cat.setFont(QFont('Consolas', theme.CODE_FONT_PT - 2))
            outer.addWidget(cat)

        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.content)

    def _on_toggle(self):
        expanded = self.toggle_btn.isChecked()
        self.content.setVisible(expanded)
        self.toggle_btn.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)

    def set_expanded(self, expanded):
        self.toggle_btn.setChecked(expanded)
        self._on_toggle()
# [FN CLOSED] CollapsibleSection


# [FN CATEGORY] LeafSection — a tagged element with no nested tagged children (e.g. a constant): a
# flat header with no fold arrow and no left indent, since there's nothing inside it to collapse.
# Rendering several of these in a row (e.g. consecutive constants) produces a flat list, not a
# staircase of indents.
# [FN] LeafSection — flat, non-collapsible header for a leaf KANT node
# [FN OPEN] LeafSection
class LeafSection(QWidget):
    editMetadata = Signal(object)

    def __init__(self, node: Node, compact=False):
        super().__init__()
        self.setObjectName('leafCompact' if compact else 'leafSection')
        self.setStyleSheet(
            f'#leafCompact {{ background:transparent; border:0; }} '
            f'#leafSection {{ background:transparent; border:0; }}'
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0 if compact else 4, 2, 0, 2)
        outer.setSpacing(1 if compact else 2)

        header_row, header = _build_header_row(self, node)
        if compact:
            header.setStyleSheet(f'padding:4px 0; border-bottom:1px solid #eef2f7;')
        outer.addLayout(header_row)

        if node.category_desc:
            cat = QLabel(html_escape(node.category_desc))
            cat.setWordWrap(True)
            cat.setStyleSheet(f'color:{theme.DIM}; margin-left: 4px;')
            cat.setFont(QFont('Consolas', theme.CODE_FONT_PT - 2))
            outer.addWidget(cat)

        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.content)
# [FN CLOSED] LeafSection


# [FN CATEGORY] ProjectTree — a QTreeWidget whose item labels (QLabel widgets with word wrap on)
# need their max width kept in sync with the available column width, since Qt doesn't do this for
# widgets embedded via setItemWidget; re-wraps every label whenever the tree itself is resized
# (e.g. by dragging the splitter), so long labels wrap instead of overflowing
# [FN] ProjectTree — project tree that re-wraps its item labels on resize
# [FN OPEN] ProjectTree
class ProjectTree(QTreeWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rewrap_labels()

    def _rewrap_labels(self):
        avail = self.viewport().width() - 12
        it = QTreeWidgetItemIterator(self)
        while it.value():
            item = it.value()
            widget = self.itemWidget(item, 0)
            if widget is not None:
                depth = 0
                p = item.parent()
                while p is not None:
                    depth += 1
                    p = p.parent()
                width = max(avail - depth * self.indentation(), 60)
                widget.setMaximumWidth(width)
                height = widget.heightForWidth(width)
                if height < 0:
                    height = widget.sizeHint().height()
                item.setSizeHint(0, QSize(width, max(height + 4, widget.sizeHint().height())))
            it += 1
# [FN CLOSED] ProjectTree


def make_star_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    center = QPointF(32, 32)
    points = []
    for i in range(10):
        radius = 25 if i % 2 == 0 else 11
        angle = math.radians(-90 + i * 36)
        points.append(QPointF(center.x() + radius * math.cos(angle), center.y() + radius * math.sin(angle)))

    painter.setBrush(QBrush(QColor(theme.HOT)))
    painter.setPen(QPen(QColor(theme.TEXT), 3))
    painter.drawPolygon(QPolygonF(points))
    painter.end()
    return QIcon(pixmap)


# [FN CATEGORY] make_save_icon — draws a small stylized floppy-disk glyph in the given color, same
# vector-drawing style as make_star_icon; used as a save-state badge next to the titlebar logo
# [FN] make_save_icon — renders a colored save-state icon
# [FN OPEN] make_save_icon
def make_save_icon(color):
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(QColor(color)))
    painter.drawRoundedRect(3, 3, 34, 34, 6, 6)
    painter.setBrush(QBrush(QColor('#ffffff')))
    painter.drawRect(9, 3, 22, 11)
    painter.setBrush(QBrush(QColor(color)))
    painter.drawRect(13, 6, 10, 5)
    painter.setBrush(QBrush(QColor('#ffffff')))
    painter.drawRoundedRect(9, 19, 22, 14, 3, 3)
    painter.end()
    return QIcon(pixmap)
# [FN CLOSED] make_save_icon


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.drag_offset = None
        self.setFixedHeight(54)
        self.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 5, 8, 5)
        layout.setSpacing(12)

        self.back_btn = QPushButton('←')
        self.back_btn.setFixedSize(32, 28)
        self.back_btn.setToolTip('Torna al menu iniziale')
        self.back_btn.clicked.connect(window._go_back_to_welcome)
        layout.addWidget(self.back_btn)

        self.save_icon = QLabel()
        self.save_icon.setFixedSize(22, 22)
        self.save_icon.setScaledContents(True)
        self.save_icon.setPixmap(make_save_icon(theme.DIM).pixmap(22, 22))
        layout.addWidget(self.save_icon)

        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(0)
        title = QLabel('KANT IDE')
        title.setFont(QFont('Consolas', 16, QFont.DemiBold))
        title.setStyleSheet(f'color:{theme.TEXT}; letter-spacing:4px;')
        subtitle = QLabel('STRUCTURAL CODE ARCHIVE')
        subtitle.setFont(QFont('Consolas', 8))
        subtitle.setStyleSheet(f'color:{theme.DIM}; letter-spacing:1px;')
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        layout.addLayout(title_box)

        self.file_menu_btn = self._menu_button('File')
        file_menu = self.file_menu_btn.menu()
        self.save_menu_action = file_menu.addAction('Salva')
        self.save_menu_action.triggered.connect(window._save_file)
        self.undo_menu_action = file_menu.addAction('Annulla file')
        self.undo_menu_action.triggered.connect(window._undo_file)
        self.redo_menu_action = file_menu.addAction('Ripeti file')
        self.redo_menu_action.triggered.connect(window._redo_file)
        self.validate_kant_menu_action = file_menu.addAction('Verifica KANT')
        self.validate_kant_menu_action.triggered.connect(window._run_kant_validation)
        self.run_menu_action = file_menu.addAction('Esegui')
        self.run_menu_action.triggered.connect(window._run_current_file)
        layout.addWidget(self.file_menu_btn)

        self.search_menu_btn = self._menu_button('Cerca')
        search_menu = self.search_menu_btn.menu()
        self.find_menu_action = search_menu.addAction('Trova nel file')
        self.find_menu_action.triggered.connect(window._show_find_bar)
        self.project_search_menu_action = search_menu.addAction('Cerca nel progetto')
        self.project_search_menu_action.triggered.connect(window._search_project)
        self.project_replace_menu_action = search_menu.addAction('Sostituisci nel progetto')
        self.project_replace_menu_action.triggered.connect(window._replace_project)
        layout.addWidget(self.search_menu_btn)

        # menu order mirrors the PyCharm-style convention: File, then editing/view-level menus
        # (Cerca ~ Edit's find/replace, Aspetto ~ View), with Git (~ VCS) last — version control is
        # its own concern, not part of the editing flow, so it sits at the end of the row
        self.appearance_menu_btn = self._menu_button('Aspetto')
        appearance_menu = self.appearance_menu_btn.menu()
        self.theme_menu_action = appearance_menu.addAction('Notte')
        self.theme_menu_action.triggered.connect(window._toggle_theme)
        layout.addWidget(self.appearance_menu_btn)

        self.lsp_menu_btn = self._menu_button('LSP')
        lsp_menu = self.lsp_menu_btn.menu()
        self.lsp_hover_menu_action = lsp_menu.addAction('Hover')
        self.lsp_hover_menu_action.triggered.connect(lambda: window._lsp_command('hover'))
        self.lsp_definition_menu_action = lsp_menu.addAction('Vai alla definizione')
        self.lsp_definition_menu_action.triggered.connect(lambda: window._lsp_command('definition'))
        self.lsp_references_menu_action = lsp_menu.addAction('References')
        self.lsp_references_menu_action.triggered.connect(lambda: window._lsp_command('references'))
        self.lsp_rename_menu_action = lsp_menu.addAction('Rename symbol')
        self.lsp_rename_menu_action.triggered.connect(lambda: window._lsp_command('rename'))
        self.lsp_format_menu_action = lsp_menu.addAction('Formatta documento')
        self.lsp_format_menu_action.triggered.connect(lambda: window._lsp_command('format'))
        layout.addWidget(self.lsp_menu_btn)

        self.git_menu_btn = self._menu_button('Git')
        git_menu = self.git_menu_btn.menu()
        self.git_refresh_menu_action = git_menu.addAction('Refresh')
        self.git_refresh_menu_action.triggered.connect(window._git_refresh)
        self.git_diff_menu_action = git_menu.addAction('Diff file')
        self.git_diff_menu_action.triggered.connect(window._git_diff_active_file)
        self.git_stage_menu_action = git_menu.addAction('Stage file')
        self.git_stage_menu_action.triggered.connect(window._git_stage_active_file)
        self.git_unstage_menu_action = git_menu.addAction('Unstage file')
        self.git_unstage_menu_action.triggered.connect(window._git_unstage_active_file)
        layout.addWidget(self.git_menu_btn)

        self.filename_label = QLabel('')
        self.filename_label.setStyleSheet(f'color:{theme.DIM};')
        layout.addWidget(self.filename_label)

        self.syntax_label = QLabel('')
        layout.addWidget(self.syntax_label)

        layout.addStretch(1)
        self.buttons = [
            self.back_btn, self.file_menu_btn, self.search_menu_btn, self.appearance_menu_btn,
            self.lsp_menu_btn, self.git_menu_btn,
        ]

        for text, callback in (('−', window.showMinimized), ('□', self._toggle_maximized), ('×', window.close)):
            btn = QPushButton(text)
            btn.setFixedSize(36, 28)
            btn.clicked.connect(callback)
            self.buttons.append(btn)
            btn.setStyleSheet(theme.BUTTON_STYLE)
            layout.addWidget(btn)
        self.apply_style()

    def _menu_button(self, text):
        btn = QToolButton()
        btn.setText(text)
        btn.setPopupMode(QToolButton.DelayedPopup)
        btn.setMenu(QMenu(btn))
        btn.clicked.connect(lambda _checked=False, b=btn: self._show_button_menu(b))
        btn.setFixedHeight(28)
        return btn

    def _show_button_menu(self, btn):
        menu = btn.menu()
        if menu is not None:
            menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def apply_style(self):
        self.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        self.theme_menu_action.setText('Giorno' if self.window.night_mode else 'Notte')
        tool_button_style = theme.BUTTON_STYLE.replace('QPushButton', 'QToolButton')
        for btn in self.buttons:
            btn.setStyleSheet(tool_button_style if isinstance(btn, QToolButton) else theme.BUTTON_STYLE)
        active = self.window.active_tab if hasattr(self.window, 'tabs') else None
        dirty = active.dirty if active else False
        self.filename_label.setStyleSheet(f'color:{theme.ACCENT if dirty else theme.DIM};')
        self.set_save_state(active is not None, dirty)

    # [FN CATEGORY] set_save_state — recolors the save-state badge: dim with no file open, accent
    # while a change is pending autosave, green once it's been written to disk
    # [FN] set_save_state — updates the titlebar save icon to reflect current save state
    # [FN OPEN] set_save_state
    def set_save_state(self, has_file, dirty):
        if not has_file:
            color, tip = theme.DIM, 'Nessun file aperto'
        elif dirty:
            color, tip = theme.ACCENT, 'Modifiche in attesa di salvataggio automatico'
        else:
            color, tip = theme.OK, 'Salvato'
        self.save_icon.setPixmap(make_save_icon(color).pixmap(22, 22))
        self.save_icon.setToolTip(tip)
    # [FN CLOSED] set_save_state

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_offset and event.buttons() & Qt.LeftButton and not self.window.isMaximized():
            self.window.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()

    def mouseReleaseEvent(self, _event):
        self.drag_offset = None

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._toggle_maximized()

    def _toggle_maximized(self):
        self.window.showNormal() if self.window.isMaximized() else self.window.showMaximized()


# [FN CATEGORY] FileTab — one open file: its own scroll area/view and its own dirty/autosave state,
# so multiple files can be open at once without their edits or save timers interfering with each
# other. Section-building (_build_node_widgets etc.) stays on MainWindow, parametrized by the tab
# it's building into, rather than duplicating that logic per tab.
# [FN] FileTab — a single open-file tab's widget and state
# [FN OPEN] FileTab
class FileTab(QWidget):
    dirtyChanged = Signal()
    saved = Signal()  # emitted after a successful write_file_atomic, for MainWindow to resync the KANT map
    saveFailed = Signal(str)
    saveConflict = Signal()

    def __init__(self, path, tree, line_ending='LF'):
        super().__init__()
        self.path = path
        self.tree = tree
        self.dirty = False
        self.filter_uid = None
        self.line_ending = line_ending
        self.section_widgets = {}  # uid -> CollapsibleSection | LeafSection
        self.collapsibles = []
        self.undo_stack = []
        self.redo_stack = []
        self._last_undo_capture = 0.0
        self.disk_fingerprint = file_fingerprint(path)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.view_container = QWidget()
        self.view_layout = QVBoxLayout(self.view_container)
        self.view_layout.setAlignment(Qt.AlignTop)
        self.view_layout.setContentsMargins(24, 18, 24, 18)
        self.view_layout.setSpacing(12)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.view_container)
        layout.addWidget(self.scroll_area)
        self.apply_style()

        # debounced autosave: fires 2s after the last edit in THIS tab, independent of other tabs
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.timeout.connect(self.autosave)

    def apply_style(self):
        self.view_container.setStyleSheet(f'background:{theme.BG};')
        self.scroll_area.setStyleSheet(f'border:none; background:{theme.BG};')

    def mark_dirty(self):
        self.dirty = True
        self.autosave_timer.start(2000)
        self.dirtyChanged.emit()

    def remember_undo_state(self, coalesce=False):
        now = time.monotonic()
        if coalesce and now - self._last_undo_capture < 0.75:
            self.redo_stack.clear()
            return
        snapshot = serialize_kant(self.tree)
        if not self.undo_stack or self.undo_stack[-1] != snapshot:
            self.undo_stack.append(snapshot)
            self.undo_stack = self.undo_stack[-30:]
        self.redo_stack.clear()
        self._last_undo_capture = now

    def _restore_snapshot(self, snapshot):
        try:
            self.tree = parse_kant(snapshot)
        except KantParseError:
            return False
        self.mark_dirty()
        return True

    def undo_file(self):
        if not self.undo_stack:
            return False
        self._last_undo_capture = 0.0
        self.redo_stack.append(serialize_kant(self.tree))
        return self._restore_snapshot(self.undo_stack.pop())

    def redo_file(self):
        if not self.redo_stack:
            return False
        self._last_undo_capture = 0.0
        self.undo_stack.append(serialize_kant(self.tree))
        return self._restore_snapshot(self.redo_stack.pop())

    def save(self, force=False):
        if not force and file_fingerprint(self.path) != self.disk_fingerprint:
            self.saveConflict.emit()
            return not self.dirty
        try:
            write_file_atomic(self.path, serialize_kant(self.tree))
        except OSError as e:
            self.dirty = True
            self.saveFailed.emit(str(e))
            self.dirtyChanged.emit()
            return False
        self.disk_fingerprint = file_fingerprint(self.path)
        self.dirty = False
        self.dirtyChanged.emit()
        self.saved.emit()
        return True

    def autosave(self):
        if self.dirty:
            return self.save()
        return True

    def flush_pending_save(self):
        if self.autosave_timer.isActive():
            self.autosave_timer.stop()
        return self.autosave()
# [FN CLOSED] FileTab


# [FN CATEGORY] XrefMapView — QGraphicsView drawing an Obsidian-style force-directed project graph:
# connected nodes attract, all nodes repel, and every node can be dragged while its curved arrows
# follow live. Selecting still dims non-neighbours; wheel zooms and background drag pans the scene.
# [FN] XrefMapView — interactive graph view of the KANT cross-reference map
# [FN OPEN] XrefMapView
def _module_flow_seeds(elements):
    """Place file clusters left-to-right along the condensed directed dependency graph."""
    files = sorted({element.file for element in elements.values()})
    adjacency = {name: set() for name in files}
    flow_weights = {name: {} for name in files}
    for element in elements.values():
        for target in element.outgoing:
            if target not in elements or elements[target].file == element.file:
                continue
            target_file = elements[target].file
            adjacency[element.file].add(target_file)
            flow_weights[element.file][target_file] = flow_weights[element.file].get(target_file, 0) + 1

    visited, finish = set(), []
    for root in files:
        if root in visited:
            continue
        stack = [(root, False)]
        while stack:
            file_name, closing = stack.pop()
            if closing:
                finish.append(file_name)
                continue
            if file_name in visited:
                continue
            visited.add(file_name)
            stack.append((file_name, True))
            stack.extend((target, False) for target in sorted(adjacency[file_name], reverse=True))
    reverse = {name: set() for name in files}
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].add(source)
    assigned, components = set(), []
    for root in reversed(finish):
        if root in assigned:
            continue
        component, stack = [], [root]
        assigned.add(root)
        while stack:
            file_name = stack.pop(); component.append(file_name)
            for source in sorted(reverse[file_name], reverse=True):
                if source not in assigned:
                    assigned.add(source); stack.append(source)
        components.append(sorted(component))
    component_of = {file_name: number for number, component in enumerate(components) for file_name in component}
    dag = {number: set() for number in range(len(components))}
    for source, targets in adjacency.items():
        for target in targets:
            if component_of[source] != component_of[target]:
                dag[component_of[source]].add(component_of[target])
    indegree = {number: 0 for number in dag}
    for targets in dag.values():
        for target in targets:
            indegree[target] += 1
    rank = {number: 0 for number in dag}
    queue = sorted((number for number, degree in indegree.items() if degree == 0), key=lambda n: components[n])
    while queue:
        source = queue.pop(0)
        for target in sorted(dag[source], key=lambda n: components[n]):
            rank[target] = max(rank[target], rank[source] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target); queue.sort(key=lambda n: components[n])
    file_rank = {file_name: rank[component_of[file_name]] for file_name in files}
    layers = {level: [name for name in files if file_rank[name] == level] for level in sorted(set(file_rank.values()))}
    order = {name: index for names in layers.values() for index, name in enumerate(names)}
    neighbour_weights = {name: dict(flow_weights[name]) for name in files}
    for source in files:
        for target, weight in flow_weights[source].items():
            neighbour_weights[target][source] = neighbour_weights[target].get(source, 0) + weight
    for sweep in range(4):
        levels = list(layers) if sweep % 2 == 0 else list(reversed(layers))
        for level in levels:
            layers[level].sort(key=lambda name: (
                sum(order[n] * weight for n, weight in neighbour_weights[name].items())
                / sum(neighbour_weights[name].values()) if neighbour_weights[name] else order[name],
                name,
            ))
            for position, name in enumerate(layers[level]):
                order[name] = position

    by_file = {name: sorted((e for e in elements.values() if e.file == name), key=lambda e: (e.order, e.key)) for name in files}
    radii = {name: max(180.0, 130.0 * math.sqrt(len(by_file[name]))) for name in files}
    layer_gap = max(760.0, max((radius * 2 for radius in radii.values()), default=0) + 420.0)
    seeds = {}
    golden_angle = math.pi * (3 - math.sqrt(5))
    for level, names in layers.items():
        total_height = sum(radii[name] * 2 + 180 for name in names) - (180 if names else 0)
        cursor = -total_height / 2
        for name in names:
            center_y = cursor + radii[name]
            center_x = level * layer_gap
            for position, element in enumerate(by_file[name]):
                if position == 0:
                    x, y = center_x, center_y
                else:
                    radius = 125.0 * math.sqrt(position)
                    angle = position * golden_angle
                    x, y = center_x + math.cos(angle) * radius, center_y + math.sin(angle) * radius
                seeds[element.key] = (x - 120, y - 15)
            cursor += radii[name] * 2 + 180
    return seeds


def _force_layout_positions(elements, fixed=None):
    """Directed module seeding plus local attraction/repulsion for readable organic spacing."""
    fixed = {key: tuple(value) for key, value in (fixed or {}).items() if key in elements}
    keys = sorted(elements)
    count = len(keys)
    if not count:
        return {}
    seeds = _module_flow_seeds(elements)
    radius = max(320.0, 140.0 * math.sqrt(count))
    positions = {
        key: list(fixed.get(key, seeds[key]))
        for key in keys
    }
    if len(fixed) != count:
        spacing = 280.0
        iterations = max(2, min(60, 1200 // count))
        temperature = radius * 0.22
        edges = {(min(source, target), max(source, target))
                 for source, element in elements.items() for target in element.outgoing if target in elements}
        # ponytail: O(n²) repulsion is simplest; use Barnes-Hut only if thousand-node maps lag.
        for step in range(iterations):
            movement = {key: [0.0, 0.0] for key in keys}
            for index, left in enumerate(keys):
                for right in keys[index + 1:]:
                    dx = positions[left][0] - positions[right][0]
                    dy = positions[left][1] - positions[right][1]
                    distance = max(1.0, math.hypot(dx, dy))
                    force = spacing * spacing / distance
                    fx, fy = dx / distance * force, dy / distance * force
                    movement[left][0] += fx; movement[left][1] += fy
                    movement[right][0] -= fx; movement[right][1] -= fy
            for left, right in edges:
                dx = positions[left][0] - positions[right][0]
                dy = positions[left][1] - positions[right][1]
                distance = max(1.0, math.hypot(dx, dy))
                force = distance * distance / spacing
                fx, fy = dx / distance * force, dy / distance * force
                movement[left][0] -= fx; movement[left][1] -= fy
                movement[right][0] += fx; movement[right][1] += fy
            for key in keys:
                if key in fixed:
                    continue
                dx, dy = movement[key]
                length = max(1.0, math.hypot(dx, dy))
                limit = temperature * (1 - step / iterations)
                positions[key][0] += dx / length * min(length, limit) - positions[key][0] * 0.008
                positions[key][1] += dy / length * min(length, limit) - positions[key][1] * 0.008
                positions[key][0] += (seeds[key][0] - positions[key][0]) * 0.12
                positions[key][1] += (seeds[key][1] - positions[key][1]) * 0.05
    if not fixed:
        min_x = min(point[0] for point in positions.values())
        min_y = min(point[1] for point in positions.values())
        for point in positions.values():
            point[0] -= min_x
            point[1] -= min_y
    return {key: (round(point[0], 2), round(point[1], 2)) for key, point in positions.items()}


class XrefNodeItem(QGraphicsPathItem):
    def __init__(self, key, path, moved):
        super().__init__(path)
        self.key, self._moved = key, moved
        self.setData(0, key)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemSendsGeometryChanges)
        self.setCursor(Qt.OpenHandCursor)
        self.setZValue(1)

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.ItemPositionHasChanged and self.scene() is not None:
            self._moved(self.key)
        return result


class XrefEdgeItem(QGraphicsPathItem):
    def __init__(self, source, target, hovered, pinned):
        super().__init__()
        self.source, self.target = source, target
        self._hovered, self._pinned = hovered, pinned
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setZValue(-1)

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(14)
        return stroker.createStroke(self.path())

    def hoverEnterEvent(self, event):
        self._hovered(self.source, self.target, event.scenePos(), True)
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        self._hovered(self.source, self.target, event.scenePos(), True)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered(self.source, self.target, event.scenePos(), False)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pinned(self.source, self.target, event.scenePos())
            event.accept()
            return
        super().mousePressEvent(event)


class XrefMapView(QGraphicsView):
    NODE_W, NODE_H = 240, 30

    nodeSelected = Signal(object)   # element key (str) or None
    nodeActivated = Signal(str)     # element key, on double click
    nodeMoved = Signal(str, float, float)
    edgeHovered = Signal(str, str, object, bool)
    edgePinned = Signal(str, str, object)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._elements = {}       # currently drawn subset: key -> element
        self._edges = []          # (source_key, target_key, path_item, arrow_item, base_color)
        self._node_items = {}     # key -> rect item
        self._label_items = {}    # key -> text item
        self._selected = None
        self._laying_out = False

    def apply_style(self):
        self.setStyleSheet(f'background:{theme.CODE_BG}; border:none;')
        self.scene().setBackgroundBrush(QColor(theme.CODE_BG))

    def wheelEvent(self, event):
        self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)

    def zoom(self, factor):
        current = self.transform().m11()
        target = max(0.15, min(3.0, current * factor))
        self.scale(target / current, target / current)

    def fit(self):
        rect = self.scene().itemsBoundingRect()
        if not rect.isNull():
            self.resetTransform()
            self.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.KeepAspectRatio)
            if self.transform().m11() > 1.25:
                self.resetTransform()
                self.scale(1.25, 1.25)
                self.centerOn(rect.center())

    # [FN CATEGORY] set_data — redraws the filtered graph using persisted coordinates as fixed
    # anchors and force-layout positions for new nodes, then creates every visible live edge.
    # [FN] set_data — renders a subset of the cross-reference graph
    # [FN OPEN] set_data
    def set_data(self, elements, saved_positions=None):
        scene = self.scene()
        scene.clear()
        self._elements = elements
        self._edges, self._node_items, self._label_items = [], {}, {}
        if self._selected not in elements:
            self._selected = None
        if not elements:
            empty = scene.addSimpleText('Nessun elemento da mostrare')
            empty.setBrush(QColor(theme.DIM))
            return

        positions = _force_layout_positions(elements, saved_positions)
        node_font = QFont('Consolas', theme.CODE_FONT_PT - 3)
        self._laying_out = True
        for el in elements.values():
            path = QPainterPath()
            path.addRoundedRect(0, 0, self.NODE_W, self.NODE_H, 7, 7)
            rect = XrefNodeItem(el.key, path, self._node_moved)
            rect.setPen(QPen(QColor(theme.TAG_COLORS.get(el.tag, theme.DIM)), 1.4))
            rect.setBrush(QBrush(QColor(theme.TAG_BACKGROUNDS.get(el.tag, theme.PANEL))))
            rect.setPos(*positions[el.key])
            collapsed = getattr(el, 'collapsed', None)
            prefix = '▸ ' if collapsed is True else ('▾ ' if collapsed is False else '')
            tooltip = f'{el.file}\n{el.category_desc or el.desc or el.name}'
            rect.setToolTip(tooltip)
            label = QGraphicsSimpleTextItem(f'{prefix}[{el.tag}] {el.desc}'[:38], rect)
            label.setBrush(QColor(theme.TEXT))
            label.setPos(8, (self.NODE_H - label.boundingRect().height()) / 2)
            label.setData(0, el.key)
            label.setToolTip(tooltip)
            label.setAcceptedMouseButtons(Qt.NoButton)
            scene.addItem(rect)
            self._node_items[el.key] = rect
            self._label_items[el.key] = label
        self._laying_out = False

        for el in elements.values():
            for target_key in el.outgoing:
                if el.key not in self._node_items or target_key not in self._node_items:
                    continue
                base_color = QColor(theme.TAG_COLORS.get(el.tag, theme.DIM))
                color = QColor(base_color)
                color.setAlpha(80)
                path_item = XrefEdgeItem(
                    el.key, target_key,
                    lambda source, target, point, entered: self.edgeHovered.emit(source, target, point, entered),
                    lambda source, target, point: self.edgePinned.emit(source, target, point),
                )
                path_item.setPen(QPen(color, 1.4))
                scene.addItem(path_item)
                arrow = scene.addPolygon(
                    QPolygonF(), QPen(Qt.NoPen), QBrush(color),
                )
                arrow.setZValue(-1)
                arrow.setAcceptedMouseButtons(Qt.NoButton)
                self._edges.append((el.key, target_key, path_item, arrow, base_color))

        self._redraw_edges()
        # An Obsidian-like canvas needs breathing room; generous stable bounds prevent scrollbar
        # recentering when a module expands or a node is dragged near the current graph edge.
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-1600, -1200, 1600, 1200))
        self._apply_highlight()
    # [FN CLOSED] set_data

    def positions(self):
        return {key: (item.pos().x(), item.pos().y()) for key, item in self._node_items.items()}

    def _anchor(self, source_key, target_key):
        source = self._node_items[source_key].sceneBoundingRect()
        target = self._node_items[target_key].sceneBoundingRect()
        start, end = source.center(), target.center()
        dx, dy = end.x() - start.x(), end.y() - start.y()
        if not dx and not dy:
            return start, end

        def boundary(rect, center, vx, vy):
            scale = min(
                (rect.width() / 2) / abs(vx) if vx else float('inf'),
                (rect.height() / 2) / abs(vy) if vy else float('inf'),
            )
            return QPointF(center.x() + vx * scale, center.y() + vy * scale)

        return boundary(source, start, dx, dy), boundary(target, end, -dx, -dy)

    def _redraw_edges(self):
        for source, target, path_item, arrow, _base in self._edges:
            if source not in self._node_items or target not in self._node_items:
                continue
            p1, p2 = self._anchor(source, target)
            dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
            length = math.hypot(dx, dy) or 1.0
            bend = min(34.0, length * 0.08) * (-1 if source < target else 1)
            control = QPointF((p1.x() + p2.x()) / 2 - dy / length * bend,
                              (p1.y() + p2.y()) / 2 + dx / length * bend)
            path = QPainterPath(p1)
            path.quadTo(control, p2)
            path_item.setPath(path)
            direction = p2 - control
            direction_length = math.hypot(direction.x(), direction.y()) or 1.0
            ux, uy = direction.x() / direction_length, direction.y() / direction_length
            back = QPointF(p2.x() - 9 * ux, p2.y() - 9 * uy)
            normal = QPointF(-uy * 4, ux * 4)
            arrow.setPolygon(QPolygonF([p2, back + normal, back - normal]))

    def _node_moved(self, key):
        self._redraw_edges()
        node_rect = self._node_items[key].sceneBoundingRect().adjusted(-80, -80, 80, 80)
        self.scene().setSceneRect(self.scene().sceneRect().united(node_rect))
        if not self._laying_out and key in self._node_items:
            point = self._node_items[key].pos()
            self.nodeMoved.emit(key, point.x(), point.y())

    def _key_at(self, viewport_pos):
        item = self.itemAt(viewport_pos)
        return item.data(0) if item is not None else None

    def mousePressEvent(self, event):
        key = self._key_at(event.position().toPoint())
        if key is not None or self._selected is not None:
            self._selected = key if key != self._selected else None
            self._apply_highlight()
            self.nodeSelected.emit(self._selected)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        key = self._key_at(event.position().toPoint())
        if key is not None:
            self.nodeActivated.emit(key)
        super().mouseDoubleClickEvent(event)

    def select(self, key):
        self._selected = key if key in self._node_items else None
        self._apply_highlight()

    def focus_on(self, key):
        if key in self._node_items:
            self.select(key)
            self.centerOn(self._node_items[key])

    # [FN CATEGORY] _apply_highlight — selection focus: with a node selected, it and its direct
    # neighbours stay full-strength while every other node, label and edge drops to low opacity, so
    # a single element's connections stand out even in a dense graph; no selection resets all to the
    # default resting look
    # [FN] _apply_highlight — dims everything except the selected node and its neighbours
    # [FN OPEN] _apply_highlight
    def _apply_highlight(self):
        key = self._selected
        neighbours = set()
        if key is not None and key in self._elements:
            el = self._elements[key]
            neighbours = {key} | set(el.incoming) | set(el.outgoing)
        for k, rect in self._node_items.items():
            active = key is None or k in neighbours
            rect.setOpacity(1.0 if active else 0.18)
            self._label_items[k].setOpacity(1.0)  # child inherits the node's dimming
            pen = rect.pen()
            pen.setWidthF(2.8 if k == key else 1.4)
            rect.setPen(pen)
        for src, dst, path_item, arrow, base in self._edges:
            touches = key is None or src == key or dst == key
            color = QColor(base)
            color.setAlpha((210 if key is not None else 80) if touches else 16)
            pen = path_item.pen()
            pen.setColor(color)
            pen.setWidthF(2.2 if (key is not None and touches) else 1.4)
            path_item.setPen(pen)
            arrow.setBrush(QBrush(color))
    # [FN CLOSED] _apply_highlight
# [FN CLOSED] XrefMapView


class EdgeFlowPopup(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('edgeFlowPopup')
        self.setMinimumWidth(340)
        self.setMaximumWidth(480)
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 11)
        layout.setSpacing(7)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.state = QLabel('Hover · clicca l’arco per fissare')
        self.incoming = QLabel()
        self.outgoing = QLabel()
        for label in (self.incoming, self.outgoing):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.title)
        layout.addWidget(self.state)
        layout.addWidget(self.incoming)
        layout.addWidget(self.outgoing)
        self.apply_style()
        self.hide()

    def apply_style(self):
        self.setStyleSheet(
            f'#edgeFlowPopup {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; border-radius:12px; }}'
        )
        self.title.setStyleSheet(f'color:{theme.TEXT}; font-weight:700; border:none;')
        self.state.setStyleSheet(f'color:{theme.DIM}; border:none;')
        self.incoming.setStyleSheet(f'color:{theme.OK}; border:none;')
        self.outgoing.setStyleSheet('color:#ef4444; border:none;')

    def set_flow(self, title, incoming, outgoing, pinned):
        self.title.setText(title)
        self.state.setText('Fissato · clicca di nuovo l’arco per chiudere' if pinned else 'Hover · clicca l’arco per fissare')
        self.incoming.setText('INCOMING\n' + ('\n'.join(f'← {item}' for item in incoming) if incoming else '← nessuno'))
        self.outgoing.setText('OUTGOING\n' + ('\n'.join(f'→ {item}' for item in outgoing) if outgoing else '→ nessuno'))
        self.adjustSize()


# [FN CATEGORY] XrefMapDialog — the cross-reference map as a frameless dialog INTERNAL to the IDE
# (a QDialog parented to the main window, not a separate OS window with its own taskbar entry): it
# floats over the editor, closes with the app, and is centered over the main window on first show.
# Wraps XrefMapView with the aids that make a large graph usable. Two defaults matter: every module
# starts COLLAPSED (only the file-level MOD/CFG node shows, with references between whole files
# aggregated onto it — double-click a module to expand it into its elements), and the TST tag starts
# OFF so tests are hidden. Also: a name/description search that expands+focuses a hidden match, tag
# toggle buttons (also the colour legend), a file selector that isolates one file plus its
# neighbours, an "isolate selected" mode, expand/collapse-all, and zoom/fit. Owns the full graph and
# recomputes the displayed (filtered + collapsed) node set on every change; the view only renders
# that. Double-clicking a leaf node emits nodeActivated so the main window can open it in the editor.
# [FN] XrefMapDialog — filterable, searchable, collapsible cross-reference map dialog
# [FN OPEN] XrefMapDialog
class XrefMapDialog(QDialog):
    TAG_ORDER = ('MOD', 'CFG', 'CLS', 'TYP', 'FN', 'CST', 'VAR', 'TST')
    HEADER_H = 38

    nodeActivated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(False)
        self._elements = {}
        self._display = {}                              # last rendered display nodes
        self._active_tags = set(self.TAG_ORDER) - {'TST'}   # tests hidden by default
        self._expanded = set()                          # files shown expanded; empty = all collapsed
        self._focus_file = None
        self._isolate = False
        self._selected = None
        self._drag_offset = None
        self._positioned = False
        self._position_key = None
        self._positions = {}
        self._position_timer = QTimer(self)
        self._position_timer.setSingleShot(True)
        self._position_timer.timeout.connect(self._save_positions)
        self._pinned_edge = None
        self._edge_hide_timer = QTimer(self)
        self._edge_hide_timer.setSingleShot(True)
        self._edge_hide_timer.timeout.connect(self._hide_edge_popup)

        self.view = XrefMapView()
        self.view.nodeSelected.connect(self._on_node_selected)
        self.view.nodeActivated.connect(self._on_node_activated)
        self.view.nodeMoved.connect(self._on_node_moved)
        self.view.edgeHovered.connect(self._on_edge_hovered)
        self.view.edgePinned.connect(self._on_edge_pinned)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        outer.addWidget(self._build_toolbar())
        outer.addWidget(self.view, 1)
        outer.addWidget(self._build_footer())
        self.edge_popup = EdgeFlowPopup(self)

    def _build_header(self):
        bar = QWidget()
        bar.setObjectName('mapHeader')
        bar.setFixedHeight(self.HEADER_H)
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 0, 8, 0)
        self.title_label = QLabel('Mappa KANT')
        self.title_label.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        self.title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)  # let drags pass through
        row.addWidget(self.title_label)
        row.addStretch(1)
        self.close_btn = QPushButton('×')
        self.close_btn.setFixedSize(30, 26)
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.close_btn)
        self._header = bar
        return bar

    def _build_toolbar(self):
        bar = QWidget()
        bar.setObjectName('mapToolbar')
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(10, 8, 10, 8)
        rows.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText('Cerca per nome o descrizione…')
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._on_search)
        self.search_box.returnPressed.connect(self._on_search_enter)
        self.search_box.setMaximumWidth(300)
        top.addWidget(self.search_box)

        top.addWidget(QLabel('File:'))
        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(200)
        self.file_combo.currentIndexChanged.connect(self._on_file_filter)
        top.addWidget(self.file_combo)

        self.expand_all_btn = QPushButton('Espandi tutti')
        self.collapse_all_btn = QPushButton('Comprimi tutti')
        self.expand_all_btn.clicked.connect(self._expand_all)
        self.collapse_all_btn.clicked.connect(self._collapse_all)
        top.addWidget(self.expand_all_btn)
        top.addWidget(self.collapse_all_btn)
        self.relayout_btn = QPushButton('Riorganizza')
        self.relayout_btn.clicked.connect(self._reorganize)
        top.addWidget(self.relayout_btn)

        self.isolate_btn = QPushButton('Isola selezionato')
        self.isolate_btn.setCheckable(True)
        self.isolate_btn.toggled.connect(self._on_isolate)
        top.addWidget(self.isolate_btn)

        top.addStretch(1)
        zoom_out = QPushButton('−')
        zoom_in = QPushButton('+')
        fit = QPushButton('Adatta')
        zoom_out.clicked.connect(lambda: self.view.zoom(1 / 1.2))
        zoom_in.clicked.connect(lambda: self.view.zoom(1.2))
        fit.clicked.connect(self.view.fit)
        for b in (zoom_out, zoom_in, fit):
            b.setFixedHeight(28)
            top.addWidget(b)
        rows.addLayout(top)

        tag_row = QHBoxLayout()
        tag_row.setSpacing(6)
        tag_row.addWidget(QLabel('Tag:'))
        self.tag_buttons = {}
        for tag in self.TAG_ORDER:
            btn = QPushButton(tag)
            btn.setCheckable(True)
            btn.setChecked(tag in self._active_tags)
            btn.setFixedHeight(26)
            btn.toggled.connect(lambda checked, t=tag: self._on_tag_toggle(t, checked))
            self.tag_buttons[tag] = btn
            tag_row.addWidget(btn)
        tag_row.addStretch(1)
        self.count_label = QLabel('')
        self.count_label.setStyleSheet(f'color:{theme.DIM};')
        tag_row.addWidget(self.count_label)
        rows.addLayout(tag_row)

        self._toolbar = bar
        return bar

    def _build_footer(self):
        bar = QWidget()
        bar.setObjectName('mapFooter')
        bar.setFixedHeight(20)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.hint_label = QLabel('  Trascina i nodi per disporli · doppio clic per espandere o aprire · Riorganizza ripristina il layout')
        self.hint_label.setStyleSheet(f'color:{theme.DIM};')
        row.addWidget(self.hint_label)
        row.addStretch(1)
        row.addWidget(QSizeGrip(bar), 0, Qt.AlignBottom | Qt.AlignRight)
        self._footer = bar
        return bar

    def apply_style(self):
        self.setStyleSheet(
            f'XrefMapDialog {{ background:{theme.BG}; border:1px solid {theme.BORDER}; }}'
        )
        self._header.setStyleSheet(
            f'#mapHeader {{ background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER}; }}'
        )
        self.title_label.setStyleSheet(f'color:{theme.TEXT}; letter-spacing:2px;')
        self.close_btn.setStyleSheet(theme.BUTTON_STYLE)
        self._toolbar.setStyleSheet(
            f'#mapToolbar {{ background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER}; }} '
            f'QLabel {{ color:{theme.TEXT}; }}'
        )
        self._footer.setStyleSheet(f'#mapFooter {{ background:{theme.PANEL}; }}')
        self.search_box.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:5px 8px;'
        )
        self.file_combo.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:4px 8px;'
        )
        for b in (self.isolate_btn, self.expand_all_btn, self.collapse_all_btn, self.relayout_btn):
            b.setStyleSheet(theme.BUTTON_STYLE)
        for tag, btn in self.tag_buttons.items():
            color = theme.TAG_COLORS.get(tag, theme.DIM)
            bg = theme.TAG_BACKGROUNDS.get(tag, theme.PANEL)
            # checked = colour-filled legend chip; unchecked = muted, so the toggle doubles as legend
            btn.setStyleSheet(
                f'QPushButton {{ border:1px solid {color}; border-radius:6px; padding:3px 10px; '
                f'font-weight:700; color:{color}; background:{theme.PANEL}; }} '
                f'QPushButton:checked {{ background:{bg}; color:{color}; }} '
                f'QPushButton:!checked {{ color:{theme.DIM}; border-color:{theme.BORDER}; }}'
            )
        self.view.apply_style()
        self.edge_popup.apply_style()

    # ---- frameless header drag ------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() <= self.HEADER_H:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        else:
            self._drag_offset = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parentWidget()
        if not self._positioned and parent is not None:
            self.resize(int(parent.width() * 0.86), int(parent.height() * 0.84))
            geo = self.frameGeometry()
            geo.moveCenter(parent.frameGeometry().center())
            self.move(geo.topLeft())
            self._positioned = True

    # [FN] set_graph — loads a fresh full graph and renders it (keeping the user's filter state)
    # [FN OPEN] set_graph
    def set_graph(self, elements, project_name='', project_path=''):
        identity = os.path.normcase(os.path.abspath(project_path or project_name or '.'))
        position_key = 'xrefPositionsV2/' + hashlib.sha1(identity.encode('utf-8')).hexdigest()
        new_project = position_key != self._position_key
        if position_key != self._position_key:
            self._position_key = position_key
            self._positions = self._load_positions()
        self._elements = elements
        self._positions = {key: value for key, value in self._positions.items() if key in elements}
        self._selected = None
        self.title_label.setText(f'Mappa KANT — {project_name}' if project_name else 'Mappa KANT')
        files = sorted({el.file for el in elements.values()})
        # a file the user had expanded that no longer exists is dropped; the rest of their state stays
        self._expanded &= set(files)
        current = self.file_combo.currentData()
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        self.file_combo.addItem('Tutti i file', None)
        for f in files:
            self.file_combo.addItem(f, f)
        idx = self.file_combo.findData(current)
        self.file_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.file_combo.blockSignals(False)
        self._focus_file = self.file_combo.currentData()
        self._refresh(fit=new_project or not self.view._node_items)
    # [FN CLOSED] set_graph

    def _load_positions(self):
        try:
            raw = QSettings('KANT', 'KANT Editor').value(self._position_key, '{}')
            data = json.loads(raw)
            return {
                key: (float(value[0]), float(value[1]))
                for key, value in data.items()
                if isinstance(key, str) and isinstance(value, list) and len(value) == 2
                and all(isinstance(number, (int, float)) and math.isfinite(number) for number in value)
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _save_positions(self):
        if self._position_key:
            QSettings('KANT', 'KANT Editor').setValue(
                self._position_key,
                json.dumps({key: [round(x, 2), round(y, 2)] for key, (x, y) in self._positions.items()}),
            )

    def _on_node_moved(self, key, x, y):
        self._positions[key] = (x, y)
        self._position_timer.start(250)

    def _reorganize(self):
        self._positions.clear()
        self._refresh(fit=True)
        self._save_positions()

    def closeEvent(self, event):
        self._position_timer.stop()
        self._save_positions()
        super().closeEvent(event)

    def _element_label(self, key):
        element = self._display.get(key) or self._elements.get(key)
        return f'[{element.tag}] {element.desc} — {element.file}' if element else key

    def _show_edge_popup(self, source, target, scene_point, pinned):
        source_element = self._display.get(source)
        target_element = self._display.get(target)
        if source_element is None or target_element is None:
            return
        incoming = [self._element_label(key) for key in target_element.incoming]
        outgoing = [self._element_label(key) for key in source_element.outgoing]
        self.edge_popup.set_flow(
            f'{self._element_label(source)}  →  {self._element_label(target)}',
            incoming, outgoing, pinned,
        )
        viewport_point = self.view.mapFromScene(scene_point)
        point = self.view.viewport().mapTo(self, viewport_point)
        x = min(max(8, point.x() + 14), max(8, self.width() - self.edge_popup.width() - 8))
        y = point.y() + 14
        if y + self.edge_popup.height() > self.height() - 8:
            y = max(8, point.y() - self.edge_popup.height() - 14)
        self.edge_popup.move(x, y)
        self.edge_popup.raise_()
        self.edge_popup.show()

    def _on_edge_hovered(self, source, target, scene_point, entered):
        if self._pinned_edge is not None:
            return
        if entered:
            self._edge_hide_timer.stop()
            self._show_edge_popup(source, target, scene_point, False)
        else:
            self._edge_hide_timer.start(120)

    def _on_edge_pinned(self, source, target, scene_point):
        edge = (source, target)
        if self._pinned_edge == edge:
            self._pinned_edge = None
            self.edge_popup.hide()
            return
        self._edge_hide_timer.stop()
        self._pinned_edge = edge
        self._show_edge_popup(source, target, scene_point, True)

    def _hide_edge_popup(self):
        if self._pinned_edge is None:
            self.edge_popup.hide()

    def _file_roots(self):
        roots = {}
        for k, e in self._elements.items():
            if e.file not in roots or e.order < self._elements[roots[e.file]].order:
                roots[e.file] = k
        return roots

    def _file_counts(self):
        counts = {}
        for e in self._elements.values():
            counts[e.file] = counts.get(e.file, 0) + 1
        return counts

    # [FN CATEGORY] _display_elements — turns the full graph into the node/edge set actually drawn,
    # applying (1) the tag filter, (2) module collapse — every element of a collapsed file is remapped
    # onto that file's root node so cross-file references aggregate into module-to-module edges —
    # then (3) the file-focus restriction and (4) isolate-selected. Synthesises fresh XrefElement
    # nodes (never mutating the originals) and tags each file-root with `.collapsed` so the view can
    # draw the ▸/▾ affordance.
    # [FN] _display_elements — computes the filtered + collapsed node set for the view
    # [FN OPEN] _display_elements
    def _display_elements(self):
        roots = self._file_roots()
        counts = self._file_counts()
        expanded = set(self._expanded)
        if self._focus_file is not None:
            expanded.add(self._focus_file)  # focusing a file always shows its contents

        def dkey(k):
            e = self._elements[k]
            return k if e.file in expanded else roots.get(e.file, k)

        base = {k: e for k, e in self._elements.items() if e.tag in self._active_tags}
        disp = {}

        def ensure(dk):
            de = self._elements.get(dk)
            if de is None or de.tag not in self._active_tags:
                return None
            if dk not in disp:
                node = XrefElement(
                    key=dk, uid=de.uid, tag=de.tag, name=de.name, desc=de.desc,
                    file=de.file, order=de.order, category_desc=de.category_desc,
                )
                collapsible = dk == roots.get(de.file) and counts.get(de.file, 0) > 1
                node.collapsed = (de.file not in expanded) if collapsible else None
                disp[dk] = node
            return disp[dk]

        for k in base:
            ensure(dkey(k))
        for k, e in base.items():
            a = dkey(k)
            if a not in disp:
                continue
            for tk in e.outgoing:
                if tk not in base:
                    continue
                b = dkey(tk)
                if b == a or b not in disp:
                    continue
                if b not in disp[a].outgoing:
                    disp[a].outgoing.append(b)
                if a not in disp[b].incoming:
                    disp[b].incoming.append(a)

        if self._focus_file is not None:
            keep = {k for k, e in disp.items() if e.file == self._focus_file}
            for k in list(keep):
                keep |= set(disp[k].incoming) | set(disp[k].outgoing)
            disp = {k: e for k, e in disp.items() if k in keep}
            self._prune_edges(disp)
        if self._isolate and self._selected in disp:
            sel = disp[self._selected]
            keep = {self._selected} | set(sel.incoming) | set(sel.outgoing)
            disp = {k: e for k, e in disp.items() if k in keep}
            self._prune_edges(disp)
        return disp
    # [FN CLOSED] _display_elements

    @staticmethod
    def _prune_edges(disp):
        for e in disp.values():
            e.outgoing = [k for k in e.outgoing if k in disp]
            e.incoming = [k for k in e.incoming if k in disp]

    def _refresh(self, fit=False):
        old_transform = self.view.transform()
        old_center = self.view.mapToScene(self.view.viewport().rect().center())
        self._display = self._display_elements()
        self.view.set_data(self._display, self._positions)
        self._positions.update(self.view.positions())
        self._position_timer.start(250)
        visible_edges = {(source, target) for source, target, *_rest in self.view._edges}
        if self._pinned_edge not in visible_edges:
            self._pinned_edge = None
            self.edge_popup.hide()
        if self._selected in self._display:
            self.view.select(self._selected)
        modules = sum(1 for e in self._display.values() if getattr(e, 'collapsed', None) is not None)
        self.count_label.setText(
            f'{len(self._display)} nodi · {len(self.view._edges)} collegamenti'
            + (f' · {modules} moduli comprimibili' if modules else '')
        )
        if fit:
            QTimer.singleShot(0, self.view.fit)
        else:
            self.view.setTransform(old_transform)
            self.view.centerOn(old_center)

    def _on_tag_toggle(self, tag, checked):
        if checked:
            self._active_tags.add(tag)
        else:
            self._active_tags.discard(tag)
        self._refresh()

    def _on_file_filter(self, _index):
        self._focus_file = self.file_combo.currentData()
        self._refresh()

    def _on_isolate(self, checked):
        self._isolate = checked
        self._refresh()
        if checked and self._selected in self._display:
            self.view.focus_on(self._selected)

    def _expand_all(self):
        self._expanded = {e.file for e in self._elements.values()}
        self._refresh()

    def _collapse_all(self):
        self._expanded = set()
        self._refresh()

    def _on_node_selected(self, key):
        self._selected = key
        if self._isolate:
            self._refresh()
            if key in self._display:
                self.view.focus_on(key)

    # [FN CATEGORY] _on_node_activated — double-click routing: on a collapsible module root it toggles
    # that file's expansion; on any other node it re-emits nodeActivated so the editor jumps to the
    # element. (Single click always just selects/highlights, via _on_node_selected.)
    # [FN] _on_node_activated — expands a module or opens an element on double-click
    # [FN OPEN] _on_node_activated
    def _on_node_activated(self, key):
        node = self._display.get(key)
        if node is not None and getattr(node, 'collapsed', None) is not None:
            self._expanded.symmetric_difference_update({node.file})
            self._refresh()
        else:
            self.nodeActivated.emit(key)
    # [FN CLOSED] _on_node_activated

    # [FN CATEGORY] _on_search — locates the first element whose name or short description matches;
    # if it sits inside a collapsed module the module is expanded first so the node exists to focus on
    # [FN] _on_search — expands if needed and focuses the first matching element
    # [FN OPEN] _on_search
    def _on_search(self, text):
        text = text.strip().lower()
        if not text:
            return
        for key, el in self._elements.items():
            if text in el.name.lower() or text in el.desc.lower():
                if el.file not in self._expanded and self._focus_file != el.file:
                    self._expanded.add(el.file)
                    self._refresh()
                self._selected = key
                self.view.focus_on(key)
                return
    # [FN CLOSED] _on_search

    def _on_search_enter(self):
        self._on_search(self.search_box.text())
# [FN CLOSED] XrefMapDialog
