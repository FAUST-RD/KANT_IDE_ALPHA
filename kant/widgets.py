"""Reusable Qt components, ordered by feature rather than application flow.

AI navigation:
- editor/terminal primitives: ``KantHighlighter`` through ``TerminalPane``;
- agent process and review UI: ``ClaudePane`` and ``_AiReviewCard``;
- KANT section/tree chrome: section widgets, ``ProjectTree``, and ``TitleBar``;
- file state: ``FileTab``;
- MAPPA: layout helpers, ``XrefMapView``, then ``XrefMapDialog``.

Application-wide coordination stays in ``mainwindow.py``. Filesystem transactions and rollback
stay in ``workspace.py``; widgets expose signals/callbacks instead of importing ``MainWindow``.
"""
import json
import hashlib
import locale
import math
import os
import re
import shutil
import sys
import tempfile
import time
from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import (
    QElapsedTimer, QFileSystemWatcher, QObject, QPointF, QProcess, QRect, QRectF, Qt, QSettings, QSize, Signal, QTimer,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
    QPainterPathStroker, QShortcut, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
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

    def mousePressEvent(self, event):
        self.editor.toggle_breakpoint_at(event.position().toPoint())
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
            f'border-radius:4px; padding:4px;'
        )
        self.highlighter = KantHighlighter(self.document())
        # QSyntaxHighlighter's first pass is deferred to the next paint, which fires textChanged
        # with no real edit — forcing it now (signals blocked) keeps that pass from reaching the
        # dirty-tracking connection the caller wires up right after this constructor returns.
        self.blockSignals(True)
        self.highlighter.rehighlight()
        self.blockSignals(False)

        self.breakpoints = set()  # block numbers (0-indexed, relative to this Run's own text)
        self.line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self._update_line_number_area_width()

        self.textChanged.connect(self._auto_resize)
        # the scrollbar-reservation part of _auto_resize's height depends on isVisible(), which
        # only becomes accurate once Qt has recomputed the scrollable range for the current width —
        # rangeChanged is exactly that recomputation, and fires independently of textChanged/resize
        # (e.g. once real layout width is known after being placed in the section's QVBoxLayout)
        self.horizontalScrollBar().rangeChanged.connect(lambda *_: QTimer.singleShot(0, self._auto_resize))
        self._auto_resize()

    def _auto_resize(self):
        lines = max(self.blockCount(), 1)
        padding = 10
        # only reserve the horizontal scrollbar's height when a line actually overflows the
        # viewport and it will really show — with ScrollBarAsNeeded this was previously reserved
        # unconditionally, adding ~14px of pure dead space under every single code block that never
        # needs one (the overwhelming majority of KANT elements' short snippets)
        scrollbar = self.horizontalScrollBar().sizeHint().height() if self.horizontalScrollBar().isVisible() else 0
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
        # the scrollbar-reservation part of _auto_resize's height depends on isVisible(), which
        # Qt only finalizes once the widget has a real width — re-check now that it does, not just
        # on textChanged (whose very first firing happens during __init__, before any real width)
        self._auto_resize()

    def line_number_area_paint_event(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor(theme.CODE_BG))
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                if block_number in self.breakpoints:
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor('#e5484d'))
                    dot = min(10, self.fontMetrics().height() - 4)
                    painter.drawEllipse(2, top + (self.fontMetrics().height() - dot) // 2, dot, dot)
                painter.setPen(QColor(theme.DIM))
                painter.drawText(
                    0, top, self.line_number_area.width() - 8, self.fontMetrics().height(),
                    Qt.AlignRight, str(block_number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1

    # [FN] toggle_breakpoint_at — flips the breakpoint on the gutter line under a click, Python only
    # [FN OPEN] toggle_breakpoint_at
    def toggle_breakpoint_at(self, pos):
        block = self.firstVisibleBlock()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        while block.isValid() and top <= pos.y():
            if block.isVisible() and bottom >= pos.y():
                number = block.blockNumber()
                self.breakpoints.symmetric_difference_update({number})
                self.line_number_area.update()
                return
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
    # [FN CLOSED] toggle_breakpoint_at
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

    # [FN CATEGORY] keyPressEvent — Enter's meaning depends on whether a process is running: with
    # none running it launches a new shell command; with one running (including an interactive
    # program like `python -m pdb`) it writes the typed line to that process's stdin instead, so
    # stepping/inspecting variables at a "(Pdb)" prompt works through this same pane. Not a real PTY
    # (no true multiplexed echo), but sufficient for pause-and-respond tools like pdb.
    # [FN] keyPressEvent — routes Enter to either a new command or the running process's stdin
    # [FN OPEN] keyPressEvent
    def keyPressEvent(self, event):
        if self.process is not None and event.key() == Qt.Key_C and event.modifiers() & Qt.ControlModifier:
            self.process.kill()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            text = self.toPlainText()[self.prompt_start:]
            self._append('\n')
            if self.process is not None:
                self.prompt_start = len(self.toPlainText())
                self.process.write((text + '\n').encode(self.encoding))
            else:
                self._run(text.strip())
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
    # [FN CLOSED] keyPressEvent

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
        if os.name == 'nt':
            self.process.start(os.environ.get('COMSPEC', 'cmd.exe'), ['/c', command])
        else:
            self.process.start('/bin/sh', ['-lc', command])
        self.prompt_start = len(self.toPlainText())

    # [FN CATEGORY] run_debug_python — launches a file under `python -m pdb`, pre-arming it with a
    # breakpoint per requested line and then continuing — pdb pauses there instead of at the first
    # line. No shell involved (direct argv), so no quoting to get right. Stepping/inspecting
    # variables afterward is just typing pdb commands (n, s, c, p x) at the resulting prompt,
    # via the same stdin-forwarding keyPressEvent uses for any running process.
    # [FN] run_debug_python — starts a pdb session for path with breakpoints pre-set
    # [FN OPEN] run_debug_python
    def run_debug_python(self, path, breakpoint_lines, cwd=None):
        if self.process is not None:
            self._append('\n# terminal busy: stop the running command first\n')
            return False
        if cwd:
            self.cwd = cwd
        if len(self.toPlainText()) > self.prompt_start:
            self._append('\n')
        self._append(f'{sys.executable} -m pdb {path}\n')
        self.process = QProcess(self)
        self.process.setWorkingDirectory(self.cwd)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._error)
        self.process.finished.connect(self._finished)
        self.process.start(sys.executable, ['-m', 'pdb', path])
        self.prompt_start = len(self.toPlainText())
        for line_no in sorted(breakpoint_lines):
            self.process.write(f'break {path}:{line_no}\n'.encode(self.encoding))
        self.process.write(b'continue\n')
        return True
    # [FN CLOSED] run_debug_python

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
        self.prompt_start = len(self.toPlainText())

    def _read_stderr(self):
        self._append(bytes(self.process.readAllStandardError()).decode(self.encoding, errors='replace'))
        self.prompt_start = len(self.toPlainText())

    def _error(self, error):
        self._append(f'\n[errore avvio processo: {error}]\n')
        self.process = None
        self._show_prompt()

    def _finished(self, exit_code, _status):
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


# [FN] _agent_command — builds the argv for launching one prompt, per-provider. Effort is a real
# parameter for both CLIs, just under different mechanisms: claude has a direct `--effort` flag
# (low/medium/high/xhigh/max, per `claude --help`); codex has no dedicated flag but honors the
# `model_reasoning_effort` config key via its generic `-c key=value` override.
def _agent_command(agent, prompt, auto_permissions=False, model=None, effort=None):
    model_args = ('--model', model) if model else ()
    if agent == 'codex':
        effort_args = ('-c', f'model_reasoning_effort="{effort}"') if effort else ()
        return 'codex', ['exec', *(('--full-auto',) if auto_permissions else ()), *model_args, *effort_args, prompt]
    effort_args = ('--effort', effort) if effort else ()
    return 'claude', [*model_args, *effort_args, '-p', prompt]


def _agent_label(agent):
    return 'Codex' if agent == 'codex' else 'Claude Code'


# [CST] _TYPING_FRAMES — cycled in the placeholder assistant bubble while a prompt is running and
# no output has streamed back yet, so the chat shows the AI is working instead of sitting blank
_TYPING_FRAMES = ('·', '· ·', '· · ·')


# [CST] MODEL_DEFAULT — sentinel meaning "no --model flag, let the CLI pick its own default"
MODEL_DEFAULT = '(predefinito)'

# [CST] CLAUDE_MODELS — current Claude model IDs accepted by `claude -p --model`, per Anthropic's
# own model catalog (Fable 5, Opus 4.8, Sonnet 5, Haiku 4.5, and the immediately preceding Opus/
# Sonnet releases). The combo stays editable so an older/newer ID can always be typed in directly.
CLAUDE_MODELS = (
    MODEL_DEFAULT, 'claude-opus-4-8', 'claude-sonnet-5', 'claude-haiku-4-5',
    'claude-fable-5', 'claude-opus-4-7', 'claude-sonnet-4-6',
)
# ponytail: unlike Claude's model list, there's no equivalent verified live catalog for Codex here
# — these are the most recent OpenAI Codex CLI model names known at the time this was written, not
# a guaranteed-current source. The combo is editable specifically so this list can go stale
# gracefully: type the real value over it instead of waiting for a code update.
CODEX_MODELS = (
    MODEL_DEFAULT, 'gpt-5.1-codex-max', 'gpt-5.1-codex', 'gpt-5-codex', 'o4-mini',
)


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
        self.context_hint = None  # callable returning a hidden scoping instruction, or None; see _send
        self._messages = []
        self._stream_label = None
        self._stream_text = ''
        self._typing_timer = QTimer(self)
        self._typing_timer.timeout.connect(self._typing_tick)
        self._typing_frame = 0
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
        self.model_select = QComboBox()
        self.model_select.setEditable(True)
        self.model_select.setToolTip("Modello per l'agente selezionato (modificabile: puoi scrivere un ID non in elenco)")
        self.model_select.addItems(CLAUDE_MODELS)
        header.addWidget(self.model_select)
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
        self.model_select.setStyleSheet(theme.BUTTON_STYLE)
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
        current = self.model_select.currentText().strip()
        is_codex = self._agent() == 'codex'
        models = CODEX_MODELS if is_codex else CLAUDE_MODELS
        other_models = CLAUDE_MODELS if is_codex else CODEX_MODELS
        self.model_select.clear()
        self.model_select.addItems(models)
        if current in models:
            self.model_select.setCurrentText(current)
        elif current and current != MODEL_DEFAULT and current not in other_models:
            self.model_select.setEditText(current)  # a genuinely custom string — keep it
        else:
            self.model_select.setCurrentIndex(0)  # a preset from the other agent doesn't carry over

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

    def _typing_tick(self):
        self._typing_frame = (self._typing_frame + 1) % len(_TYPING_FRAMES)
        if self._stream_label is not None:
            self._stream_label.setText(_TYPING_FRAMES[self._typing_frame])

    def _append_stream(self, text):
        if not text:
            return
        self._typing_timer.stop()  # real output arrived — stop the "still working" placeholder
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
        hint = self.context_hint() if self.context_hint else None
        if self.run_prompt(prompt, context_hint=hint):
            self.prompt.clear()

    # [FN CATEGORY] run_prompt — the actual `claude -p` launch, shared by the prompt box's Invia
    # button and any caller that needs to drive this pane programmatically (e.g. MainWindow forcing
    # the kant-code-map task on project open). Always forces kant-comment-standard, plus whichever
    # extra skill bodies the caller passes — injected via --append-system-prompt so the instructions
    # apply no matter what project's folder this pane's cwd currently points at, and never depend on
    # claude discovering/recognizing a "/name" command there.
    # [FN] run_prompt — runs one prompt through `claude -p` in this pane's cwd
    # [FN OPEN] run_prompt
    def run_prompt(self, prompt, extra_skills=(), agent=None, auto_permissions_once=False, effort=None, context_hint=None):
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
        # context_hint (the coding panel's current file/element, unless GLOBAL is on) rides the
        # same hidden system-prompt channel as the KANT comment standard — never part of the
        # visible prompt/chat bubble, reaches the model the identical way for both providers
        skill_prompts = [_load_skill_prompt(name) for name in ('kant-comment-standard', *extra_skills)]
        system_prompt = '\n\n'.join(p for p in (*skill_prompts, context_hint) if p)
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
        model = self.model_select.currentText().strip()
        if model == MODEL_DEFAULT:
            model = ''
        _, args = _agent_command(agent, prompt, auto_permissions_once, model or None, effort)
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
        self._stream_text = ''
        self.process.start(executable, args)
        # claude -p reads its prompt from -p, never stdin; without this it waits ~3s for piped input
        # that never comes ("no stdin data received in 3s") before proceeding — closing the write
        # channel signals EOF immediately so it starts right away
        self.process.closeWriteChannel()
        # a real response can take a few seconds; show an animated "still working" placeholder
        # right away instead of leaving the chat looking stalled until the first output arrives
        self._stream_label = self._add_message(_TYPING_FRAMES[0], 'assistant')
        self._typing_frame = 0
        self._typing_timer.start(450)
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
        self._typing_timer.stop()
        if self._stream_label is not None and not self._stream_text:
            self._stream_label.setText('')  # no output ever arrived — drop the typing placeholder
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

    # [FN CATEGORY] show_ai_review — embeds the AI change review directly in the chat transcript
    # (same insert-into-chat_layout pattern as permission request cards) instead of opening it as a
    # separate modal dialog, so it reads as part of the conversation and never blocks the rest of
    # the IDE. on_resolved(action, accepted, manual_text) fires exactly once, when the user applies
    # or cancels; the caller does the actual apply/rollback and reports the outcome as a follow-up
    # chat message.
    # [FN] show_ai_review — inserts an interactive AI review card into the chat
    # [FN OPEN] show_ai_review
    def show_ai_review(self, review, render_text, on_resolved):
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        card = _AiReviewCard(review, render_text)
        row_layout.addWidget(card, 0, Qt.AlignLeft)
        row_layout.addStretch(1)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
        QTimer.singleShot(0, lambda: self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum()))

        def fire(action):
            card.set_resolved()
            on_resolved(action, card.accepted(), card.manual_text)

        card.resolved.connect(fire)
    # [FN CLOSED] show_ai_review


# [FN CATEGORY] _AiReviewCard — the AI change review UI, embedded as a chat card rather than a
# modal dialog: a compact summary (file count, +/- totals, per-file rows) that "Controllo" expands
# into a file/hunk checklist plus a diff/editable-result tab view. Emits resolved('apply'|'cancel')
# exactly once and then locks its action buttons — the outcome message comes from the chat itself.
# [FN] _AiReviewCard — inline AI review widget (file/hunk selection, diff, editable result)
# [FN OPEN] _AiReviewCard
class _AiReviewCard(QWidget):
    DATA_ROLE = Qt.UserRole
    resolved = Signal(str)

    def __init__(self, review, render_text):
        super().__init__()
        self.review = review
        self.render_text = render_text
        self.by_path = {item['path']: item for item in review}
        self.file_items = {}
        self.manual_text = {}
        self._current_path = None
        self._loading_editor = False
        self._action_buttons = []
        self.setMaximumWidth(720)
        self.setStyleSheet(
            f'#aiReviewBubble, #aiReviewDetails {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; border-radius:12px; }} '
            f'QLabel {{ color:{theme.TEXT}; }} QTreeWidget, QPlainTextEdit {{ background:{theme.CODE_BG}; '
            f'color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:8px; }} '
            f'QTabWidget::pane {{ border:1px solid {theme.BORDER}; border-radius:8px; background:{theme.CODE_BG}; }}'
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        root.addWidget(self._build_summary())
        self.details = self._build_details()
        self.details.hide()
        root.addWidget(self.details)

    def _button(self, text, slot, action=False):
        button = QPushButton(text)
        button.setStyleSheet(theme.BUTTON_STYLE)
        button.clicked.connect(slot)
        if action:
            self._action_buttons.append(button)
        return button

    def _build_summary(self):
        panel = QWidget()
        panel.setObjectName('aiReviewBubble')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 9)
        layout.setSpacing(3)
        # same name-label treatment as a normal assistant chat bubble (_style_message), so this
        # reads as another message in the conversation rather than a separate card bolted on
        sender = QLabel('Assistente AI')
        sender.setStyleSheet(f'color:{theme.TEXT}; font-weight:600; background:transparent;')
        layout.addWidget(sender)
        header = QHBoxLayout()
        totals = QVBoxLayout()
        title = QLabel(f"{len(self.review)} file {'modificato' if len(self.review) == 1 else 'modificati'}")
        title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        totals.addWidget(title)
        added = sum(item['additions'] for item in self.review)
        deleted = sum(item['deletions'] for item in self.review)
        counts = QLabel(f'<span style="color:{theme.OK}">+{added}</span>  <span style="color:#ef4444">-{deleted}</span>')
        totals.addWidget(counts)
        header.addLayout(totals)
        header.addStretch(1)
        header.addWidget(self._button('Annulla ↶', lambda: self.resolved.emit('cancel'), action=True))
        header.addWidget(self._button('Controllo', self._show_details))
        layout.addLayout(header)

        self.summary_rows = []
        for index, item in enumerate(self.review):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            path_button = QPushButton(item['path'])
            path_button.setStyleSheet(
                f'QPushButton {{ text-align:left; padding:5px 0; border:none; color:{theme.TEXT}; background:transparent; }} '
                f'QPushButton:hover {{ color:{theme.ACCENT}; }}'
            )
            path_button.clicked.connect(lambda _checked=False, path=item['path']: self._show_details(path))
            row_layout.addWidget(path_button, 1)
            added_label = QLabel(f"+{item['additions']}")
            added_label.setStyleSheet(f'color:{theme.OK};')
            deleted_label = QLabel(f"-{item['deletions']}")
            deleted_label.setStyleSheet('color:#ef4444;')
            row_layout.addWidget(added_label)
            row_layout.addWidget(deleted_label)
            row.setVisible(index < 3)
            layout.addWidget(row)
            self.summary_rows.append(row)
        hidden = max(0, len(self.review) - 3)
        self.more_btn = self._button(f'Mostra altri {hidden} file ⌄', self._toggle_summary)
        self.more_btn.setVisible(bool(hidden))
        layout.addWidget(self.more_btn)
        return panel

    def _toggle_summary(self):
        expanded = not self.summary_rows[-1].isVisible()
        for row in self.summary_rows[3:]:
            row.setVisible(expanded)
        self.more_btn.setText('Mostra meno ⌃' if expanded else f'Mostra altri {len(self.review) - 3} file ⌄')

    def _build_details(self):
        panel = QWidget()
        panel.setObjectName('aiReviewDetails')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 10)
        splitter = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['File e blocchi da mantenere'])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setMinimumWidth(220)
        for item in self.review:
            file_item = QTreeWidgetItem([item['path']])
            file_item.setData(0, self.DATA_ROLE, (item['path'], None))
            file_item.setFlags(file_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            file_item.setCheckState(0, Qt.Checked)
            for index, hunk in enumerate(item['hunks']):
                child = QTreeWidgetItem([hunk['title']])
                child.setData(0, self.DATA_ROLE, (item['path'], index))
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Checked)
                file_item.addChild(child)
            self.tree.addTopLevelItem(file_item)
            self.file_items[item['path']] = file_item
        self.tree.expandAll()
        self.tree.currentItemChanged.connect(self._show_tree_item)
        self.tree.itemChanged.connect(self._selection_changed)
        splitter.addWidget(self.tree)

        self.tabs = QTabWidget()
        self.diff_view = QPlainTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.result_editor = QPlainTextEdit()
        self.result_editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.result_editor.textChanged.connect(self._editor_changed)
        self.tabs.addTab(self.diff_view, 'Differenze')
        self.tabs.addTab(self.result_editor, 'Risultato modificabile')
        splitter.addWidget(self.tabs)
        splitter.setSizes([240, 560])
        layout.addWidget(splitter, 1)

        note = QLabel('I blocchi selezionati saranno mantenuti. Cambiare una selezione ripristina il risultato del file e scarta i ritocchi manuali su quel file.')
        note.setWordWrap(True)
        note.setStyleSheet(f'color:{theme.DIM};')
        layout.addWidget(note)
        actions = QHBoxLayout()
        actions.addWidget(self._button('Rifiuta selezionati', lambda: self._set_selected(False)))
        actions.addWidget(self._button('Accetta selezionati', lambda: self._set_selected(True)))
        actions.addStretch(1)
        actions.addWidget(self._button('Annulla tutto', lambda: self.resolved.emit('cancel'), action=True))
        actions.addWidget(self._button('Accetta tutto', self._accept_all, action=True))
        actions.addWidget(self._button('Applica scelte', lambda: self.resolved.emit('apply'), action=True))
        layout.addLayout(actions)
        return panel

    def _show_details(self, path=None):
        self.details.setVisible(True)
        self.details.setMinimumHeight(420)
        if path:
            self.tree.setCurrentItem(self.file_items[path])
        elif self.tree.currentItem() is None and self.review:
            self.tree.setCurrentItem(self.file_items[self.review[0]['path']])

    def _show_tree_item(self, current, _previous=None):
        if current is None:
            return
        path, hunk_index = current.data(0, self.DATA_ROLE)
        item = self.by_path[path]
        self._current_path = path
        diff = item['hunks'][hunk_index]['diff'] if hunk_index is not None else '\n'.join(h['diff'] for h in item['hunks'])
        self.diff_view.setPlainText(diff)
        self._loading_editor = True
        if item['binary']:
            self.result_editor.setPlainText('[file binario: disponibile solo accettazione o rifiuto]')
            self.result_editor.setReadOnly(True)
        else:
            self.result_editor.setReadOnly(False)
            selected = self.accepted_hunks(path)
            self.result_editor.setPlainText(self.manual_text.get(path, self.render_text(item, selected)))
            self.result_editor.document().setModified(path in self.manual_text)
        self._loading_editor = False

    def _selection_changed(self, tree_item, _column):
        data = tree_item.data(0, self.DATA_ROLE)
        if not data:
            return
        path, _index = data
        self.manual_text.pop(path, None)
        if path == self._current_path:
            self._show_tree_item(tree_item)

    def _editor_changed(self):
        if not self._loading_editor and self._current_path and not self.by_path[self._current_path]['binary']:
            self.manual_text[self._current_path] = self.result_editor.toPlainText()

    def _set_selected(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for selected in self.tree.selectedItems():
            if selected.childCount():
                for index in range(selected.childCount()):
                    selected.child(index).setCheckState(0, state)
            else:
                selected.setCheckState(0, state)

    def accepted_hunks(self, path):
        parent = self.file_items[path]
        return {index for index in range(parent.childCount()) if parent.child(index).checkState(0) == Qt.Checked}

    def accepted(self):
        return {path: self.accepted_hunks(path) for path in self.file_items}

    def _accept_all(self):
        for parent in self.file_items.values():
            for index in range(parent.childCount()):
                parent.child(index).setCheckState(0, Qt.Checked)
        self.resolved.emit('apply')

    def set_resolved(self):
        for button in self._action_buttons:
            button.setEnabled(False)
# [FN CLOSED] _AiReviewCard


def _tag_header_html(tag, name, desc, bold_name=False):
    color = theme.TAG_COLORS.get(tag, theme.TEXT)
    bg = theme.TAG_BACKGROUNDS.get(tag, '#eef2f7')
    label = desc or name
    html = (
        f'<span style="color:{color}; background-color:{bg}; font-weight:700; '
        f'padding:0px 4px; border-radius:4px">[{tag}]</span> '
    )
    html += f'<b>{html_escape(label)}</b>' if bold_name else html_escape(label)
    return html


# [FN CATEGORY] _build_header_row — shared tag/name label + a "more" (⋮) button row for
# CollapsibleSection/LeafSection. The button opens the metadata editor directly (no intermediate
# menu) since it currently has exactly one action.
# [FN] _build_header_row — builds the tag/name label plus a ⋮ metadata button for a KANT element
# [FN OPEN] _build_header_row
def _build_header_row(owner, node):
    # the name/short-description is the element's headline — bigger than the extended
    # [TAG CATEGORY] description below it (CODE_FONT_PT - 2), not just the same size in a bolder weight
    header = QLabel(_tag_header_html(node.tag, node.name, node.desc))
    header.setTextFormat(Qt.RichText)
    header.setFont(QFont('Consolas', theme.CODE_FONT_PT + 1))
    header.setWordWrap(True)
    header_row = QHBoxLayout()
    header_row.setContentsMargins(0, 0, 0, 0)
    header_row.setSpacing(1)
    header_row.addWidget(header, 1)
    meta_btn = QToolButton()
    meta_btn.setText('⋮')
    meta_btn.setToolTip('Modifica metadati KANT')
    meta_btn.setCursor(Qt.PointingHandCursor)
    # fixed size so the bigger glyph (bigger per user request) doesn't stretch the whole header
    # row's height via QToolButton's own sizeHint — that would put empty space back around every
    # element's header, the opposite of the density this row is built for
    meta_btn.setFixedSize(24, 22)
    meta_btn.setStyleSheet(
        f'QToolButton {{ border:none; background:transparent; color:{theme.DIM}; '
        f'font-weight:900; font-size:{theme.CODE_FONT_PT + 8}pt; padding:0; }} '
        f'QToolButton:hover {{ color:{theme.ACCENT}; }}'
    )
    meta_btn.clicked.connect(lambda _checked=False, n=node: owner.editMetadata.emit(n))
    header_row.addWidget(meta_btn)
    return header_row, header
# [FN CLOSED] _build_header_row


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
        outer.setContentsMargins(3, 1, 2, 1)
        outer.setSpacing(1)

        self.toggle_btn = QToolButton()
        self.toggle_btn.setArrowType(Qt.DownArrow)
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setChecked(True)
        self.toggle_btn.setStyleSheet(f'border:none; color:{theme.TEXT}; background:transparent; padding:0; margin:0;')
        self.toggle_btn.setMaximumWidth(16)
        self.toggle_btn.clicked.connect(self._on_toggle)

        header_row, header = _build_header_row(self, node)
        header.setCursor(Qt.PointingHandCursor)
        header.mousePressEvent = lambda _event: self.toggle_btn.click()
        header_row.insertWidget(0, self.toggle_btn)
        outer.addLayout(header_row)

        if node.category_desc:
            cat = QLabel(html_escape(node.category_desc))
            cat.setWordWrap(True)
            cat.setStyleSheet(f'color:{theme.DIM}; margin-left: 12px;')
            cat.setFont(QFont('Consolas', theme.CODE_FONT_PT - 2))
            outer.addWidget(cat)

        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(1)  # platform-default spacing here was the real source of gaps
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
        outer.setContentsMargins(0 if compact else 3, 1, 0, 1)
        outer.setSpacing(1)

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
        self.content_layout.setSpacing(1)  # platform-default spacing here was the real source of gaps
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
        # a bare QWidget doesn't paint stylesheet background/border at all unless this is set —
        # the border-bottom below (meant to separate the title bar from the panels underneath)
        # was silently never rendering
        self.setAttribute(Qt.WA_StyledBackground, True)
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
        self.view_layout.setContentsMargins(6, 4, 6, 4)
        self.view_layout.setSpacing(1)
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
def _module_flow_seeds(elements, rtl=False):
    """Place file clusters left-to-right (or right-to-left when rtl) along the condensed directed
    dependency graph. Named `rtl`, not `reverse`, to avoid shadowing the unrelated reverse-adjacency
    dict already local to this function."""
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
    for level, names in layers.items():
        total_height = sum(radii[name] * 2 + 180 for name in names) - (180 if names else 0)
        cursor = -total_height / 2
        for name in names:
            center_y = cursor + radii[name]
            center_x = level * layer_gap * (-1 if rtl else 1)
            _seed_file_cluster(by_file[name], center_x, center_y, radii[name], seeds, rtl)
            cursor += radii[name] * 2 + 180
    return seeds


def _seed_file_cluster(elements_in_file, center_x, center_y, radius, seeds, rtl=False):
    """Seed one file's elements around (center_x, center_y), ranking left-to-right (or
    right-to-left when rtl) by intra-file call depth (source before target) instead of an
    arbitrary spiral, so the starting position already reads as logical flow before the force
    simulation even runs — the simulation's own seed-pull is strong enough that a direction-blind
    seed never recovers. Shared by the normal multi-file map and MAPPA's drill-down (there, the
    "file" is really the drilled element's own children, ranked by their mutual references)."""
    keys = {e.key for e in elements_in_file}
    local_targets = {e.key: [t for t in e.outgoing if t in keys and t != e.key] for e in elements_in_file}
    rank = {e.key: 0 for e in elements_in_file}
    for _ in range(len(elements_in_file)):
        changed = False
        for e in elements_in_file:
            for target in local_targets[e.key]:
                if rank[target] < rank[e.key] + 1:
                    rank[target] = rank[e.key] + 1
                    changed = True
        if not changed:
            break
    groups = {}
    for element in elements_in_file:
        groups.setdefault(rank[element.key], []).append(element)
    max_rank = max(groups) if groups else 0
    span = radius * 1.6
    step = span / (max_rank + 1) if max_rank else 0.0
    for level, group in groups.items():
        offset = level * step
        x = center_x + span / 2 - offset if rtl else center_x - span / 2 + offset
        group_height = len(group) * 60.0
        y = center_y - group_height / 2 + 30.0
        for element in group:
            seeds[element.key] = (x, y)
            y += 60.0


# [FN] _element_degree — connectivity count driving node size, heatmap intensity, and layout weight.
# Node-tag visibility already narrows incoming/outgoing before this ever runs (_display_elements
# only aggregates references between currently-active tags) — passing `elements`/`active_edge_tags`
# additionally narrows by the connections filter, the same rule that decides whether an edge is
# drawn, so size/heatmap/geography react to that filter too, not just to node visibility.
def _element_degree(el, elements=None, active_edge_tags=None):
    incoming = getattr(el, 'incoming_detail', None) or el.incoming
    outgoing = getattr(el, 'outgoing_detail', None) or el.outgoing
    if active_edge_tags is None or elements is None:
        return len(incoming) + len(outgoing)
    if el.tag not in active_edge_tags:
        return 0
    incoming = [k for k in incoming if k in elements and elements[k].tag in active_edge_tags]
    outgoing = [k for k in outgoing if k in elements and elements[k].tag in active_edge_tags]
    return len(incoming) + len(outgoing)


# [CST] MIN_NODE_W/MAX_NODE_W/MIN_NODE_H/MAX_NODE_H — node box size bounds; an element scales
# between these by how much code traffic it carries (see _element_size). Shared by the force
# layout (so real box footprints never overlap) and XrefMapView's rendering (so what's drawn
# matches what was laid out).
MIN_NODE_W, MAX_NODE_W = 170, 320
MIN_NODE_H, MAX_NODE_H = 22, 42
ANCHOR_SIZE = 16  # small unmarked "common origin" circle footprint — fixed, not traffic-scaled


def _element_size(el, max_degree, elements=None, active_edge_tags=None):
    """Box (width, height) for one element, scaled by its share of the busiest node's traffic.
    A common-origin anchor is always the small fixed circle size regardless of how many
    siblings connect to it — it's a marker, not a traffic hub."""
    if getattr(el, 'is_anchor', False):
        return ANCHOR_SIZE, ANCHOR_SIZE
    degree = _element_degree(el, elements, active_edge_tags)
    t = (degree / max_degree) if max_degree else 0.0
    width = MIN_NODE_W + (MAX_NODE_W - MIN_NODE_W) * t
    height = MIN_NODE_H + (MAX_NODE_H - MIN_NODE_H) * t
    return width, height


def _force_layout_positions(elements, fixed=None, seed=None, active_edge_tags=None, use_parent_attraction=True, rtl=False):
    """Directed module seeding plus local attraction/repulsion for readable organic spacing.
    `fixed` positions are pinned and never moved by the simulation (persisted/dragged nodes).
    `seed` positions are only a starting point the simulation is free to adjust — used to warm-start
    a re-layout after a filter change so nodes drift to their new spot instead of jumping.
    Nodes with no incoming/outgoing edges at all get a much stronger pull toward their module's
    seed position, since nothing else anchors them and unopposed repulsion would otherwise push
    them far from their cluster. Repulsion strength is derived per-pair from each node's own
    (traffic-scaled) box size, so two large hubs never overlap just because a single global spacing
    constant happened to fit smaller nodes. Individual connections — not just module rank — read
    left-to-right as the logical flow of the code, because `_module_flow_seeds` ranks each file's
    own elements by intra-file call depth before the simulation starts (the seed-pull below is
    strong enough that a direction-blind starting position never recovers into one afterward).

    `fixed`/`seed` are given, and the result is returned, as top-left corners (what callers store
    and what QGraphicsItem.setPos expects) — but since nodes now vary in size, the physics itself
    runs in center coordinates internally: comparing top-left corners directly would make two
    differently-sized nodes look closer or farther apart than their real edges actually are,
    letting boxes overlap. `sizes`/half-size are computed once up front and used to convert at the
    boundary in both directions."""
    max_degree = max((_element_degree(e, elements, active_edge_tags) for e in elements.values()), default=0)
    sizes = {key: _element_size(element, max_degree, elements, active_edge_tags) for key, element in elements.items()}

    def to_center(pos, key):
        w, h = sizes[key]
        return (pos[0] + w / 2, pos[1] + h / 2)

    fixed = {key: to_center(tuple(value), key) for key, value in (fixed or {}).items() if key in elements}
    seed = {key: to_center(tuple(value), key) for key, value in (seed or {}).items() if key in elements}
    keys = sorted(elements)
    count = len(keys)
    if not count:
        return {}
    seeds = _module_flow_seeds(elements, rtl)  # already centers
    radius = max(320.0, 140.0 * math.sqrt(count))
    positions = {
        key: list(fixed[key] if key in fixed else seed.get(key, seeds[key]))
        for key in keys
    }
    if len(fixed) != count:
        # nodes are wide-and-short (roughly 170-320 x 22-42, scaled per-node by traffic): an
        # isotropic (circular) force wastes vertical room since a ~30px-tall box needs far less
        # vertical clearance than its width does. Shrinking dy's contribution to the
        # repulsion/attraction distance (not the real dy itself) lets rows pack tighter vertically
        # while keeping horizontal spacing intact.
        y_squash = 0.55
        # different modules repel harder than elements of the same module, so a cluster's own
        # boundary reads clearly against its neighbours instead of blending into them.
        cross_module_boost = 1.35
        # common-origin (same immediate KANT parent, or the same anchor for orphaned siblings —
        # see _add_common_origin_anchors) pulls elements toward each other too, but deliberately
        # weaker than the reference-edge attraction (1.0x) below: this is a secondary clustering
        # cue, not a replacement for the hierarchy-rank seeding or call-direction flow that already
        # dominate node placement.
        origin_weight = 0.35
        iterations = max(2, min(60, 1200 // count))
        temperature = radius * 0.22
        cooling = 0.94  # geometric decay — smoother settling than a linear ramp to zero
        # each node's own half-diagonal, in the same squashed metric used for `distance` below, so
        # a required minimum separation compares like with like
        half_diag = {key: math.hypot(w / 2, (h / 2) * y_squash) for key, (w, h) in sizes.items()}
        edges = {(min(source, target), max(source, target))
                 for source, element in elements.items() for target in element.outgoing if target in elements}
        connected = {key for pair in edges for key in pair}
        # a node with no edges at all has nothing pulling it back in — only repulsion pushes it —
        # so it drifts far from its cluster over enough iterations. Anchoring it to its seed much
        # more strongly keeps it near its module instead of stranded at the edge of the canvas.
        isolated = elements.keys() - connected
        parent_pairs = [
            (key, element.parent) for key, element in elements.items()
            if use_parent_attraction and element.parent and element.parent in elements
        ]
        # ponytail: O(n²) repulsion is simplest; use Barnes-Hut only if thousand-node maps lag.
        for step in range(iterations):
            movement = {key: [0.0, 0.0] for key in keys}
            for index, left in enumerate(keys):
                for right in keys[index + 1:]:
                    dx = positions[left][0] - positions[right][0]
                    dy = positions[left][1] - positions[right][1]
                    distance = max(1.0, math.hypot(dx, dy * y_squash))
                    boost = cross_module_boost if elements[left].file != elements[right].file else 1.0
                    min_sep = half_diag[left] + half_diag[right] + 40.0  # real footprints + a visible gap
                    force = min_sep * min_sep * boost / distance
                    fx, fy = dx / distance * force, dy / distance * force
                    movement[left][0] += fx; movement[left][1] += fy
                    movement[right][0] -= fx; movement[right][1] -= fy
            for left, right in edges:
                dx = positions[left][0] - positions[right][0]
                dy = positions[left][1] - positions[right][1]
                distance = max(1.0, math.hypot(dx, dy * y_squash))
                force = distance * distance / 280.0
                fx, fy = dx / distance * force, dy / distance * force
                movement[left][0] -= fx; movement[left][1] -= fy
                movement[right][0] += fx; movement[right][1] += fy
            for child, parent in parent_pairs:
                dx = positions[child][0] - positions[parent][0]
                dy = positions[child][1] - positions[parent][1]
                distance = max(1.0, math.hypot(dx, dy * y_squash))
                force = distance * distance / 280.0 * origin_weight
                fx, fy = dx / distance * force, dy / distance * force
                movement[child][0] -= fx; movement[child][1] -= fy
                movement[parent][0] += fx; movement[parent][1] += fy
            limit = temperature * (cooling ** step)
            for key in keys:
                if key in fixed:
                    continue
                dx, dy = movement[key]
                length = max(1.0, math.hypot(dx, dy))
                positions[key][0] += dx / length * min(length, limit) - positions[key][0] * 0.008
                positions[key][1] += dy / length * min(length, limit) - positions[key][1] * 0.008
                pull_x, pull_y = (0.35, 0.22) if key in isolated else (0.12, 0.05)
                positions[key][0] += (seeds[key][0] - positions[key][0]) * pull_x
                positions[key][1] += (seeds[key][1] - positions[key][1]) * pull_y
        # the repulsion above is a soft, elliptical approximation (via y_squash) — it makes overlap
        # rare but can't guarantee zero in every configuration, particularly when two boxes end up
        # nearly aligned on one axis. Directly resolve any real axis-aligned overlap left over,
        # using each box's true (unsquashed) width/height — a hard constraint layered on top of the
        # soft aesthetic preference above, so two boxes never actually overlap on screen.
        margin = 10.0
        for _ in range(12):
            moved = False
            for index, left in enumerate(keys):
                for right in keys[index + 1:]:
                    move_left, move_right = left not in fixed, right not in fixed
                    if not move_left and not move_right:
                        continue
                    lw, lh = sizes[left]
                    rw, rh = sizes[right]
                    dx = positions[left][0] - positions[right][0]
                    dy = positions[left][1] - positions[right][1]
                    overlap_x = (lw + rw) / 2 + margin - abs(dx)
                    overlap_y = (lh + rh) / 2 + margin - abs(dy)
                    if overlap_x <= 0 or overlap_y <= 0:
                        continue
                    moved = True
                    share = 0.5 if (move_left and move_right) else 1.0
                    if overlap_x < overlap_y:
                        sign = 1.0 if dx >= 0 else -1.0
                        push = overlap_x * share
                        if move_left:
                            positions[left][0] += sign * push
                        if move_right:
                            positions[right][0] -= sign * push
                    else:
                        sign = 1.0 if dy >= 0 else -1.0
                        push = overlap_y * share
                        if move_left:
                            positions[left][1] += sign * push
                        if move_right:
                            positions[right][1] -= sign * push
            if not moved:
                break
    if not fixed and not seed:
        min_x = min(point[0] for point in positions.values())
        min_y = min(point[1] for point in positions.values())
        for point in positions.values():
            point[0] -= min_x
            point[1] -= min_y
    # convert back from the internal center representation to the top-left corners callers expect
    return {
        key: (round(point[0] - sizes[key][0] / 2, 2), round(point[1] - sizes[key][1] / 2, 2))
        for key, point in positions.items()
    }


# [CST] _HEAT_STOPS — classic heat-spectrum gradient (cold blue to hot red), interpolated by
# _heat_color; used by MAPPA's heatmap color mode instead of per-tag coloring
_HEAT_STOPS = (
    (0.0, (59, 130, 246)), (0.25, (34, 211, 238)), (0.5, (34, 197, 94)),
    (0.75, (250, 204, 21)), (1.0, (239, 68, 68)),
)


def _heat_color(t):
    t = max(0.0, min(1.0, t))
    for (t0, c0), (t1, c1) in zip(_HEAT_STOPS, _HEAT_STOPS[1:]):
        if t0 <= t <= t1:
            span = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return QColor(*(round(a + (b - a) * span) for a, b in zip(c0, c1)))
    return QColor(*_HEAT_STOPS[-1][1])


class XrefNodeItem(QGraphicsPathItem):
    def __init__(self, key, path, moved, hovered=None):
        super().__init__(path)
        self.key, self._moved, self._hovered = key, moved, hovered
        self.setData(0, key)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemSendsGeometryChanges)
        self.setCursor(Qt.OpenHandCursor)
        self.setZValue(1)
        self.setAcceptHoverEvents(True)

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.ItemPositionHasChanged and self.scene() is not None:
            self._moved(self.key)
        return result

    def hoverEnterEvent(self, event):
        if self._hovered:
            self._hovered(self.key, event.scenePos(), True)
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        if self._hovered:
            self._hovered(self.key, event.scenePos(), True)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        if self._hovered:
            self._hovered(self.key, event.scenePos(), False)
        super().hoverLeaveEvent(event)


# [FN CATEGORY] PinBadgeItem — the pin-sequence marker shown above a pinned node: a filled circle
# with the sequence number painted directly (not a font glyph/emoji), so its click/hover area is
# the whole circle — predictable and generously sized — instead of whatever a "📌" glyph's actual
# ink happens to occupy in a given font.
# [FN] PinBadgeItem — drawn (not emoji) pin-sequence badge
# [FN OPEN] PinBadgeItem
class PinBadgeItem(QGraphicsItem):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._number = 1
        self._diameter = 40.0
        self.setAcceptedMouseButtons(Qt.NoButton)  # decorative only; the node itself owns pin-toggling

    def set_appearance(self, number, diameter):
        self._number = number
        self._diameter = diameter
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        return QRectF(0, 0, self._diameter, self._diameter)

    def paint(self, painter, _option, _widget=None):
        d = self._diameter
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.HOT))
        painter.drawEllipse(QRectF(0, 0, d, d))
        painter.setPen(QColor('#ffffff'))
        painter.setFont(QFont('Consolas', max(7, round(d * 0.42)), QFont.Bold))
        painter.drawText(QRectF(0, 0, d, d), Qt.AlignCenter, str(self._number))
