"""Qt widgets: editor, terminal, Claude pane, sections, tree, title bar, tabs."""
import locale
import math
import os
import re
import shutil
import tempfile
from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QPointF, QProcess, QRect, Qt, QSettings, QSize, Signal, QTimer
from PySide6.QtGui import (
    QBrush, QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QPolygonF,
    QShortcut, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSizeGrip, QSplitter, QStackedWidget, QTabWidget, QToolButton,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from kant import theme
from kant.model import Node, serialize_kant
from kant.fileio import write_file_atomic
from kant.syntax import KEYWORDS, TOKEN_RE


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


def _agent_command(agent, prompt):
    if agent == 'codex':
        return 'codex', ['exec', prompt]
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
        self.current_agent = 'claude'
        self.validate_after_finish = False
        self.log_path = os.path.join(tempfile.gettempdir(), 'kant-ai-terminal.log')
        self.encoding = locale.getpreferredencoding(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.title = QLabel('AI TERMINAL')
        self.title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        header.addWidget(self.title)
        header.addStretch(1)
        self.agent_select = QComboBox()
        self.agent_select.addItem('Claude Code', 'claude')
        self.agent_select.addItem('Codex', 'codex')
        header.addWidget(self.agent_select)
        layout.addLayout(header)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        self.output.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:8px; padding:8px;'
        )
        layout.addWidget(self.output, 1)

        self.prompt = QPlainTextEdit()
        self.prompt.setFixedHeight(90)
        self.prompt.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        self.prompt.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:8px; padding:6px;'
        )
        layout.addWidget(self.prompt)

        self.send_btn = QPushButton('Invia')
        self.send_btn.clicked.connect(self._send)
        self.send_btn.setStyleSheet(theme.BUTTON_STYLE + f'QPushButton {{ color:{theme.WARN}; border-color:{theme.WARN}; }}')
        layout.addWidget(self.send_btn)

        self.agent_select.currentIndexChanged.connect(self._agent_changed)
        self._agent_changed()
        self.apply_style()

    def apply_style(self):
        self.setStyleSheet(f'background:{theme.PANEL}; border-left:1px solid {theme.BORDER};')
        self.title.setStyleSheet(f'color:{theme.WARN}; letter-spacing:2px;')
        self.output.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:8px; padding:8px;'
        )
        self.prompt.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:8px; padding:6px;'
        )
        self.agent_select.setStyleSheet(theme.BUTTON_STYLE)
        self.send_btn.setStyleSheet(theme.BUTTON_STYLE + f'QPushButton {{ color:{theme.WARN}; border-color:{theme.WARN}; }}')

    def _agent(self):
        return self.agent_select.currentData() or 'claude'

    def set_agent(self, agent):
        idx = self.agent_select.findData(agent)
        if idx != -1:
            self.agent_select.setCurrentIndex(idx)

    def _agent_changed(self):
        prompt = 'Prompt per codex exec...' if self._agent() == 'codex' else 'Prompt per claude -p...'
        self.prompt.setPlaceholderText(prompt)

    def set_cwd(self, cwd):
        self.cwd = cwd
        self._append(f'# cwd: {cwd}\n')

    def _append(self, text):
        self.output.moveCursor(QTextCursor.End)
        self.output.insertPlainText(text)
        self.output.moveCursor(QTextCursor.End)
        try:
            with open(self.log_path, 'a', encoding='utf-8', newline='') as f:
                f.write(text)
        except OSError:
            pass

    def write_info(self, text):
        if not text.endswith('\n'):
            text += '\n'
        self._append(text)

    def _send(self):
        if self.process is not None:
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
    def run_prompt(self, prompt, extra_skills=(), agent=None):
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
        self._append(f'\n[{agent_label}]> {prompt}\n')
        if not executable:
            self._append(f'{command} non trovato nel PATH.\n')
            return False
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
        _, args = _agent_command(agent, prompt)
        self.process = QProcess(self)
        self.process.setWorkingDirectory(self.cwd)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._error)
        self.process.finished.connect(self._finished)
        self.send_btn.setText('Stop')
        self.send_btn.setEnabled(True)
        self._append(f'[{agent_label} avvio]\n')
        # force the skill bodies onto this call, invisibly — CLI flags, not part of the visible
        # prompt echoed above, so they never show up in the transcript
        if agent == 'claude' and system_prompt:
            if len(system_prompt) > 6000:
                self.system_prompt_file = _write_system_prompt_file(system_prompt)
                args += ['--append-system-prompt-file', self.system_prompt_file]
            else:
                args += ['--append-system-prompt', system_prompt]
        self.process.start(executable, args)
        # claude -p reads its prompt from -p, never stdin; without this it waits ~3s for piped input
        # that never comes ("no stdin data received in 3s") before proceeding — closing the write
        # channel signals EOF immediately so it starts right away
        self.process.closeWriteChannel()
        return True
    # [FN CLOSED] run_prompt

    def _read_stdout(self):
        self._append(bytes(self.process.readAllStandardOutput()).decode(self.encoding, errors='replace'))

    def _read_stderr(self):
        self._append(bytes(self.process.readAllStandardError()).decode(self.encoding, errors='replace'))

    def _cleanup_system_prompt_file(self):
        if self.system_prompt_file:
            try:
                os.unlink(self.system_prompt_file)
            except OSError:
                pass
            self.system_prompt_file = None

    def _error(self, _error):
        if self.process is not None:
            self._append(f'\n[{_agent_label(self.current_agent)} errore: {self.process.errorString()}]\n')
        self._cleanup_system_prompt_file()
        self.process = None
        self.send_btn.setText('Invia')
        self.send_btn.setEnabled(True)
        self.finished.emit()

    def _finished(self, exit_code, _status):
        if self.process is None:
            return
        if exit_code:
            self._append(f'\n[{_agent_label(self.current_agent)} exit code {exit_code}]\n')
        else:
            self._append(f'\n[{_agent_label(self.current_agent)} completato]\n')
        self._cleanup_system_prompt_file()
        self.process = None
        self.send_btn.setText('Invia')
        self.send_btn.setEnabled(True)
        self.finished.emit()


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