# [FN CLOSED] PinBadgeItem


# [FN CATEGORY] EyeBadgeItem — the clickable drill-down icon next to a single pinned node's
# sequence badge, shown only when that element has a complex enough internal structure to be
# worth drilling into. A drawn eye (outline + pupil), not an emoji glyph — same reasoning as
# PinBadgeItem: the whole circle is the hit area, not an unpredictable font glyph's ink extent.
# Swallows its own press so clicking it doesn't also re-toggle the node's pin underneath.
# [FN] EyeBadgeItem — clickable drill-down icon, child of one node's XrefNodeItem
# [FN OPEN] EyeBadgeItem
class EyeBadgeItem(QGraphicsItem):
    def __init__(self, key, clicked, parent=None):
        super().__init__(parent)
        self.key = key
        self._clicked = clicked
        self._diameter = 40.0
        self.setCursor(Qt.PointingHandCursor)
        self.setZValue(4)
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def set_diameter(self, diameter):
        self._diameter = diameter
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        return QRectF(0, 0, self._diameter, self._diameter)

    def paint(self, painter, _option, _widget=None):
        d = self._diameter
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.HOT))
        painter.drawEllipse(QRectF(0, 0, d, d))
        painter.setPen(QPen(QColor('#ffffff'), max(1.2, d * 0.08)))
        painter.setBrush(Qt.NoBrush)
        eye_w, eye_h = d * 0.62, d * 0.34
        painter.drawEllipse(QRectF(d / 2 - eye_w / 2, d / 2 - eye_h / 2, eye_w, eye_h))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#ffffff'))
        pupil = d * 0.18
        painter.drawEllipse(QRectF(d / 2 - pupil / 2, d / 2 - pupil / 2, pupil, pupil))

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.contains(event.pos()):
            self._clicked(self.key)
        event.accept()