# [FN CATEGORY] CollapsibleSection — a tagged element that has nested tagged children: a header you
# can fold, with a left accent bar echoing the HTML version's border-left indent
# [FN] CollapsibleSection — collapsible container for a non-leaf KANT node
# [FN OPEN] CollapsibleSection
class CollapsibleSection(QWidget):
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

        header = QLabel(_tag_header_html(node.tag, node.name, node.desc))
        header.setTextFormat(Qt.RichText)
        header.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        header.setCursor(Qt.PointingHandCursor)
        header.mousePressEvent = lambda _event: self.toggle_btn.click()
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.addWidget(self.toggle_btn)
        header_row.addWidget(header, 1)
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

        header = QLabel(_tag_header_html(node.tag, node.name, node.desc))
        header.setTextFormat(Qt.RichText)
        header.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        if compact:
            header.setStyleSheet(f'padding:4px 0; border-bottom:1px solid #eef2f7;')
        outer.addWidget(header)

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
                widget.setMaximumWidth(max(avail - depth * self.indentation(), 60))
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

        self.save_btn = QPushButton('Salva')
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(window._save_file)
        layout.addWidget(self.save_btn)
        self.save_btn.hide()

        self.run_btn = QPushButton('▶')
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(window._run_current_file)
        layout.addWidget(self.run_btn)
        self.run_btn.hide()

        layout.addStretch(1)
        self.theme_btn = QPushButton('Giorno' if window.night_mode else 'Notte')
        self.theme_btn.clicked.connect(window._toggle_theme)
        layout.addWidget(self.theme_btn)
        self.theme_btn.hide()
        self.buttons = [
            self.back_btn, self.file_menu_btn, self.search_menu_btn, self.appearance_menu_btn,
            self.git_menu_btn, self.save_btn, self.run_btn, self.theme_btn,
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
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setMenu(QMenu(btn))
        btn.setFixedHeight(28)
        return btn

    def apply_style(self):
        self.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        self.theme_btn.setText('Giorno' if self.window.night_mode else 'Notte')
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

    def __init__(self, path, tree, line_ending='LF'):
        super().__init__()
        self.path = path
        self.tree = tree
        self.dirty = False
        self.filter_uid = None
        self.line_ending = line_ending
        self.section_widgets = {}  # uid -> CollapsibleSection | LeafSection
        self.collapsibles = []

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

    def save(self):
        try:
            write_file_atomic(self.path, serialize_kant(self.tree))
        except OSError as e:
            self.dirty = True
            self.saveFailed.emit(str(e))
            self.dirtyChanged.emit()
            return False
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