# [FN CLOSED] EyeBadgeItem


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
    FONT_PT_SPAN = 6  # label font grows this many points from quietest to busiest node

    nodesPinned = Signal(list)     # ordered list of pinned element keys (sequence order = pin order)
    nodeActivated = Signal(str)     # element key, on double click
    nodeMoved = Signal(str, float, float)
    nodeHovered = Signal(str, object, bool)
    edgeHovered = Signal(str, str, object, bool)
    edgePinned = Signal(str, str, object)
    drillRequested = Signal(str)   # element key, from clicking its eye icon

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # panning is drag-based; scrollbars are clutter
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._elements = {}       # currently drawn subset: key -> element
        self._edges = []          # (source_key, target_key, path_item, arrow_item, base_color)
        self._containment_edges = []  # (parent_key, child_key, path_item) — neutral, arrowless hierarchy lines
        self._active_edge_tags = None  # None = no filter yet (show all); set by the dialog's edge-tag row
        self._show_containment = True  # whether the neutral "belonging" connections draw/pull at all
        self._rtl = False  # False = code flow reads left-to-right (default); True = right-to-left
        self._node_items = {}     # key -> rect item
        self._label_items = {}    # key -> text item
        self._pin_badges = {}     # key -> small "📌N" label shown above a pinned node
        self._eye_badges = {}     # key -> clickable "👁" drill-down icon, next to the pin badge
        self._pinned = []         # ordered list of pinned element keys; order = pin sequence number
        self._drillable_key = None  # key eligible for the eye icon right now (dialog decides eligibility)
        self._laying_out = False
        self._heatmap = False    # False: color by tag; True: color by connectivity heat
        self._max_degree = 0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._animate_tick)
        self._anim_start = {}
        self._anim_target = {}
        self._anim_clock = QElapsedTimer()
        self._anim_duration = 280

    def apply_style(self):
        self.setStyleSheet(f'background:{theme.CODE_BG}; border:none;')
        self.scene().setBackgroundBrush(QColor(theme.CODE_BG))

    def wheelEvent(self, event):
        self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)

    def zoom(self, factor):
        current = self.transform().m11()
        target = max(0.15, min(3.0, current * factor))
        self.scale(target / current, target / current)
        self._update_node_scale()

    # [FN CATEGORY] _update_node_scale — bubble size follows the camera but damped: on-screen size
    # scales with zoom**0.5 instead of zoom**1, so zooming way out to see the whole graph doesn't
    # shrink labels to illegible specks, and zooming in close doesn't blow a single node up past
    # the screen. Scaled around each box's own center (not its top-left corner) so nodes don't
    # visually drift as the factor changes; edges re-anchor to the live (scaled) box afterward.
    # [FN] _update_node_scale — applies zoom-damped LOD scaling to every displayed node
    # [FN OPEN] _update_node_scale
    def _update_node_scale(self):
        zoom = self.transform().m11()
        if zoom <= 0:
            return
        factor = max(0.55, min(2.2, zoom ** -0.5))
        for item in self._node_items.values():
            item.setScale(factor)
        self._redraw_edges()
    # [FN CLOSED] _update_node_scale

    def fit(self):
        rect = self.scene().itemsBoundingRect()
        if not rect.isNull():
            self.resetTransform()
            self.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.KeepAspectRatio)
            if self.transform().m11() > 1.25:
                self.resetTransform()
                self.scale(1.25, 1.25)
                self.centerOn(rect.center())
            self._update_node_scale()

    # [FN CATEGORY] set_data — redraws the filtered graph using persisted coordinates as fixed
    # anchors and force-layout positions for new nodes, then creates every visible live edge.
    # [FN] set_data — renders a subset of the cross-reference graph
    # [FN OPEN] set_data
    def set_data(self, elements, saved_positions=None):
        scene = self.scene()
        scene.clear()
        self._elements = elements
        self._edges, self._node_items, self._label_items, self._pin_badges = [], {}, {}, {}
        self._eye_badges = {}
        self._containment_edges = []
        self._pinned = [key for key in self._pinned if key in elements]
        if not elements:
            empty = scene.addSimpleText('Nessun elemento da mostrare')
            empty.setBrush(QColor(theme.DIM))
            return

        self._max_degree = max((self._node_degree(e) for e in elements.values()), default=0)
        positions = _force_layout_positions(
            elements, saved_positions,
            active_edge_tags=self._active_edge_tags, use_parent_attraction=self._show_containment,
            rtl=self._rtl,
        )
        self._laying_out = True
        for el in elements.values():
            width, height, font_pt = self._node_dims(el)
            is_anchor = getattr(el, 'is_anchor', False)
            path = QPainterPath()
            if is_anchor:
                path.addEllipse(0, 0, width, height)
            else:
                path.addRoundedRect(0, 0, width, height, 7, 7)
            rect = XrefNodeItem(
                el.key, path, self._node_moved,
                lambda key, point, entered: self.nodeHovered.emit(key, point, entered),
            )
            rect.setTransformOriginPoint(width / 2, height / 2)  # scale from center, not corner
            pen_color, fill_color = self._node_colors(el)
            rect.setPen(QPen(pen_color, 1.4))
            rect.setBrush(QBrush(fill_color))
            rect.setPos(*positions[el.key])
            if is_anchor:
                # unmarked: no text, no tag/category tooltip — just a reminder these elements
                # share an origin that isn't drawn right now
                rect.setToolTip('Origine comune (elemento radice non visualizzato)')
                label = QGraphicsSimpleTextItem('', rect)
            else:
                collapsed = getattr(el, 'collapsed', None)
                prefix = '▸ ' if collapsed is True else ('▾ ' if collapsed is False else '')
                tooltip = f'{el.file}\n{el.category_desc or el.desc or el.name}'
                rect.setToolTip(tooltip)
                label_font = QFont('Consolas', round(font_pt))
                available_width = max(10, int(width) - 16)  # 8px margin each side
                elided = QFontMetrics(label_font).elidedText(f'{prefix}[{el.tag}] {el.desc}', Qt.ElideRight, available_width)
                label = QGraphicsSimpleTextItem(elided, rect)
                label.setFont(label_font)
                label.setBrush(QColor(theme.TEXT))
                label.setPos(8, (height - label.boundingRect().height()) / 2)
                label.setData(0, el.key)
                label.setToolTip(tooltip)
                label.setAcceptedMouseButtons(Qt.NoButton)
            pin_badge = PinBadgeItem(rect)
            pin_badge.set_appearance(1, 40.0)
            pin_badge.setPos(2, -47)  # a real gap above the node, not touching its top edge
            pin_badge.setZValue(3)
            pin_badge.setVisible(False)
            eye_badge = EyeBadgeItem(el.key, lambda key: self.drillRequested.emit(key), rect)
            eye_badge.set_diameter(40.0)
            eye_badge.setPos(2, -47)
            eye_badge.setZValue(4)
            eye_badge.setVisible(False)
            scene.addItem(rect)
            self._node_items[el.key] = rect
            self._label_items[el.key] = label
            self._pin_badges[el.key] = pin_badge
            self._eye_badges[el.key] = eye_badge
        self._laying_out = False

        for el in elements.values():
            for target_key in el.outgoing:
                if el.key not in self._node_items or target_key not in self._node_items:
                    continue
                target_el = elements.get(target_key)
                if self._active_edge_tags is not None and (
                    el.tag not in self._active_edge_tags
                    or (target_el is not None and target_el.tag not in self._active_edge_tags)
                ):
                    continue
                # a direct parent-child pair already has the neutral containment line — a colored
                # reference arrow between the very same two nodes (e.g. a class referencing its
                # own method) would just be a redundant second connection for one relationship
                if target_el is not None and (target_el.parent == el.key or el.parent == target_key):
                    continue
                base_color = self._node_colors(el)[0]
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

        # hierarchy lines: a plain, arrowless black edge from each element to its immediate
        # container (module/class, or a common-origin anchor standing in for one that isn't
        # currently displayed) — independent of the colored directed reference arrows above, this
        # is what makes containment/belonging readable on the map, not just call flow. Toggled off
        # entirely via the "Appartenenza" connections selector.
        if self._show_containment:
            for el in elements.values():
                if el.parent and el.parent in self._node_items and el.key in self._node_items:
                    line_item = QGraphicsPathItem()
                    line_item.setZValue(-2)
                    line_item.setAcceptedMouseButtons(Qt.NoButton)
                    line_item.setPen(QPen(QColor(0, 0, 0, 90), 1.2))
                    scene.addItem(line_item)
                    self._containment_edges.append((el.parent, el.key, line_item))

        self._update_node_scale()  # newly built nodes must reflect the current zoom's LOD factor
        # An Obsidian-like canvas needs breathing room; generous stable bounds prevent scrollbar
        # recentering when a module expands or a node is dragged near the current graph edge.
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-1600, -1200, 1600, 1200))
        self._apply_highlight()
    # [FN CLOSED] set_data

    def _node_degree(self, el):
        return _element_degree(el, self._elements, self._active_edge_tags)

    def _traffic_t(self, el):
        """0..1: how connected this element is relative to the busiest one on screen right now."""
        return (self._node_degree(el) / self._max_degree) if self._max_degree else 0.0

    # [FN CATEGORY] _node_colors — a node's pen/fill colors: normally per-tag (TAG_COLORS/
    # TAG_BACKGROUNDS), but in heatmap mode a classic cold-to-hot gradient keyed on how connected
    # the node is (incoming + outgoing reference count), normalized against the busiest node
    # currently on screen — so the map reads as "where the activity is" instead of "what kind of
    # thing this is".
    # [FN] _node_colors — returns (pen_color, fill_color) for one displayed node
    # [FN OPEN] _node_colors
    def _node_colors(self, el):
        if getattr(el, 'is_anchor', False):
            black = QColor(0, 0, 0)
            return black, black  # unmarked: same black whether heatmap or tag coloring is active
        if self._heatmap:
            color = _heat_color(self._traffic_t(el))
            fill = QColor(color)
            fill.setAlpha(130)
            return color, fill
        return QColor(theme.TAG_COLORS.get(el.tag, theme.DIM)), QColor(theme.TAG_BACKGROUNDS.get(el.tag, theme.PANEL))
    # [FN CLOSED] _node_colors

    # [FN CATEGORY] _node_dims — box width/height and label font size scale with how much code
    # traffic this element carries (incoming + outgoing reference count, same metric as heatmap
    # coloring): a busy hub gets a bigger, more legible box, a quiet leaf stays compact — always
    # on, independent of whether heatmap coloring itself is enabled.
    # [FN] _node_dims — returns (width, height, font_pt) for one displayed node
    # [FN OPEN] _node_dims
    def _node_dims(self, el):
        width, height = _element_size(el, self._max_degree, self._elements, self._active_edge_tags)
        font_pt = (theme.CODE_FONT_PT - 3) + self.FONT_PT_SPAN * self._traffic_t(el)
        return width, height, font_pt
    # [FN CLOSED] _node_dims

    def recolor(self, heatmap):
        """Re-applies tag or heatmap coloring to the already-drawn graph, without touching
        positions, the camera, or the selected/pinned state — a pure appearance toggle."""
        self._heatmap = heatmap
        self._max_degree = max((self._node_degree(e) for e in self._elements.values()), default=0) if heatmap else 0
        for key, rect in self._node_items.items():
            pen_color, fill_color = self._node_colors(self._elements[key])
            rect.setPen(QPen(pen_color, 1.4))
            rect.setBrush(QBrush(fill_color))
        for index, (source, target, path_item, arrow, _old_base) in enumerate(self._edges):
            base_color = self._node_colors(self._elements[source])[0]
            self._edges[index] = (source, target, path_item, arrow, base_color)
        self._apply_highlight()

    def positions(self):
        return {key: (item.pos().x(), item.pos().y()) for key, item in self._node_items.items()}

    # [FN CATEGORY] relayout_to — recomputes positions for a changed element set (tag/file filter,
    # module expand/collapse, isolate) using current on-screen coordinates as a warm start rather
    # than hard pins, then renders at the final layout and animates surviving nodes back from their
    # old spot to it — so filtering reads as the graph settling into place, not a jump cut.
    # [FN] relayout_to — recomputes and smoothly animates node positions for a new element set
    # [FN OPEN] relayout_to
    def relayout_to(self, elements, seed_positions):
        old_on_screen = {key: item.pos() for key, item in self._node_items.items()}
        new_positions = _force_layout_positions(
            elements, seed=seed_positions,
            active_edge_tags=self._active_edge_tags, use_parent_attraction=self._show_containment, rtl=self._rtl,
        )
        self.set_data(elements, new_positions)
        self._laying_out = True
        for key, pos in old_on_screen.items():
            item = self._node_items.get(key)
            if item is not None:
                item.setPos(pos)
        self._laying_out = False
        self._redraw_edges()
        self._animate_positions(old_on_screen, new_positions)
        return new_positions
    # [FN CLOSED] relayout_to

    def _animate_positions(self, start, target, duration_ms=280):
        self._anim_timer.stop()
        self._anim_start = {key: QPointF(pos) for key, pos in start.items() if key in target and key in self._node_items}
        self._anim_target = {key: QPointF(*target[key]) for key in self._anim_start}
        if not self._anim_start:
            return
        self._anim_duration = duration_ms
        self._laying_out = True
        self._anim_clock.start()
        self._anim_timer.start(16)

    def _animate_tick(self):
        elapsed = self._anim_clock.elapsed()
        t = min(1.0, elapsed / self._anim_duration)
        eased = 1 - (1 - t) ** 3  # ease-out cubic
        for key, start_pos in self._anim_start.items():
            item = self._node_items.get(key)
            if item is not None:
                item.setPos(start_pos + (self._anim_target[key] - start_pos) * eased)
        # one redraw for the whole batch, not one per node moved this frame (see _node_moved)
        self._redraw_edges()
        if t >= 1.0:
            self._anim_timer.stop()
            self._laying_out = False

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
        for parent_key, child_key, line_item in self._containment_edges:
            if parent_key not in self._node_items or child_key not in self._node_items:
                continue
            p1, p2 = self._anchor(parent_key, child_key)
            path = QPainterPath(p1)
            path.lineTo(p2)  # straight, not bent — reads as structural, not a call
            line_item.setPath(path)

    def _node_moved(self, key):
        if self._laying_out:
            # a programmatic relayout (or the initial set_data build) moves every node in one
            # batch; redrawing all edges once per node here is O(nodes) redundant redraws per
            # frame — the animation tick (or set_data itself) does the single redraw that's
            # actually needed once the whole batch has moved
            return
        self._redraw_edges()
        node_rect = self._node_items[key].sceneBoundingRect().adjusted(-80, -80, 80, 80)
        self.scene().setSceneRect(self.scene().sceneRect().united(node_rect))
        point = self._node_items[key].pos()
        self.nodeMoved.emit(key, point.x(), point.y())

    def _key_at(self, viewport_pos):
        item = self.itemAt(viewport_pos)
        return item.data(0) if item is not None else None

    def mousePressEvent(self, event):
        # the eye badge sits on top of its node (higher Z) with no item-data of its own, so
        # itemAt() here would return it and _key_at would read back None — read as "clicked empty
        # canvas" and clear every pin before Qt ever delivers the press to the eye's own handler.
        # Recognize it explicitly and let the normal event dispatch below reach it untouched.
        if isinstance(self.itemAt(event.position().toPoint()), EyeBadgeItem):
            super().mousePressEvent(event)
            return
        key = self._key_at(event.position().toPoint())
        if key is not None:
            pinned = list(self._pinned)
            if key in pinned:
                pinned.remove(key)  # clicking an already-pinned node's own pin ends its highlight
            else:
                pinned.append(key)  # new pin goes to the end of the sequence
            self.set_pinned(pinned)
            self.nodesPinned.emit(list(self._pinned))
        elif self._pinned:
            self.set_pinned([])  # clicking empty canvas clears every pin at once
            self.nodesPinned.emit([])
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        key = self._key_at(event.position().toPoint())
        if key is not None:
            self.nodeActivated.emit(key)
        super().mouseDoubleClickEvent(event)

    def select(self, key):
        """Pin exactly this node, clearing any others — for single-target jumps (isolate, search)."""
        self.set_pinned([key] if key in self._node_items else [])

    def set_pinned(self, keys):
        """Replace the whole pin set (e.g. restoring it after a relayout) and redraw highlighting."""
        self._pinned = [key for key in keys if key in self._node_items]
        self._apply_highlight()

    def set_active_edge_tags(self, tags):
        """Which tags' reference edges to draw at all — independent of node visibility (self can
        show a function node while hiding every edge that touches a function, or vice versa).
        Takes effect on the next set_data(), not retroactively."""
        self._active_edge_tags = set(tags)

    def set_show_containment(self, show):
        """Whether the neutral "belonging" (containment/common-origin) connections draw and pull
        at all. Takes effect on the next set_data(), not retroactively."""
        self._show_containment = show

    def set_direction(self, rtl):
        """False (default) = code flow reads left-to-right; True = right-to-left. Takes effect
        on the next set_data()/relayout_to(), not retroactively."""
        self._rtl = rtl

    def set_drillable(self, key):
        """Which key (if any) is eligible for the eye icon right now — the dialog decides
        eligibility (needs the full, undisplayed graph to check for internal cross-references)
        and pushes it down here; this just redraws to reflect it."""
        self._drillable_key = key
        self._apply_highlight()

    def focus_on(self, key):
        if key in self._node_items:
            self.select(key)
            self.centerOn(self._node_items[key])

    # [FN CATEGORY] _apply_highlight — multi-pin focus: every pinned node and its direct neighbours
    # stay full-strength while everything else drops to low opacity, so several elements' connections
    # can be compared at once in a dense graph; each pinned node also gets a small numbered "📌N"
    # badge above it showing its position in the pin sequence. When exactly one node is pinned and
    # it's the current drillable key (dialog-decided eligibility), that badge and a "👁" drill-down
    # icon next to it both enlarge — the eye opens the internal-only view for that element. No pins
    # resets all to the default resting look.
    # [FN] _apply_highlight — dims everything except pinned nodes and their neighbours
    # [FN OPEN] _apply_highlight
    def _apply_highlight(self):
        show_all = not self._pinned
        neighbours = set()
        for key in self._pinned:
            if key in self._elements:
                el = self._elements[key]
                neighbours |= {key} | set(el.incoming) | set(el.outgoing)
        for k, rect in self._node_items.items():
            active = show_all or k in neighbours
            rect.setOpacity(1.0 if active else 0.18)
            self._label_items[k].setOpacity(1.0)  # child inherits the node's dimming
            badge = self._pin_badges.get(k)
            eye = self._eye_badges.get(k)
            if badge is not None:
                if k in self._pinned:
                    drillable = len(self._pinned) == 1 and k == self._drillable_key
                    diameter = 60.0 if drillable else 40.0
                    badge.set_appearance(self._pinned.index(k) + 1, diameter)
                    badge.setPos(2, -diameter - 7)
                    badge.setVisible(True)
                    badge.setOpacity(1.0)
                    if eye is not None:
                        eye.setVisible(drillable)
                        if drillable:
                            eye.set_diameter(diameter)
                            eye.setOpacity(1.0)
                            eye.setPos(badge.pos().x() + diameter + 8, badge.pos().y())
                else:
                    badge.setVisible(False)
                    if eye is not None:
                        eye.setVisible(False)
            pen = rect.pen()
            pen.setWidthF(2.8 if k in self._pinned else 1.4)
            rect.setPen(pen)
        for src, dst, path_item, arrow, base in self._edges:
            touches = show_all or src in self._pinned or dst in self._pinned
            color = QColor(base)
            color.setAlpha((210 if not show_all else 80) if touches else 16)
            pen = path_item.pen()
            pen.setColor(color)
            pen.setWidthF(2.2 if (not show_all and touches) else 1.4)
            path_item.setPen(pen)
            arrow.setBrush(QBrush(color))
        for parent_key, child_key, line_item in self._containment_edges:
            touches = show_all or parent_key in self._pinned or child_key in self._pinned
            pen = line_item.pen()
            pen.setColor(QColor(0, 0, 0, (140 if not show_all else 90) if touches else 18))
            line_item.setPen(pen)
    # [FN CLOSED] _apply_highlight
# [FN CLOSED] XrefMapView


class EdgeFlowPopup(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('edgeFlowPopup')
        self._base_min_width, self._base_max_width = 340, 480
        self.setMinimumWidth(self._base_min_width)
        self.setMaximumWidth(self._base_max_width)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._pinned = False
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

    # [FN CATEGORY] apply_style — re-applies theme colors, plus a stronger accent border and bolder
    # state line while pinned, so a fixed popup clearly reads as "stuck" rather than a passing hover.
    # Pinned uses HOT (not ACCENT) since ACCENT is the same blue as every button/hover/selection in
    # the app — a pinned popup needs a border that doesn't blend into that everywhere-blue.
    # [FN] apply_style — re-applies theme (and pinned-state) styling to the popup
    # [FN OPEN] apply_style
    def apply_style(self):
        border_color = theme.HOT if self._pinned else theme.BORDER
        border_width = 2 if self._pinned else 1
        self.setStyleSheet(
            f'#edgeFlowPopup {{ background:{theme.PANEL}; border:{border_width}px solid {border_color}; border-radius:12px; }}'
        )
        self.title.setStyleSheet(f'color:{theme.TEXT}; font-weight:700; border:none;')
        self.state.setStyleSheet(
            f'color:{theme.HOT if self._pinned else theme.DIM}; font-weight:{700 if self._pinned else 400}; border:none;'
        )
        self.incoming.setStyleSheet(f'color:{theme.OK}; border:none;')
        self.outgoing.setStyleSheet('color:#ef4444; border:none;')
    # [FN CLOSED] apply_style

    def set_flow(self, title, incoming, outgoing, pinned):
        self._pinned = pinned
        self.title.setText(title)
        self.state.setText('📌 Fissato · clicca di nuovo l’arco per chiudere' if pinned else 'Hover · clicca l’arco per fissare')
        self.incoming.setText('INCOMING\n' + ('\n'.join(f'← {item}' for item in incoming) if incoming else '← nessuno'))
        self.outgoing.setText('OUTGOING\n' + ('\n'.join(f'→ {item}' for item in outgoing) if outgoing else '→ nessuno'))
        self.apply_style()
        self.adjustSize()

    # [FN CATEGORY] set_zoom_scale — the popup is a fixed screen-space overlay, not a scene item, so
    # it never zooms with the canvas on its own; this makes its size track the camera's current
    # zoom level (clamped to a sane range) instead of staying identical regardless of how far in or
    # out the map is.
    # [FN] set_zoom_scale — scales the popup's width bounds and font sizes with the given factor
    # [FN OPEN] set_zoom_scale
    def set_zoom_scale(self, scale):
        scale = max(0.7, min(1.6, scale))
        self.setMinimumWidth(round(self._base_min_width * scale))
        self.setMaximumWidth(round(self._base_max_width * scale))
        base_pt = theme.CODE_FONT_PT
        title_font = self.title.font()
        title_font.setPointSizeF(max(7.0, (base_pt + 1) * scale))
        self.title.setFont(title_font)
        for label in (self.state, self.incoming, self.outgoing):
            font = label.font()
            font.setPointSizeF(max(6.5, base_pt * scale))
            label.setFont(font)
        self.adjustSize()
    # [FN CLOSED] set_zoom_scale


# [FN CATEGORY] XrefMapDialog — the cross-reference map as a frameless dialog INTERNAL to the IDE
# (a QDialog parented to the main window, not a separate OS window with its own taskbar entry): it
# floats over the editor, closes with the app, and is centered over the main window on first show.
# Wraps XrefMapView with the aids that make a large graph usable. Two defaults matter: every module
# starts fully EXPANDED on each open (set_graph() re-expands every file, regardless of how a
# previous session left it) — double-click a module to collapse it back to its file-level MOD/CFG
# node, aggregating references between whole files onto it — and the TST tag starts OFF so tests
# are hidden. Also: a name/description search that expands+focuses a hidden match, tag
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
    resized = Signal()  # lets the main window keep the close-tab (reparented onto this dialog) positioned

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(False)
        self._elements = {}
        self._display = {}                              # last rendered display nodes
        self._active_tags = set(self.TAG_ORDER) - {'TST'}   # tests hidden by default
        self._active_edge_tags = set(self.TAG_ORDER)     # which tags' reference edges are drawn at all
        self._show_containment = True    # the neutral "belonging" connections, toggled alongside the tags
        self._rtl = False    # False = code flow reads left-to-right (default); True = right-to-left
        self._expanded = set()                          # files shown expanded; set_graph() fills this with every file on each open
        self._focus_file = None
        self._isolate = False
        self._selected = None       # most-recently-pinned key — read by selected_key() for map-close navigation
        self._pinned_nodes = []     # full ordered multi-pin set (mirrors XrefMapView._pinned)
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
        self._hover_show_timer = QTimer(self)     # delay between hover and the popup appearing
        self._hover_show_timer.setSingleShot(True)
        self._hover_show_timer.timeout.connect(self._show_pending_hover)
        self._pending_hover = None                # ('edge', (source, target, point)) or ('node', (key, point))
        self._drill_key = None    # element whose internal-only view we're showing, or None for the full map
        self._project_name = ''

        self.view = XrefMapView()
        self.view.set_active_edge_tags(self._active_edge_tags)
        self.view.set_show_containment(self._show_containment)
        self.view.set_direction(self._rtl)
        self.view.nodesPinned.connect(self._on_nodes_pinned)
        self.view.nodeActivated.connect(self._on_node_activated)
        self.view.nodeMoved.connect(self._on_node_moved)
        self.view.nodeHovered.connect(self._on_node_hovered)
        self.view.edgeHovered.connect(self._on_edge_hovered)
        self.view.edgePinned.connect(self._on_edge_pinned)
        self.view.drillRequested.connect(self._enter_drill_mode)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        outer.addWidget(self._build_toolbar())
        outer.addWidget(self.view, 1)
        outer.addWidget(self._build_footer())
        self.edge_popup = EdgeFlowPopup(self)

        # drill mode's detached "title card": a widget overlay (not a scene item, so it never
        # scales with zoom) pinned to the view's top-right corner at a constant on-screen size
        self.drill_title_card = QFrame(self)
        self.drill_title_card.setObjectName('drillTitleCard')
        drill_card_layout = QVBoxLayout(self.drill_title_card)
        drill_card_layout.setContentsMargins(16, 10, 16, 12)
        drill_card_layout.setSpacing(2)
        self.drill_title_tag = QLabel('')
        self.drill_title_tag.setFont(QFont('Consolas', 10, QFont.DemiBold))
        self.drill_title_name = QLabel('')
        self.drill_title_name.setFont(QFont('Consolas', 20, QFont.Bold))
        self.drill_title_name.setWordWrap(True)
        drill_card_layout.addWidget(self.drill_title_tag)
        drill_card_layout.addWidget(self.drill_title_name)
        self.drill_title_card.setMaximumWidth(320)
        self.drill_title_card.hide()
        self.resized.connect(self._position_drill_title_card)

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
        self.drill_back_btn = QPushButton('◀ Torna alla mappa completa')
        self.drill_back_btn.setFixedHeight(26)
        self.drill_back_btn.clicked.connect(self._exit_drill_mode)
        self.drill_back_btn.hide()  # only shown while drilled into one element's internals
        row.addWidget(self.drill_back_btn)
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

        self.heatmap_btn = QPushButton('Heatmap')
        self.heatmap_btn.setCheckable(True)
        self.heatmap_btn.setToolTip('Colora i nodi per connettività (caldo = molti riferimenti) invece che per tag')
        self.heatmap_btn.toggled.connect(self._on_heatmap_toggle)
        top.addWidget(self.heatmap_btn)

        # direction of the code flow (module rank + intra-cluster call depth): left-to-right by
        # default, this button flips the whole layout to right-to-left
        self.direction_btn = QPushButton('Direzione: Sx → Dx')
        self.direction_btn.setCheckable(True)
        self.direction_btn.setToolTip('Inverte la direzione del flusso logico del codice nella mappa')
        self.direction_btn.toggled.connect(self._on_direction_toggle)
        top.addWidget(self.direction_btn)

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

        # separate from node visibility above: which tags' reference connections are drawn at all
        # (e.g. show FN nodes but hide every edge touching a function) — same tag set, own state
        edge_tag_row = QHBoxLayout()
        edge_tag_row.setSpacing(6)
        edge_tag_row.addWidget(QLabel('Connessioni:'))
        self.edge_tag_buttons = {}
        for tag in self.TAG_ORDER:
            btn = QPushButton(tag)
            btn.setCheckable(True)
            btn.setChecked(tag in self._active_edge_tags)
            btn.setFixedHeight(26)
            btn.toggled.connect(lambda checked, t=tag: self._on_edge_tag_toggle(t, checked))
            self.edge_tag_buttons[tag] = btn
            edge_tag_row.addWidget(btn)
        edge_tag_row.addSpacing(10)
        self.containment_btn = QPushButton('Appartenenza')
        self.containment_btn.setCheckable(True)
        self.containment_btn.setChecked(self._show_containment)
        self.containment_btn.setFixedHeight(26)
        self.containment_btn.setToolTip('Connessione neutra che collega un elemento alla sua origine comune (modulo/classe)')
        self.containment_btn.toggled.connect(self._on_containment_toggle)
        edge_tag_row.addWidget(self.containment_btn)
        edge_tag_row.addStretch(1)
        rows.addLayout(edge_tag_row)

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
        self.drill_back_btn.setStyleSheet(theme.BUTTON_STYLE)
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
        for b in (self.isolate_btn, self.heatmap_btn, self.direction_btn, self.expand_all_btn, self.collapse_all_btn, self.relayout_btn):
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
        for tag, btn in self.edge_tag_buttons.items():
            color = theme.TAG_COLORS.get(tag, theme.DIM)
            bg = theme.TAG_BACKGROUNDS.get(tag, theme.PANEL)
            btn.setStyleSheet(
                f'QPushButton {{ border:1px solid {color}; border-radius:6px; padding:3px 10px; '
                f'font-weight:700; color:{color}; background:{theme.PANEL}; }} '
                f'QPushButton:checked {{ background:{bg}; color:{color}; }} '
                f'QPushButton:!checked {{ color:{theme.DIM}; border-color:{theme.BORDER}; }}'
            )
        # black, not a tag color — this toggle is the neutral belonging connection itself
        self.containment_btn.setStyleSheet(
            f'QPushButton {{ border:1px solid #000000; border-radius:6px; padding:3px 10px; '
            f'font-weight:700; color:#000000; background:{theme.PANEL}; }} '
            f'QPushButton:checked {{ background:#00000022; color:#000000; }} '
            f'QPushButton:!checked {{ color:{theme.DIM}; border-color:{theme.BORDER}; }}'
        )
        self.view.apply_style()
        self.edge_popup.apply_style()
        self.drill_title_card.setStyleSheet(
            f'#drillTitleCard {{ background:{theme.PANEL}; border:2px solid {theme.ACCENT}; border-radius:10px; }}'
        )
        self.drill_title_tag.setStyleSheet(f'color:{theme.ACCENT}; letter-spacing:1px; border:none;')
        self.drill_title_name.setStyleSheet(f'color:{theme.TEXT}; border:none;')

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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parentWidget()
        if not self._positioned and parent is not None:
            self.resize(int(parent.width() * 0.96), int(parent.height() * 0.94))
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
        self._pinned_nodes = []
        self._project_name = project_name
        self.title_label.setText(f'Mappa KANT — {project_name}' if project_name else 'Mappa KANT')
        files = sorted({el.file for el in elements.values()})
        # every open starts fully expanded, regardless of how it was left last time
        self._expanded = set(files)
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
        self._refresh(relayout=True, fit=True)
        self._save_positions()

    def closeEvent(self, event):
        self._position_timer.stop()
        self._save_positions()
        super().closeEvent(event)

    def _element_label(self, key):
        element = self._display.get(key) or self._elements.get(key)
        return f'[{element.tag}] {element.desc} — {element.file}' if element else key

    def _position_popup(self, scene_point):
        viewport_point = self.view.mapFromScene(scene_point)
        point = self.view.viewport().mapTo(self, viewport_point)
        x = min(max(8, point.x() + 14), max(8, self.width() - self.edge_popup.width() - 8))
        y = point.y() + 14
        if y + self.edge_popup.height() > self.height() - 8:
            y = max(8, point.y() - self.edge_popup.height() - 14)
        self.edge_popup.move(x, y)
        self.edge_popup.raise_()
        self.edge_popup.show()

    def _show_edge_popup(self, source, target, scene_point, pinned):
        source_element = self._display.get(source)
        target_element = self._display.get(target)
        if source_element is None or target_element is None:
            return
        incoming = [self._element_label(key) for key in target_element.incoming_detail]
        outgoing = [self._element_label(key) for key in source_element.outgoing_detail]
        self.edge_popup.set_zoom_scale(self.view.transform().m11())
        self.edge_popup.set_flow(
            f'{self._element_label(source)}  →  {self._element_label(target)}',
            incoming, outgoing, pinned,
        )
        self._position_popup(scene_point)

    # [FN] _show_node_popup — same explanatory popup as an edge's, but for one node's own
    # incoming/outgoing rather than a source→target flow
    # [FN OPEN] _show_node_popup
    def _show_node_popup(self, key, scene_point, pinned):
        element = self._display.get(key)
        if element is None or getattr(element, 'is_anchor', False):
            return  # a common-origin anchor has no real data of its own to explain
        incoming = [self._element_label(k) for k in (element.incoming_detail or element.incoming)]
        outgoing = [self._element_label(k) for k in (element.outgoing_detail or element.outgoing)]
        self.edge_popup.set_zoom_scale(self.view.transform().m11())
        self.edge_popup.set_flow(self._element_label(key), incoming, outgoing, pinned)
        self._position_popup(scene_point)
    # [FN CLOSED] _show_node_popup

    # [FN CATEGORY] hover popups — a short delay (_hover_show_timer) between hovering and the popup
    # actually appearing, so a mouse simply passing over an edge or node on the way elsewhere
    # doesn't pop a window open; leaving before the delay elapses just cancels the pending show.
    # Shared by both edges (source→target flow) and nodes (one element's own incoming/outgoing).
    # [FN] _on_edge_hovered / _on_node_hovered / _show_pending_hover
    # [FN OPEN] hover popups
    def _on_edge_hovered(self, source, target, scene_point, entered):
        if self._pinned_edge is not None:
            return
        if entered:
            self._edge_hide_timer.stop()
            self._pending_hover = ('edge', (source, target, scene_point))
            self._hover_show_timer.start(450)
        else:
            self._hover_show_timer.stop()
            self._pending_hover = None
            self._edge_hide_timer.start(120)

    def _on_node_hovered(self, key, scene_point, entered):
        if self._pinned_edge is not None:
            return
        if entered:
            self._edge_hide_timer.stop()
            self._pending_hover = ('node', (key, scene_point))
            self._hover_show_timer.start(450)
        else:
            self._hover_show_timer.stop()
            self._pending_hover = None
            self._edge_hide_timer.start(120)

    def _show_pending_hover(self):
        if self._pending_hover is None:
            return
        kind, args = self._pending_hover
        if kind == 'edge':
            self._show_edge_popup(*args, False)
        else:
            self._show_node_popup(*args, False)
    # [FN CLOSED] hover popups

    def _on_edge_pinned(self, source, target, scene_point):
        edge = (source, target)
        if self._pinned_edge == edge:
            self._pinned_edge = None
            self.edge_popup.hide()
            return
        self._hover_show_timer.stop()
        self._pending_hover = None
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
    # draw the ▸/▾ affordance. `.outgoing`/`.incoming` are deduped per target module (one arrow per
    # module pair); `.outgoing_detail`/`.incoming_detail` keep every underlying real element key
    # that feeds into that arrow, so the edge popup can list all of them, not just one.
    # [FN] _display_elements — computes the filtered + collapsed node set for the view
    # [FN OPEN] _display_elements
    def _display_elements(self):
        if self._drill_key is not None:
            return self._drill_display()
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
                    parent=de.parent,
                )
                collapsible = dk == roots.get(de.file) and counts.get(de.file, 0) > 1
                node.collapsed = (de.file not in expanded) if collapsible else None
                # real (uncollapsed) element keys behind this node's aggregated incoming/outgoing —
                # .incoming/.outgoing are deduped per target module (one arrow per module pair), but
                # the edge popup needs every individual reference, not just "some module points here"
                node.outgoing_detail = []
                node.incoming_detail = []
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
                disp[a].outgoing_detail.append(tk)
                disp[b].incoming_detail.append(k)

        if self._focus_file is not None:
            keep = {k for k, e in disp.items() if e.file == self._focus_file}
            for k in list(keep):
                keep |= set(disp[k].incoming) | set(disp[k].outgoing)
            disp = {k: e for k, e in disp.items() if k in keep}
            self._prune_edges(disp)
        if self._isolate and self._pinned_nodes:
            keep = set()
            for pinned_key in self._pinned_nodes:
                if pinned_key in disp:
                    keep |= {pinned_key} | set(disp[pinned_key].incoming) | set(disp[pinned_key].outgoing)
            if keep:
                disp = {k: e for k, e in disp.items() if k in keep}
                self._prune_edges(disp)
        if self._show_containment:
            self._add_common_origin_anchors(disp)
        return disp
    # [FN CLOSED] _display_elements

    # [FN CATEGORY] _drill_display — the internal-only view for one element: ONLY its direct
    # children, with only the reference edges where BOTH ends are children (no outside callers/
    # callees, no sibling clusters, no grandchildren) — just the geography of how the children
    # relate to each other. The parent itself is deliberately excluded from this set: it's not a
    # node in this graph at all, it's rendered as a fixed title card instead (see _enter_drill_mode).
    # Node-visibility/edge-tag/isolate filters don't apply here; drilling in is its own lens.
    # [FN] _drill_display — builds the children-only node set for drill-down mode
    # [FN OPEN] _drill_display
    def _drill_display(self):
        if self._drill_key not in self._elements:
            return {}
        children = {key for key, element in self._elements.items() if element.parent == self._drill_key}
        disp = {}
        for key in children:
            e = self._elements[key]
            node = XrefElement(
                key=e.key, uid=e.uid, tag=e.tag, name=e.name, desc=e.desc,
                file=e.file, order=e.order, category_desc=e.category_desc,
                parent=None,  # the real parent is excluded from this graph entirely
            )
            node.outgoing_detail = []
            node.incoming_detail = []
            disp[key] = node
        for key in children:
            for target in self._elements[key].outgoing:
                if target in children and target != key:
                    if target not in disp[key].outgoing:
                        disp[key].outgoing.append(target)
                    if key not in disp[target].incoming:
                        disp[target].incoming.append(key)
                    disp[key].outgoing_detail.append(target)
                    disp[target].incoming_detail.append(key)
        return disp
    # [FN CLOSED] _drill_display

    # [FN CATEGORY] _enter_drill_mode / _exit_drill_mode — switches the map between the full
    # project graph and one element's internal-only view. The drilled element is detached from
    # the graph entirely: it becomes a fixed title card (drill_title_card) pinned to the
    # viewport's top-right corner at a constant on-screen size, independent of zoom/pan — a
    # widget overlay, not a scene item, since scene items scale with the view's transform.
    # [FN] _enter_drill_mode / _exit_drill_mode
    # [FN OPEN] drill mode
    def _enter_drill_mode(self, key):
        if key not in self._elements:
            return
        self._drill_key = key
        self.view.set_pinned([])
        element = self._elements[key]
        self.drill_title_tag.setText(f'[{element.tag}]')
        self.drill_title_name.setText(element.desc or element.name)
        self.drill_title_card.show()
        self._position_drill_title_card()
        self.title_label.setText(f'Mappa KANT — dentro {self._element_label(key)}')
        self._refresh(relayout=True, fit=True)
        self.drill_back_btn.show()

    def _exit_drill_mode(self):
        self._drill_key = None
        self.drill_back_btn.hide()
        self.drill_title_card.hide()
        self.title_label.setText(f'Mappa KANT — {self._project_name}' if self._project_name else 'Mappa KANT')
        self._refresh(fit=True, relayout=True)
    # [FN CLOSED] drill mode

    def _position_drill_title_card(self):
        if not self.drill_title_card.isVisible():
            return
        self.drill_title_card.adjustSize()
        view_geo = self.view.geometry()
        x = view_geo.right() - self.drill_title_card.width() - 20
        y = view_geo.top() + 20
        self.drill_title_card.move(x, y)
        self.drill_title_card.raise_()

    # [FN CATEGORY] _add_common_origin_anchors — a filter/collapse can remove an element's own
    # parent from the displayed set while leaving several of its siblings visible; without this
    # they'd show no trace of coming from the same place. Synthesizes one small unmarked "common
    # origin" anchor node per orphaned parent (only when 2+ siblings survive together — a single
    # orphan has no sibling to visually connect with) and re-parents those children onto it, so the
    # existing containment-edge drawing in XrefMapView.set_data — and the secondary origin-clustering
    # force in _force_layout_positions — both apply to it exactly like a real parent, no new code
    # paths needed there.
    # [FN] _add_common_origin_anchors — synthesizes anchor nodes for orphaned sibling groups
    # [FN OPEN] _add_common_origin_anchors
    def _add_common_origin_anchors(self, disp):
        orphans = {}
        for key, element in disp.items():
            if element.parent and element.parent not in disp:
                orphans.setdefault(element.parent, []).append(key)
        for parent_key, children in orphans.items():
            if len(children) < 2:
                continue
            anchor_key = f'__anchor__::{parent_key}'
            anchor = XrefElement(
                key=anchor_key, uid=anchor_key, tag='', name='', desc='',
                file=disp[children[0]].file, order=-1,
            )
            anchor.is_anchor = True
            disp[anchor_key] = anchor
            for child_key in children:
                disp[child_key].parent = anchor_key
    # [FN CLOSED] _add_common_origin_anchors

    @staticmethod
    def _prune_edges(disp):
        for e in disp.values():
            e.outgoing = [k for k in e.outgoing if k in disp]
            e.incoming = [k for k in e.incoming if k in disp]

    # [FN CATEGORY] _refresh — rebuilds self._display from current filters, then hands it to the
    # view. Plain refresh (fit=False, relayout=False) keeps every node exactly where it already is —
    # used for things that don't change which nodes are visible (selection, drag persistence).
    # relayout=True recomputes positions for the new element set instead, warm-started from where
    # nodes already are, and animates the transition — used whenever the filtered/collapsed set
    # itself changes (tag toggle, file focus, isolate, expand/collapse, search), so removing or
    # adding nodes lets the layout actually resettle instead of leaving survivors pinned to spots
    # chosen for a different set of neighbours.
    # [FN] _refresh — recomputes the displayed graph and (optionally) relays it out
    # [FN OPEN] _refresh
    def _refresh(self, fit=False, relayout=False):
        old_transform = self.view.transform()
        old_center = self.view.mapToScene(self.view.viewport().rect().center())
        self._display = self._display_elements()
        if relayout and self.view._node_items:
            self._positions.update(self.view.relayout_to(self._display, self._positions))
        else:
            self.view.set_data(self._display, self._positions)
            self._positions.update(self.view.positions())
        self._position_timer.start(250)
        visible_edges = {(source, target) for source, target, *_rest in self.view._edges}
        if self._pinned_edge not in visible_edges:
            self._pinned_edge = None
            self.edge_popup.hide()
        self.view.set_pinned([k for k in self._pinned_nodes if k in self._display])
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
        self._refresh(relayout=True)

    def _on_edge_tag_toggle(self, tag, checked):
        if checked:
            self._active_edge_tags.add(tag)
        else:
            self._active_edge_tags.discard(tag)
        self.view.set_active_edge_tags(self._active_edge_tags)
        # node size/heatmap intensity and the layout itself are also driven by (filtered) degree
        # now, not just which edges get drawn — so this needs a real relayout, not just a redraw
        self._refresh(relayout=True)

    def _on_containment_toggle(self, checked):
        self._show_containment = checked
        self.view.set_show_containment(checked)
        self._refresh(relayout=True)

    def _on_direction_toggle(self, checked):
        self._rtl = checked
        self.direction_btn.setText('Direzione: Dx → Sx' if checked else 'Direzione: Sx → Dx')
        self.view.set_direction(checked)
        self._refresh(relayout=True)

    def _on_file_filter(self, _index):
        self._focus_file = self.file_combo.currentData()
        self._refresh(relayout=True)

    def _on_isolate(self, checked):
        self._isolate = checked
        self._refresh(relayout=True)
        if checked and self._pinned_nodes:
            # multiple nodes may be pinned; fit the whole isolated neighbourhood rather than
            # re-centering (and re-pinning) on just one of them
            self.view.fit()

    def _on_heatmap_toggle(self, checked):
        self.view.recolor(checked)

    def _expand_all(self):
        self._expanded = {e.file for e in self._elements.values()}
        self._refresh(relayout=True)

    def _collapse_all(self):
        self._expanded = set()
        self._refresh(relayout=True)

    # [FN] _on_nodes_pinned — mirrors the view's multi-pin set at the dialog level
    # [FN OPEN] _on_nodes_pinned
    def _on_nodes_pinned(self, keys):
        self._pinned_nodes = list(keys)
        self._selected = keys[-1] if keys else None
        drillable = keys[0] if len(keys) == 1 and self._is_drillable(keys[0]) else None
        self.view.set_drillable(drillable)
        if self._isolate:
            self._refresh(relayout=True)
    # [FN CLOSED] _on_nodes_pinned

    # [FN] _is_drillable — a real KANT tree parent has ≥2 direct children AND at least one child
    # references another child directly; only then is drilling into it more informative than the
    # main map already is. Uses the full undisplayed graph, not the filtered/collapsed view.
    # [FN OPEN] _is_drillable
    def _is_drillable(self, key):
        children = [k for k, e in self._elements.items() if e.parent == key]
        if len(children) < 2:
            return False
        child_set = set(children)
        return any(target in child_set for child in children for target in self._elements[child].outgoing)
    # [FN CLOSED] _is_drillable

    def selected_key(self):
        """Most-recently-pinned element key, or None — read when the map closes so the coding
        panel underneath can open whatever was last pinned."""
        return self._selected

    # [FN CATEGORY] _on_node_activated — double-click routing: on a collapsible module root it toggles
    # that file's expansion; on any other node it re-emits nodeActivated so the editor jumps to the
    # element. (Single click always just pins/highlights, via _on_nodes_pinned.)
    # [FN] _on_node_activated — expands a module or opens an element on double-click
    # [FN OPEN] _on_node_activated
    def _on_node_activated(self, key):
        node = self._display.get(key)
        if node is not None and getattr(node, 'collapsed', None) is not None:
            self._expanded.symmetric_difference_update({node.file})
            self._refresh(relayout=True)
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
                    self._refresh(relayout=True)
                self._selected = key
                self._pinned_nodes = [key]
                self.view.focus_on(key)
                return
    # [FN CLOSED] _on_search

    def _on_search_enter(self):
        self._on_search(self.search_box.text())
# [FN CLOSED] XrefMapDialog
