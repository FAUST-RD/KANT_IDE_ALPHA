"""Reusable Qt components, ordered by feature rather than application flow.

AI navigation:
- editor/terminal primitives: ``KantHighlighter`` through ``TerminalPane``;
- agent process and review UI: ``ClaudePane`` and ``_AiReviewCard``;
- KANT section/tree chrome: section widgets, ``ProjectTree``, and ``TitleBar``;
- file state: ``FileTab``.

The MAPPA subsystem (layout helpers, ``XrefMapView``, ``XrefMapDialog``) lives in ``mappa.py``.

Application-wide coordination stays in ``mainwindow.py``. Filesystem transactions and rollback
stay in ``workspace.py``; widgets expose signals/callbacks instead of importing ``MainWindow``.
"""
import hashlib
import locale
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from html import escape as html_escape
from pathlib import Path

import shiboken6

from PySide6.QtCore import (
    QElapsedTimer, QFileSystemWatcher, QObject, QPoint, QPointF, QProcess, QRect, QRectF, Qt, QSettings,
    QSize, QStringListModel, Signal, QTimer,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
    QPainterPathStroker, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QCompleter, QDialog, QFileDialog, QFrame,
    QGraphicsDropShadowEffect, QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
    QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenuBar, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSizeGrip, QSplitter, QStackedWidget, QTabWidget, QToolButton,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from kant import theme
from kant.aipermissions import PermissionBridge, write_permission_config
from kant.icons import draw_icon
from kant.model import Node, parse_kant, serialize_kant, KantParseError
from kant.fileio import file_fingerprint, write_file_atomic, safe_mkstemp
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


# [FN CATEGORY] DiffHighlighter — colors whole unified-diff lines by their leading character
# (+/-/@@), the same one-format-per-block shape as KantHighlighter but keyed on the line's start
# instead of a token regex. Background is the foreground color at low alpha rather than a literal
# hex tint, so it reads correctly against CODE_BG in both day and night theme without a second
# color pair to keep in sync.
# [FN] DiffHighlighter — green/red backgrounds for added/removed unified-diff lines
# [FN OPEN] DiffHighlighter
class DiffHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.fmt_added = self._fmt(theme.OK)
        self.fmt_removed = self._fmt(theme.TAG_COLORS['TST'])
        self.fmt_header = self._fmt(theme.ACCENT, bold=True, tint=False)

    @staticmethod
    def _fmt(color, bold=False, tint=True):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Bold)
        if tint:
            bg = QColor(color)
            bg.setAlpha(30)
            fmt.setBackground(bg)
        return fmt

    def highlightBlock(self, text):
        if text.startswith('+++') or text.startswith('---') or text.startswith('@@'):
            self.setFormat(0, len(text), self.fmt_header)
        elif text.startswith('+'):
            self.setFormat(0, len(text), self.fmt_added)
        elif text.startswith('-'):
            self.setFormat(0, len(text), self.fmt_removed)
# [FN CLOSED] DiffHighlighter


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


# [CST] _VIM_MODE_ENABLED — module-level (not per-instance) so one Aspetto-menu toggle flips modal
# editing for every CodeEdit at once, the same way theme.py's own day/night globals work. List-
# wrapped so set_vim_mode can rebind the value from anywhere without a `global` statement.
_VIM_MODE_ENABLED = [False]


def set_vim_mode(enabled):
    _VIM_MODE_ENABLED[0] = enabled


def vim_mode_enabled():
    return _VIM_MODE_ENABLED[0]


# [CST] _VIM_REGISTER — the default (unnamed) yank/delete register, shared across every CodeEdit
# instance exactly like real vim's default register is shared across buffers — yanking in one KANT
# element and pasting into another must work, so this can't live on a single widget.
_VIM_REGISTER = {'text': '', 'linewise': False}


# [FN CATEGORY] CodeEdit — an editable, syntax-highlighted code block that auto-grows to fit its
# content instead of scrolling internally, mirroring the HTML version's contenteditable blocks.
# VIM-style modal editing (see set_vim_mode/vim_mode_enabled) is layered on top of the normal
# QPlainTextEdit behavior: Normal mode intercepts keys as commands, Insert mode is the original
# always-was-there typing behavior. When vim mode is off, keyPressEvent skips the vim dispatch
# entirely — every keystroke behaves exactly as before, so this can't change the experience for
# anyone not using it.
# [FN] CodeEdit — QPlainTextEdit wired with the highlighter and auto-resize
# [FN OPEN] CodeEdit
class CodeEdit(QPlainTextEdit):
    vim_mode_changed = Signal(str)  # emitted on every vim_state transition, for a status-bar indicator

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

        # autocomplete-as-you-type: mainwindow sets completion_provider to a callable(edit) that
        # fires an async LSP textDocument/completion request (or a local fallback) and later calls
        # back into show_completions on THIS same instance — kept as a callback, not a direct
        # import, so this module stays decoupled from mainwindow (see module docstring)
        self.completion_provider = None
        self._completer = QCompleter(self)
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QCompleter.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.activated.connect(self._insert_completion)
        self._completion_timer = QTimer(self)
        self._completion_timer.setSingleShot(True)
        self._completion_timer.timeout.connect(self._trigger_completion)
        self.textChanged.connect(self._on_text_changed_for_completion)

        # PyCharm-style quick-doc-on-hover: mainwindow sets hover_provider to a callable(edit,
        # cursor, global_pos) that fires an async LSP textDocument/hover (or a local fallback) and
        # shows the result via show_code_hover_popup (a themed popup, not the native QToolTip) —
        # same 450ms delay already used for MAPPA's own hover popups
        self.hover_provider = None
        self.setMouseTracking(True)
        self._hover_pos = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._trigger_hover)

        # gesture vocabulary matching other IDEs: Ctrl+Click jumps to a symbol's definition, F2
        # renames it — both just resolve the cursor here and hand off to mainwindow's existing
        # LSP/local rename-definition plumbing (same callback-not-import pattern as the providers
        # above)
        self.definition_provider = None
        self.rename_provider = None

        # VIM state — 'normal' is where vim mode always starts (matching real vim); irrelevant
        # whenever vim_mode_enabled() is False, since keyPressEvent skips the vim dispatch entirely
        # in that case rather than checking this. Structural actions this widget can't resolve on
        # its own (moving to an adjacent element, folding, search, the : command bar, undo/redo)
        # go through ONE callback set by mainwindow — vim_action(edit, name) — instead of one
        # callback per action, keeping this module's own "callback not import" decoupling from
        # mainwindow intact despite vim touching much more of the app than completion/hover do.
        self.vim_state = 'normal'
        self._vim_count = ''
        self._vim_pending_operator = None
        self._vim_pending_prefix = None
        self._vim_visual_anchor = None
        self.vim_action = None

    def _auto_resize(self):
        # the rangeChanged connection above defers this call via QTimer.singleShot(0, ...) — if
        # this CodeEdit's tab closes between scheduling and firing, the C++ side is already gone
        # by the time this runs (a bound-method QTimer.singleShot callback isn't auto-disconnected
        # on target deletion the way a direct signal/slot connection is), and touching any Qt
        # method below raises "Internal C++ object already deleted" instead of silently no-oping
        if not shiboken6.isValid(self):
            return
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

    # [FN CATEGORY] autocomplete-as-you-type — QCompleter driven by an async provider (LSP
    # textDocument/completion, or a local fallback) instead of a static word list. Typing restarts
    # a short debounce timer; when it fires, completion_provider (set by mainwindow) is asked for
    # fresh candidates and calls back into show_completions on this same widget once they arrive.
    # keyPressEvent only needs to get out of the popup's way for accept/dismiss keys — Up/Down
    # navigation and the popup's own positioning are handled by QCompleter itself (setWidget(self)
    # installs its event filter), matching Qt's own "Custom Completer" reference pattern.
    # [FN] keyPressEvent — lets the completer popup handle accept/dismiss keys when it's open
    # [FN OPEN] keyPressEvent
    def keyPressEvent(self, event):
        if self._completer.popup().isVisible() and event.key() in (
            Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape, Qt.Key_Tab, Qt.Key_Backtab,
        ):
            event.ignore()
            return
        if event.key() == Qt.Key_F2 and self.rename_provider is not None:
            self.rename_provider(self)
            return
        if vim_mode_enabled() and self._vim_key_press(event):
            return
        super().keyPressEvent(event)
    # [FN CLOSED] keyPressEvent

    # [FN CATEGORY] _vim_key_press — the modal dispatch: Insert mode only intercepts Escape (every
    # other key falls through to normal QPlainTextEdit typing, unchanged); Normal/Visual intercept
    # everything, since letting an unmapped key insert text there would break the whole point of a
    # modal editor. Returns True when the event was consumed (caller must not call super()), False
    # to fall through to the original QPlainTextEdit handling.
    # [FN] _vim_key_press — routes one key event through the current vim mode
    # [FN OPEN] _vim_key_press
    def _vim_key_press(self, event):
        key = event.key()
        text = event.text()

        if self.vim_state == 'insert':
            if key == Qt.Key_Escape:
                self._vim_enter_normal(move_left=True)
                return True
            return False

        # navigation/editing convenience keys always pass through untouched, in every vim state —
        # intercepting them would only be annoying, not more "vim", since they don't insert text
        if key in (
            Qt.Key_Home, Qt.Key_End, Qt.Key_PageUp, Qt.Key_PageDown,
            Qt.Key_Backspace, Qt.Key_Delete,
        ):
            return False

        if key == Qt.Key_Escape:
            self._vim_pending_operator = None
            self._vim_pending_prefix = None
            self._vim_count = ''
            if self.vim_state in ('visual', 'visual_line'):
                self._vim_visual_anchor = None
                cursor = self.textCursor()
                cursor.clearSelection()
                self.setTextCursor(cursor)
                self._vim_enter_normal()
            return True

        # a pending g/z prefix consumes exactly the next key (gg, G already has its own key so
        # only 'g' needs the prefix; za is the only z-command supported)
        if self._vim_pending_prefix is not None:
            prefix, self._vim_pending_prefix = self._vim_pending_prefix, None
            self._vim_count = ''
            if prefix == 'g' and text == 'g':
                self._vim_dispatch_action('first_element')
            elif prefix == 'z' and text == 'a':
                self._vim_dispatch_action('toggle_fold')
            return True

        # numeric count prefix — a leading '0' is the start-of-line motion, not a count digit
        if text.isdigit() and not (text == '0' and not self._vim_count):
            self._vim_count += text
            return True
        count = int(self._vim_count) if self._vim_count else 1

        if self._vim_pending_operator is not None:
            operator, self._vim_pending_operator = self._vim_pending_operator, None
            self._vim_count = ''
            self._vim_apply_operator(operator, key, text, count)
            return True

        if key in (Qt.Key_D, Qt.Key_Y, Qt.Key_C) and text in ('d', 'y', 'c') and self.vim_state == 'normal':
            self._vim_pending_operator = text
            return True

        if self.vim_state in ('visual', 'visual_line') and text in ('d', 'x', 'y', 'c'):
            self._vim_apply_visual_operator('d' if text == 'x' else text)
            self._vim_count = ''
            return True

        if self._vim_simple_command(key, text, count):
            self._vim_count = ''
            return True

        cursor = self.textCursor()
        mode = QTextCursor.KeepAnchor if self.vim_state in ('visual', 'visual_line') else QTextCursor.MoveAnchor
        if self._vim_move(cursor, key, text, count, mode):
            self.setTextCursor(cursor)
        self._vim_count = ''
        return True
    # [FN CLOSED] _vim_key_press

    def _vim_dispatch_action(self, name, **kwargs):
        if self.vim_action is not None:
            self.vim_action(self, name, **kwargs)

    # [FN CATEGORY] _vim_move — the shared motion table for plain cursor movement, operator ranges
    # (called with QTextCursor.KeepAnchor), and visual-selection extension (same). j/k fall through
    # to the adjacent element at a block boundary ONLY for plain movement (MoveAnchor) — jumping
    # widgets mid-operator or mid-selection has no sensible meaning, so that case is disabled.
    # [FN] _vim_move — applies one motion key to `cursor`, `count` times, in the given selection mode
    # [FN OPEN] _vim_move
    def _vim_move(self, cursor, key, text, count, mode):
        if key in (Qt.Key_H, Qt.Key_Left) and text in ('h', ''):
            cursor.movePosition(QTextCursor.Left, mode, count)
            return True
        if key in (Qt.Key_L, Qt.Key_Right) and text in ('l', ''):
            cursor.movePosition(QTextCursor.Right, mode, count)
            return True
        if key in (Qt.Key_J, Qt.Key_Down) and text in ('j', ''):
            # QTextDocument always materializes a trailing empty block for any text ending in a
            # line terminator (single \n, or a CRLF-preserved \r with nothing after it — see
            # fileio.detect_line_ending's docstring for why CR bytes can end up embedded verbatim).
            # Real vim doesn't treat a file's single trailing newline as its own landable last
            # line, so drop one trailing empty block here to match — a second consecutive blank
            # block (a genuinely blank last line) still counts, same as real vim.
            last_block = self.document().blockCount() - 1
            if last_block > 0 and not self.document().findBlockByNumber(last_block).text():
                last_block -= 1
            for _ in range(count):
                if cursor.blockNumber() >= last_block:
                    if mode == QTextCursor.MoveAnchor:
                        self._vim_dispatch_action('next_element')
                    break
                cursor.movePosition(QTextCursor.Down, mode)
            return True
        if key in (Qt.Key_K, Qt.Key_Up) and text in ('k', ''):
            for _ in range(count):
                if cursor.blockNumber() == 0:
                    if mode == QTextCursor.MoveAnchor:
                        self._vim_dispatch_action('prev_element')
                    break
                cursor.movePosition(QTextCursor.Up, mode)
            return True
        if key == Qt.Key_W and text == 'w':
            cursor.movePosition(QTextCursor.NextWord, mode, count)
            return True
        if key == Qt.Key_B and text == 'b':
            cursor.movePosition(QTextCursor.PreviousWord, mode, count)
            return True
        if key == Qt.Key_E and text == 'e':
            # EndOfWord alone correctly reaches the end of the word the cursor is already inside
            # (including sitting on its first character) — NextWord is only needed when the cursor
            # is ALREADY at a word's end (or on whitespace with nothing after it on this line),
            # otherwise unconditionally skipping to the next word first would jump clean over the
            # current one, e.g. landing on "bar"'s end instead of "foo"'s when starting on "foo"
            for _ in range(count):
                before = cursor.position()
                cursor.movePosition(QTextCursor.EndOfWord, mode)
                if cursor.position() == before:
                    cursor.movePosition(QTextCursor.NextWord, mode)
                    cursor.movePosition(QTextCursor.EndOfWord, mode)
            return True
        if key == Qt.Key_0 and text == '0':
            cursor.movePosition(QTextCursor.StartOfLine, mode)
            return True
        if key == Qt.Key_Dollar and text == '$':
            cursor.movePosition(QTextCursor.EndOfLine, mode)
            return True
        return False
    # [FN CLOSED] _vim_move

    # [FN CATEGORY] _vim_apply_operator — d/y/c followed by either a motion (charwise range) or a
    # repeat of the operator's own letter (dd/yy/cc, linewise — the whole line including its
    # newline, matching real vim). An unrecognized motion after an operator cancels silently, same
    # as real vim rather than falling through to inserting the motion key as text.
    # [FN] _vim_apply_operator — deletes/yanks/changes the text an operator+motion covers
    # [FN OPEN] _vim_apply_operator
    def _vim_apply_operator(self, operator, key, text, count):
        cursor = self.textCursor()
        is_linewise_repeat = (
            (operator == 'd' and text == 'd') or (operator == 'y' and text == 'y') or (operator == 'c' and text == 'c')
        )
        if is_linewise_repeat:
            cursor.movePosition(QTextCursor.StartOfLine)
            cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor, max(0, count - 1))
            cursor.movePosition(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)
            cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor)  # sweep up the newline too
            linewise = True
        else:
            # vim's own special case: "cw" changes only to the end of the current word (like "ce"),
            # not through the start of the next one like "dw"/"yw" would — otherwise the trailing
            # space before the next word gets eaten too, which is never what "change this word" means
            if operator == 'c' and key == Qt.Key_W and text == 'w' and not cursor.atBlockEnd():
                key, text = Qt.Key_E, 'e'
            moved = self._vim_move(cursor, key, text, count, QTextCursor.KeepAnchor)
            if not moved:
                return  # unrecognized motion: cancel, like real vim
            linewise = False
        if not cursor.hasSelection():
            return
        _VIM_REGISTER['text'] = cursor.selectedText().replace(' ', '\n')
        _VIM_REGISTER['linewise'] = linewise
        if operator in ('d', 'c'):
            cursor.removeSelectedText()
            self.setTextCursor(cursor)
        if operator == 'c':
            self._vim_enter_insert()
    # [FN CLOSED] _vim_apply_operator

    # [FN] _vim_apply_visual_operator — d/x/y/c on the current visual selection, then back to Normal
    # (or Insert, for c) — same register/linewise bookkeeping as _vim_apply_operator
    # [FN OPEN] _vim_apply_visual_operator
    def _vim_apply_visual_operator(self, operator):
        cursor = self.textCursor()
        linewise = self.vim_state == 'visual_line'
        if linewise and cursor.hasSelection():
            start, end = sorted((cursor.selectionStart(), cursor.selectionEnd()))
            cursor.setPosition(start)
            cursor.movePosition(QTextCursor.StartOfLine)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            cursor.movePosition(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)
            cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor)
        self._vim_enter_normal()
        self._vim_visual_anchor = None
        if not cursor.hasSelection():
            return
        _VIM_REGISTER['text'] = cursor.selectedText().replace(' ', '\n')
        _VIM_REGISTER['linewise'] = linewise
        if operator in ('d', 'c'):
            cursor.removeSelectedText()
            self.setTextCursor(cursor)
        if operator == 'c':
            self._vim_enter_insert()
    # [FN CLOSED] _vim_apply_visual_operator

    # [FN CATEGORY] _vim_simple_command — every Normal-mode key that isn't a motion, an operator, or
    # a g/z prefix: mode switches (i/a/I/A/o/O/v/V), x/paste/undo, and the callback-routed actions
    # (search, :, redo — redo only fires here since Ctrl+R is only intercepted in Normal mode, so
    # the app-wide Ctrl+R=Run shortcut is untouched whenever a code block isn't focused in vim mode).
    # [FN] _vim_simple_command — handles one non-motion, non-operator Normal-mode key
    # [FN OPEN] _vim_simple_command
    def _vim_simple_command(self, key, text, count):
        cursor = self.textCursor()
        if key == Qt.Key_R and (QApplication.keyboardModifiers() & Qt.ControlModifier):
            self._vim_dispatch_action('redo')
            return True
        if text == 'x':
            for _ in range(count):
                cursor.deleteChar()
            self.setTextCursor(cursor)
            return True
        if text == 'X':
            for _ in range(count):
                cursor.deletePreviousChar()
            self.setTextCursor(cursor)
            return True
        if text == 'p':
            self._vim_paste(after=True)
            return True
        if text == 'P':
            self._vim_paste(after=False)
            return True
        if key == Qt.Key_U and text == 'u':
            self._vim_dispatch_action('undo')
            return True
        if text == 'i':
            self._vim_enter_insert()
            return True
        if text == 'a':
            cursor.movePosition(QTextCursor.Right)
            self.setTextCursor(cursor)
            self._vim_enter_insert()
            return True
        if text == 'I':
            cursor.movePosition(QTextCursor.StartOfLine)
            self.setTextCursor(cursor)
            self._vim_enter_insert()
            return True
        if text == 'A':
            cursor.movePosition(QTextCursor.EndOfLine)
            self.setTextCursor(cursor)
            self._vim_enter_insert()
            return True
        if text == 'o':
            cursor.movePosition(QTextCursor.EndOfLine)
            cursor.insertText('\n')
            self.setTextCursor(cursor)
            self._vim_enter_insert()
            return True
        if text == 'O':
            cursor.movePosition(QTextCursor.StartOfLine)
            cursor.insertText('\n')
            cursor.movePosition(QTextCursor.Up)
            self.setTextCursor(cursor)
            self._vim_enter_insert()
            return True
        if text == 'v':
            self._vim_enter_visual('visual')
            return True
        if text == 'V':
            self._vim_enter_visual('visual_line')
            return True
        if text == '/':
            self._vim_dispatch_action('search')
            return True
        if key == Qt.Key_N and text == 'n':
            self._vim_dispatch_action('find_next')
            return True
        if text == 'N':
            self._vim_dispatch_action('find_prev')
            return True
        if text == ':':
            self._vim_dispatch_action('open_command_bar')
            return True
        if text == 'g':
            self._vim_pending_prefix = 'g'
            return True
        if key == Qt.Key_G and text == 'G':
            self._vim_dispatch_action('last_element')
            return True
        if text == 'z':
            self._vim_pending_prefix = 'z'
            return True
        return False
    # [FN CLOSED] _vim_simple_command

    def _vim_paste(self, after):
        if not _VIM_REGISTER['text']:
            return
        cursor = self.textCursor()
        if _VIM_REGISTER['linewise']:
            cursor.movePosition(QTextCursor.EndOfLine if after else QTextCursor.StartOfLine)
            cursor.insertText(('\n' if after else '') + _VIM_REGISTER['text'].rstrip('\n') + ('\n' if not after else ''))
            if after:
                cursor.movePosition(QTextCursor.NextBlock)
            else:
                cursor.movePosition(QTextCursor.Up, QTextCursor.MoveAnchor, _VIM_REGISTER['text'].count('\n') + 1)
        else:
            if after and not cursor.atBlockEnd():
                cursor.movePosition(QTextCursor.Right)
            cursor.insertText(_VIM_REGISTER['text'])
        self.setTextCursor(cursor)

    def _vim_enter_insert(self):
        self.vim_state = 'insert'
        self.vim_mode_changed.emit(self.vim_state)

    def _vim_enter_normal(self, move_left=False):
        self.vim_state = 'normal'
        if move_left:
            cursor = self.textCursor()
            if not cursor.atBlockStart():
                cursor.movePosition(QTextCursor.Left)
                self.setTextCursor(cursor)
        self.vim_mode_changed.emit(self.vim_state)

    def _vim_enter_visual(self, state):
        self.vim_state = state
        self._vim_visual_anchor = self.textCursor().position()
        self.vim_mode_changed.emit(self.vim_state)

    # [FN] mousePressEvent — Ctrl+Click jumps to the clicked symbol's definition, same gesture
    # every other IDE uses; the normal click still runs first so the text cursor lands at the
    # click position before definition_provider looks up what's there
    # [FN OPEN] mousePressEvent
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if (
            event.button() == Qt.LeftButton
            and event.modifiers() & Qt.ControlModifier
            and self.definition_provider is not None
        ):
            self.definition_provider(self)
    # [FN CLOSED] mousePressEvent

    def _on_text_changed_for_completion(self):
        if self.completion_provider is not None and self.hasFocus():
            self._completion_timer.start(200)

    def _trigger_completion(self):
        if self.completion_provider is not None and self.hasFocus():
            self.completion_provider(self)

    def _text_under_cursor(self):
        cursor = self.textCursor()
        cursor.select(QTextCursor.WordUnderCursor)
        return cursor.selectedText()

    # [FN] show_completions — populates and pops up the completer with fresh candidates, called
    # back by mainwindow once its async completion request (LSP or local) resolves
    # [FN OPEN] show_completions
    def show_completions(self, candidates):
        if not candidates or not self.hasFocus():
            self._completer.popup().hide()
            return
        prefix = self._text_under_cursor()
        self._completer.setModel(QStringListModel(candidates, self._completer))
        self._completer.setCompletionPrefix(prefix)
        if self._completer.completionCount() == 0:
            self._completer.popup().hide()
            return
        self._completer.popup().setCurrentIndex(self._completer.completionModel().index(0, 0))
        cr = self.cursorRect()
        cr.setWidth(
            self._completer.popup().sizeHintForColumn(0)
            + self._completer.popup().verticalScrollBar().sizeHint().width()
        )
        self._completer.complete(cr)
    # [FN CLOSED] show_completions

    def _insert_completion(self, completion):
        cursor = self.textCursor()
        extra = len(completion) - len(self._completer.completionPrefix())
        cursor.movePosition(QTextCursor.Left)
        cursor.movePosition(QTextCursor.EndOfWord)
        cursor.insertText(completion[-extra:] if extra > 0 else '')
        self.setTextCursor(cursor)

    # [FN CATEGORY] quick-doc-on-hover — mirrors PyCharm/VS Code: resting the mouse over a symbol
    # (no click needed) shows its documentation as a tooltip. mouseMoveEvent just restarts a
    # debounce timer with the latest position; the actual lookup happens in _trigger_hover so a
    # fast-moving mouse doesn't fire a request per pixel.
    # [FN] mouseMoveEvent — restarts the hover-lookup debounce on mouse movement
    # [FN OPEN] mouseMoveEvent
    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        hide_code_hover_popup()
        self._hover_pos = event.position().toPoint()
        self._hover_timer.start(450)
    # [FN CLOSED] mouseMoveEvent

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._hover_timer.stop()
        self._hover_pos = None
        hide_code_hover_popup()

    def _trigger_hover(self):
        if self.hover_provider is None or self._hover_pos is None:
            return
        cursor = self.cursorForPosition(self._hover_pos)
        global_pos = self.viewport().mapToGlobal(self._hover_pos)
        self.hover_provider(self, cursor, global_pos)

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
    def run_debug_python(self, path, breakpoint_lines, cwd=None, python_exe=None):
        if self.process is not None:
            self._append('\n# terminal busy: stop the running command first\n')
            return False
        if cwd:
            self.cwd = cwd
        python_exe = python_exe or sys.executable
        if len(self.toPlainText()) > self.prompt_start:
            self._append('\n')
        self._append(f'{python_exe} -m pdb {path}\n')
        self.process = QProcess(self)
        self.process.setWorkingDirectory(self.cwd)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._error)
        self.process.finished.connect(self._finished)
        self.process.start(python_exe, ['-m', 'pdb', path])
        self.prompt_start = len(self.toPlainText())
        for line_no in sorted(breakpoint_lines):
            self.process.write(f'break {path}:{line_no}\n'.encode(self.encoding))
        self.process.write(b'continue\n')
        return True
    # [FN CLOSED] run_debug_python

    # [FN CATEGORY] run_python_repl — starts a plain interactive Python process (-i keeps it
    # interactive even with no real tty attached, -u unbuffers stdout so output streams as it's
    # produced instead of only flushing at exit) — the same stdin-forwarding keyPressEvent already
    # uses for pdb makes this pane a working REPL with no extra input handling of its own.
    # [FN] run_python_repl — starts an interactive `python -i` session in this pane
    # [FN OPEN] run_python_repl
    def run_python_repl(self, python_exe=None):
        if self.process is not None:
            return False
        python_exe = python_exe or sys.executable
        self._append(f'{python_exe} -i\n')
        self.process = QProcess(self)
        self.process.setWorkingDirectory(self.cwd)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._error)
        self.process.finished.connect(self._finished)
        self.process.start(python_exe, ['-i', '-u'])
        self.prompt_start = len(self.toPlainText())
        return True
    # [FN CLOSED] run_python_repl

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
    fd, path = safe_mkstemp(prefix='.kant-ai-system-', suffix='.md', dir=directory)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return path


# [CST] _IMAGE_ATTACHMENT_EXTENSIONS — formats compress_attached_image actually knows how to open;
# svg is deliberately excluded (QImage rasterizes it, losing exactly the vector precision a diagram
# attachment needs) and is sent through unchanged regardless of the lossy toggle.
_IMAGE_ATTACHMENT_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}


def _attachment_temp_prefix(kind, original_path):
    # keeps the original filename recognizable in the chip label (os.path.basename of the
    # resolved temp path) instead of a bare random suffix — a user who attached "spec.pdf"
    # shouldn't see ".kant-attach-doc-a1b2c3.md" with no trace of what it came from
    stem = re.sub(r'[^\w.-]', '_', Path(original_path).stem)[:40]
    return f'.kant-attach-{kind}-{stem}-'


# [FN CATEGORY] compress_attached_image — the "risparmio token ma lossy" toggle's actual effect:
# downscales to a max dimension and re-encodes as JPEG at reduced quality using QImage (already a
# hard dependency via PySide6 — no new package needed for this), so a large screenshot or photo
# reads as far fewer tokens when the model's own file-reading tool opens it. Returns the ORIGINAL
# path unchanged on anything that isn't a recognized raster format or fails to load — a failed
# compression attempt must never silently drop the attachment.
# [FN] compress_attached_image — lossily downscale/recompress an attached image, or pass it through
# [FN OPEN] compress_attached_image
def compress_attached_image(path, max_dimension=1280, quality=60):
    if Path(path).suffix.lower() not in _IMAGE_ATTACHMENT_EXTENSIONS:
        return path
    image = QImage(path)
    if image.isNull():
        return path
    if image.width() > max_dimension or image.height() > max_dimension:
        image = image.scaled(max_dimension, max_dimension, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    try:
        fd, out_path = safe_mkstemp(prefix=_attachment_temp_prefix('img', path), suffix='.jpg')
        os.close(fd)
        if not image.save(out_path, 'JPEG', quality):
            return path
    except OSError:
        return path
    return out_path
# [FN CLOSED] compress_attached_image


# [CST] _DOCUMENT_ATTACHMENT_EXTENSIONS — formats worth routing through MarkItDown at all; plain
# text-ish formats (.txt/.md/.csv/.json/...) are already lean and go through unchanged — converting
# them would just be a no-op wrapped in a slower, more fallible code path.
_DOCUMENT_ATTACHMENT_EXTENSIONS = {'.pdf', '.docx', '.pptx', '.xlsx', '.doc', '.ppt', '.xls', '.html', '.htm'}


# [FN CATEGORY] convert_attached_document — a raw PDF/DOCX/PPTX/XLSX read by the model's own file
# tool costs far more tokens than the same content as clean Markdown (binary structure, embedded
# XML, formatting noise); MarkItDown (microsoft/markitdown) extracts just the text/structure. Two
# distinct "give up and keep the original" paths, both correct per the ask: the package not being
# installed (an optional dependency — the editor works without it, same as an optional language
# server), and a document with no extractable text (a scanned/image-only PDF) — MarkItDown would
# return empty/near-empty content for the latter, which is worse than just attaching the original.
# [FN] convert_attached_document — MarkItDown-convert a document attachment, or pass it through
# [FN OPEN] convert_attached_document
def convert_attached_document(path):
    if Path(path).suffix.lower() not in _DOCUMENT_ATTACHMENT_EXTENSIONS:
        return path
    try:
        from markitdown import MarkItDown
    except ImportError:
        return path
    try:
        text = (MarkItDown().convert(path).text_content or '').strip()
    except Exception:
        return path
    if not text:
        return path  # no text detected — keep the original as-is, per the ask
    try:
        fd, out_path = safe_mkstemp(prefix=_attachment_temp_prefix('doc', path), suffix='.md')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
    except OSError:
        return path
    return out_path
# [FN CLOSED] convert_attached_document


def _agent_executable(agent):
    return 'codex' if agent == 'codex' else 'claude'


# [FN] _agent_command — builds the argv for launching one prompt, per-provider. Effort is a real
# parameter for both CLIs, just under different mechanisms: claude has a direct `--effort` flag
# (low/medium/high/xhigh/max, per `claude --help`); codex has no dedicated flag but honors the
# `model_reasoning_effort` config key via its generic `-c key=value` override. session_args (see
# run_prompt) thread through a fresh-session or resume-session marker, per-provider shape decided
# by the caller — this function just splices it into the right spot in the argv.
def _agent_command(agent, prompt, auto_permissions=False, model=None, effort=None, session_args=()):
    model_args = ('--model', model) if model else ()
    if agent == 'codex':
        effort_args = ('-c', f'model_reasoning_effort="{effort}"') if effort else ()
        permission_args = ('--sandbox', 'workspace-write', '--ask-for-approval', 'never') if auto_permissions else ()
        return 'codex', [
            'exec', *session_args, *permission_args,
            *model_args, *effort_args, prompt,
        ]
    effort_args = ('--effort', effort) if effort else ()
    return 'claude', [*session_args, *model_args, *effort_args, '-p', prompt]


def _agent_label(agent):
    return 'Codex' if agent == 'codex' else 'Claude Code'


# [CST] _ANSI_ESCAPE_RE — matches CSI (color/cursor, "ESC[...letter"), OSC ("ESC]...BEL/ST"), and
# other single-character ESC sequences — the claude/codex CLIs colorize their own stdout, and
# those raw bytes rendered as plain text look like garbage glyphs instead of being invisible.
_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-_]')


# [FN CATEGORY] _normalize_ai_text — the claude/codex CLIs are UTF-8 regardless of the OS locale
# (unlike TerminalPane's plain shell, where the locale codepage is the actually-correct encoding
# for whatever's running in it), so decoding their output with locale.getpreferredencoding() — a
# Windows ANSI codepage, not UTF-8 — corrupts any accented letter, emoji, or box-drawing glyph.
# Also drops ANSI color/cursor codes and collapses bare \r (progress-bar/spinner overwrites, not
# real content) into nothing.
# [FN] _normalize_ai_text — decodes AI-CLI stdout/stderr bytes as UTF-8 and strips ANSI/control noise
# [FN OPEN] _normalize_ai_text
def _normalize_ai_text(raw_bytes):
    text = bytes(raw_bytes).decode('utf-8', errors='replace')
    text = _ANSI_ESCAPE_RE.sub('', text)
    return text.replace('\r\n', '\n').replace('\r', '')
# [FN CLOSED] _normalize_ai_text


# [CST] markdown regexes for _markdown_to_html — fenced blocks extracted first (DOTALL, so their
# contents never get bold/italic/inline-code substitution applied inside them); bold before italic
# so **x** doesn't leave stray single *s for the italic pattern to also match.
_MD_CODE_FENCE_RE = re.compile(r'```(?:\w+)?\n?(.*?)```', re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r'`([^`\n]+)`')
_MD_BOLD_RE = re.compile(r'\*\*(.+?)\*\*|(?<![\w_])__(.+?)__(?![\w_])')
# underscore emphasis is intraword-blind per CommonMark: bare `*` counts anywhere, but `_` only
# starts/ends emphasis when NOT flanked by a word character on that side — otherwise identifiers
# like add_item or cart_id (extremely common in any code-related chat message) get mangled into
# "add<i>item(cart</i>id...)" by treating their own underscores as italic delimiters
_MD_ITALIC_RE = re.compile(r'(?<!\*)\*([^*\n]+)\*(?!\*)|(?<![\w_])_([^_\n]+)_(?![\w_])')
# a table row/separator always has outer pipes here — the far more common shape an AI response or
# a pasted table actually uses; the no-outer-pipe GFM variant ("A | B" / "---|---") is not handled
_MD_TABLE_ROW_RE = re.compile(r'^\s*\|(.+)\|\s*$')
_MD_TABLE_SEP_CELL_RE = re.compile(r'^:?-+:?$')


def _split_table_row(line):
    return [cell.strip() for cell in line.strip()[1:-1].split('|')]


def _is_table_separator_row(line):
    match = _MD_TABLE_ROW_RE.match(line)
    if not match:
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(_MD_TABLE_SEP_CELL_RE.match(cell) for cell in cells)


def _render_table_html(header_cells, body_rows):
    cell_style = f'border:1px solid {theme.BORDER}; padding:3px 9px; text-align:left;'
    head = ''.join(f'<th style="{cell_style}">{cell}</th>' for cell in header_cells)
    body = ''.join(
        '<tr>' + ''.join(f'<td style="{cell_style}">{cell}</td>' for cell in row) + '</tr>'
        for row in body_rows
    )
    return f'<table style="border-collapse:collapse; margin:4px 0;"><tr>{head}</tr>{body}</table>'


# [FN CATEGORY] _markdown_to_html — not a full CommonMark implementation, just the subset an AI
# chat response actually uses (bold, italic, inline code, fenced code blocks, simple bullet
# lines), hand-rolled to avoid a new dependency for a narrow, controlled need — the input is
# always this app's own AI process output or the user's own typed message, not arbitrary external
# HTML. Escapes first, substitutes markdown syntax on the escaped text after (the syntax
# characters `*`/`` ` `` aren't touched by HTML-escaping), so raw `<`/`&` in prose or inside code
# render literally instead of being parsed as markup.
# [FN] _markdown_to_html — converts a chat message's markdown-ish text into Qt rich text
# [FN OPEN] _markdown_to_html
def _markdown_to_html(text):
    segments = []
    last_end = 0
    for match in _MD_CODE_FENCE_RE.finditer(text):
        segments.append(('text', text[last_end:match.start()]))
        segments.append(('code', match.group(1)))
        last_end = match.end()
    segments.append(('text', text[last_end:]))

    html_parts = []
    for kind, segment in segments:
        if kind == 'code':
            code_html = html_escape(segment.strip('\n')).replace('\n', '<br>')
            html_parts.append(
                f'<pre style="background:{theme.CODE_BG}; border:1px solid {theme.BORDER}; '
                f'border-radius:6px; padding:8px; margin:4px 0;">{code_html}</pre>'
            )
            continue
        escaped = html_escape(segment)
        # inline code is stashed behind a placeholder before bold/italic run, and only restored
        # after — otherwise markdown syntax INSIDE a `code span` (e.g. `*args`) gets re-processed
        # as italic by the later substitution instead of rendering literally
        code_spans = []

        def stash_code(m):
            code_spans.append(m.group(1))
            return f'\x00{len(code_spans) - 1}\x00'

        escaped = _MD_INLINE_CODE_RE.sub(stash_code, escaped)
        escaped = _MD_BOLD_RE.sub(lambda m: f'<b>{m.group(1) or m.group(2)}</b>', escaped)
        escaped = _MD_ITALIC_RE.sub(lambda m: f'<i>{m.group(1) or m.group(2)}</i>', escaped)
        escaped = re.sub(
            r'\x00(\d+)\x00',
            lambda m: f'<code style="background:{theme.CODE_BG}; padding:1px 4px; border-radius:3px;">{code_spans[int(m.group(1))]}</code>',
            escaped,
        )
        rendered_lines = []
        lines = escaped.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            # a table is a header row immediately followed by a |---|---| separator row (bold/
            # italic/inline-code already ran above, so cells get that formatting for free); consumes
            # every following row that still looks like a table row, not just the header+separator
            if i + 1 < len(lines) and _MD_TABLE_ROW_RE.match(line) and _is_table_separator_row(lines[i + 1]):
                header_cells = _split_table_row(line)
                i += 2
                body_rows = []
                while i < len(lines) and _MD_TABLE_ROW_RE.match(lines[i]):
                    body_rows.append(_split_table_row(lines[i]))
                    i += 1
                rendered_lines.append(_render_table_html(header_cells, body_rows))
                continue
            stripped = line.lstrip()
            if stripped[:2] in ('- ', '* '):
                rendered_lines.append('&nbsp;&nbsp;• ' + stripped[2:])
            else:
                rendered_lines.append(line)
            i += 1
        html_parts.append('<br>'.join(rendered_lines))
    return ''.join(html_parts)
# [FN CLOSED] _markdown_to_html


# [CST] _CODE_PERMISSION_FIELDS — tool-call argument keys, across Claude Code's built-in Edit/
# Write/Bash/NotebookEdit tools, whose value is source code or a shell command rather than a short
# scalar — these get the same <pre> code-block treatment as a fenced markdown block, everything
# else in the payload (file_path, replace_all, description, ...) stays a plain "key: value" line.
_CODE_PERMISSION_FIELDS = {'old_string', 'new_string', 'content', 'command', 'new_source', 'old_str', 'new_str'}
_PERMISSION_FIELD_MAX_CHARS = 2000


# [FN CATEGORY] _format_permission_input_html — the raw tool_input dict was previously shown via
# json.dumps(..., indent=2), which escapes every real newline in an Edit's old_string/new_string as
# a literal two-character "\n" — unreadable for anything but a one-liner. This renders known
# code-bearing fields as an actual multi-line, monospaced block (reusing _markdown_to_html's own
# <pre> styling) and leaves short scalar fields as plain text, so a permission card reads like a
# diff/command instead of a JSON dump.
# [FN] _format_permission_input_html — tool_input dict -> Qt rich text for the permission card
# [FN OPEN] _format_permission_input_html
def _format_permission_input_html(tool_input):
    if not isinstance(tool_input, dict):
        return html_escape(str(tool_input))
    parts = []
    for key, value in tool_input.items():
        if isinstance(value, str) and (key in _CODE_PERMISSION_FIELDS or '\n' in value):
            truncated = value[:_PERMISSION_FIELD_MAX_CHARS]
            suffix = '\n…' if len(value) > _PERMISSION_FIELD_MAX_CHARS else ''
            code_html = html_escape(truncated + suffix).replace('\n', '<br>')
            parts.append(
                f'<div><b>{html_escape(key)}:</b></div>'
                f'<pre style="background:{theme.CODE_BG}; border:1px solid {theme.BORDER}; '
                f'border-radius:6px; padding:8px; margin:2px 0 8px 0; '
                f'font-family:Consolas;">{code_html}</pre>'
            )
        else:
            parts.append(f'<div><b>{html_escape(str(key))}:</b> {html_escape(str(value))}</div>')
    return ''.join(parts)
# [FN CLOSED] _format_permission_input_html


# [FN CATEGORY] _CodeHoverPopup — quick-doc-on-hover (CodeEdit.hover_provider) used to show its text
# via QToolTip, the OS's own native tooltip — unstyled, unthemeable, visibly foreign next to every
# other panel/popup in this IDE (all of which share theme.PANEL/BORDER, see e.g. mappa.py's
# EdgeFlowPopup). This is a themed replacement: same rounded-panel look as the rest of the app, and
# renders the LSP hover response as real markdown (_markdown_to_html) instead of raw text with
# literal "**"/backtick syntax showing through.
# [FN] _CodeHoverPopup — a themed floating popup for code-element documentation-on-hover
# [FN OPEN] _CodeHoverPopup
class _CodeHoverPopup(QFrame):
    def __init__(self):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setObjectName('codeHoverPopup')
        self.setAttribute(Qt.WA_StyledBackground, True)
        # never steals a click/focus from the editor underneath — same reasoning a native tooltip
        # already gets "for free" from the OS, which a custom top-level widget doesn't by default
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)
        # a soft drop shadow lifts this off the code underneath instead of reading as pasted flat
        # onto it — the one cue a native OS tooltip gets for free that this custom widget doesn't
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(32)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 100))
        self.setGraphicsEffect(shadow)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # a thin accent-colored top strip gives this an "info card" identity of its own at a
        # glance, instead of being visually indistinguishable from every other bordered panel
        self.accent_bar = QFrame()
        self.accent_bar.setFixedHeight(3)
        outer.addWidget(self.accent_bar)
        body = QVBoxLayout()
        body.setContentsMargins(14, 10, 14, 12)
        self.label = QLabel()
        self.label.setTextFormat(Qt.RichText)
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(480)
        body.addWidget(self.label)
        outer.addLayout(body)
        self.apply_style()
        self.hide()

    def apply_style(self):
        self.setStyleSheet(
            f'#codeHoverPopup {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; '
            f'border-top:none; border-radius:11px; }}'
        )
        self.accent_bar.setStyleSheet(
            f'background:{theme.ACCENT}; border-top-left-radius:11px; border-top-right-radius:11px;'
        )
        self.label.setStyleSheet(f'color:{theme.TEXT}; border:none; font-size:{theme.CODE_FONT_PT}pt;')

    def show_at(self, global_pos, html_text):
        self.apply_style()
        self.label.setText(html_text)
        self.adjustSize()
        self.move(global_pos + QPoint(14, 18))
        self.show()
        self.raise_()
# [FN CLOSED] _CodeHoverPopup


# [CST] _code_hover_popup_instance — one shared popup for the whole app (only one hover can ever be
# showing at a time), built lazily since it needs a live QApplication before it can construct
_code_hover_popup_instance = [None]


def show_code_hover_popup(global_pos, html_text):
    if _code_hover_popup_instance[0] is None:
        _code_hover_popup_instance[0] = _CodeHoverPopup()
    _code_hover_popup_instance[0].show_at(global_pos, html_text)


def hide_code_hover_popup():
    if _code_hover_popup_instance[0] is not None:
        _code_hover_popup_instance[0].hide()


# [CST] _TYPING_FRAMES — cycled in the placeholder assistant bubble while a prompt is running and
# no output has streamed back yet, so the chat shows the AI is working instead of sitting blank
_TYPING_FRAMES = ('·', '· ·', '· · ·')


# [CST] MODEL_DEFAULT — sentinel meaning "no --model flag, let the CLI pick its own default"
MODEL_DEFAULT = '(predefinito)'

# [CST] CLAUDE_MODELS — current Claude model IDs accepted by `claude -p --model`, per Anthropic's
# own model catalog (Fable 5, Opus 4.8, Sonnet 5, Haiku 4.5, and the immediately preceding Opus/
# Sonnet releases). Keep this compact preset list aligned with the supported CLI catalog.
CLAUDE_MODELS = (
    MODEL_DEFAULT, 'claude-opus-4-8', 'claude-sonnet-5', 'claude-haiku-4-5',
    'claude-fable-5', 'claude-opus-4-7', 'claude-sonnet-4-6',
)
# Current recommended Codex models; keep this compact preset list aligned with the CLI catalog.
CODEX_MODELS = (
    MODEL_DEFAULT, 'gpt-5.6', 'gpt-5.4', 'gpt-5.6-terra', 'gpt-5.3-codex-spark',
)

# [CST] EFFORT_LEVELS — a real flag for claude (--effort), a config override for codex
# (model_reasoning_effort) — both applied by _agent_command. Availability depends on the selected
# model and is ultimately validated by the installed CLI.
EFFORT_LEVELS = {
    'claude': (MODEL_DEFAULT, 'low', 'medium', 'high', 'xhigh', 'max', 'ultracode'),
    'codex': (MODEL_DEFAULT, 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
}


# [FN CATEGORY] _PromptEdit — QPlainTextEdit's own default (Return inserts a newline) is the
# opposite of a chat box's usual convention, so Return here emits send_requested instead. Ctrl+
# Return inserts the newline explicitly rather than falling through to the default handler —
# QPlainTextEdit's own Return handling isn't bound to Ctrl+Return at all (only bare Return), so
# super().keyPressEvent(event) here would silently do nothing instead of inserting anything.
# [FN] _PromptEdit — Return sends, Ctrl+Return inserts a newline
# [FN OPEN] _PromptEdit
class _PromptEdit(QPlainTextEdit):
    send_requested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ControlModifier:
                self.insertPlainText('\n')
            else:
                self.send_requested.emit()
            return
        super().keyPressEvent(event)
# [FN CLOSED] _PromptEdit


# [FN CATEGORY] _ScanlineWidget — a very faint repeating horizontal-line texture painted over the
# widget's own background, evoking old CRT scanlines. Direct QPainter drawing rather than a QSS
# background-image: PySide6's QSS engine doesn't alpha-composite a semi-transparent background-image
# over a background-color at all (confirmed empirically — a fully opaque tile renders, anything with
# transparency silently doesn't), so a real paintEvent is the only reliable way to get a subtle
# overlay instead of an all-or-nothing one. Day mode only; a CRT glow has no business in a dark room.
# [FN] _ScanlineWidget — QWidget subclass that paints faint scanlines after its normal contents
# [FN OPEN] _ScanlineWidget
class _ScanlineWidget(QWidget):
    def paintEvent(self, event):
        super().paintEvent(event)
        if theme.NIGHT:
            return
        painter = QPainter(self)
        painter.setPen(QColor(0, 0, 0, 12))
        for y in range(0, self.height(), 2):
            painter.drawLine(0, y, self.width(), y)
# [FN CLOSED] _ScanlineWidget


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
        self.focus_hint = None  # callable returning a short human-readable focus summary, or None
        self._pending_attachments = []  # [(original_path, resolved_path), ...] from _attach_files, cleared on send
        # each run_prompt call is otherwise a brand-new, stateless claude/codex subprocess with no
        # memory of earlier turns — these track this pane's ongoing conversation per provider so a
        # later message can ask the CLI to resume it instead of starting fresh every time. Reset in
        # set_cwd (a different project is a different conversation), not on Stop (interrupting the
        # current turn doesn't erase what was already said).
        self._claude_session_id = None
        self._codex_resumable = False
        self._messages = []
        self._stream_label = None
        self._stream_text = ''
        self._typing_timer = QTimer(self)
        self._typing_timer.timeout.connect(self._typing_tick)
        self._typing_frame = 0
        # a long streamed response used to re-render the ENTIRE accumulated text through
        # _markdown_to_html on every single stdout chunk — O(n) work per chunk, O(n^2) over a full
        # response, and QProcess often delivers a fast burst of tiny reads for one logical write.
        # This throttles actual render+scroll passes to ~25/s (still reads as real-time streaming)
        # instead of one per chunk, without touching the rendering logic itself.
        self._stream_render_timer = QTimer(self)
        self._stream_render_timer.setSingleShot(True)
        self._stream_render_timer.timeout.connect(self._flush_stream_render)
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
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        self.title = QLabel('CHAT AI')
        self.title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        title_row.addWidget(self.title)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        # agent/model/effort/auto-permissions grouped as one visually distinct "controls chip"
        # (own rounded background, own row) instead of loose combo boxes floating in the title row
        self.controls_bar = QWidget()
        self.controls_bar.setObjectName('claudeControlsBar')
        header = QHBoxLayout(self.controls_bar)
        header.setContentsMargins(8, 6, 8, 6)
        header.setSpacing(6)
        self.agent_select = QComboBox()
        self.agent_select.addItem('Claude Code', 'claude')
        self.agent_select.addItem('Codex', 'codex')
        self.agent_select.setToolTip('Quale CLI AI usare per i messaggi inviati da questa plancia')
        header.addWidget(self.agent_select)
        self.model_select = QComboBox()
        self.model_select.setToolTip("Modello per l'agente selezionato")
        self.model_select.addItems(CLAUDE_MODELS)
        self.model_select.setCursor(Qt.PointingHandCursor)
        self.model_select.setIconSize(QSize(18, 18))
        self.model_select.setFixedWidth(44)
        header.addWidget(self.model_select)
        self.effort_select = QComboBox()
        self.effort_select.setToolTip("Reasoning effort per l'agente selezionato")
        self.effort_select.addItems(EFFORT_LEVELS['claude'])
        self.effort_select.setCursor(Qt.PointingHandCursor)
        self.effort_select.setIconSize(QSize(18, 18))
        self.effort_select.setFixedWidth(44)
        header.addWidget(self.effort_select)
        header.addStretch(1)
        self.auto_permissions = QCheckBox('Automatico')
        self.auto_permissions.setToolTip('Approva i permessi Claude; le modifiche restano soggette alla revisione finale.')
        self.auto_permissions.toggled.connect(self._automatic_permissions_changed)
        header.addWidget(self.auto_permissions)
        # scopes every message: default is the coding panel's current file/element (context_hint,
        # a hidden system-prompt addition — see mainwindow.py's _build_ai_context_hint); GLOBAL
        # suppresses that scoping so the AI considers the whole project instead. Lives here (not the
        # KANT-tree view-mode bar it used to share a row with) since it's an AI-chat-scoped control,
        # not a project-tree display mode.
        self.global_mode_btn = QPushButton(' GLOBAL')
        self.global_mode_btn.setIcon(draw_icon('globe', 14))
        self.global_mode_btn.setIconSize(QSize(14, 14))
        self.global_mode_btn.setCheckable(True)
        self.global_mode_btn.setToolTip(
            "Se disattivo (default), i messaggi in chat AI includono un riferimento nascosto al file/elemento "
            "attualmente aperto nella plancia di coding, cosi le modifiche restano mirate a quel punto. "
            "Attiva GLOBAL per far considerare all'AI l'intero progetto invece di un file/elemento specifico."
        )
        self.global_mode_btn.toggled.connect(lambda _checked: self.refresh_focus_label())
        header.addWidget(self.global_mode_btn)
        layout.addWidget(self.controls_bar)

        # a quiet line under the selector row surfacing what focus_hint() currently resolves to —
        # the implicit scoping (isolated element / whole file / whole project) is otherwise silent,
        # riding the hidden system-prompt channel with nothing to show for it in the chat UI itself
        self.focus_label = QLabel('')
        self.focus_label.setFont(QFont('Consolas', theme.CODE_FONT_PT - 1))
        self.focus_label.setContentsMargins(10, 0, 10, 0)
        layout.addWidget(self.focus_label)

        self.output = QScrollArea()
        self.output.setWidgetResizable(True)
        self.output.setFrameShape(QFrame.NoFrame)
        self.chat = _ScanlineWidget()
        self.chat_layout = QVBoxLayout(self.chat)
        self.chat_layout.setContentsMargins(6, 10, 6, 10)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch(1)
        self.output.setWidget(self.chat)
        layout.addWidget(self.output, 1)

        # "risparmio token ma lossy": when checked, an attached image (not documents — those go
        # through convert_attached_document unconditionally, which isn't lossy the same way) is
        # downscaled/recompressed by compress_attached_image before its path is queued. Always
        # visible next to the attach button, not just while something's already attached — it's a
        # setting for the NEXT attachment, not a property of the current chip row.
        self.lossy_images = QCheckBox('Immagini compresse')
        self.lossy_images.setToolTip(
            "Modalità risparmio token (lossy): le immagini allegate vengono ridimensionate e "
            "ricompresse prima dell'invio, per far leggere meno token al modello a scapito della "
            "qualità. I documenti (PDF, DOCX, ...) non sono affetti da questa opzione."
        )
        lossy_row = QHBoxLayout()
        lossy_row.setContentsMargins(0, 0, 0, 4)
        lossy_row.addWidget(self.lossy_images)
        lossy_row.addStretch(1)
        layout.addLayout(lossy_row)

        # attached-files chip row: only visible while at least one file is pending, cleared on send
        # (see _attach_files/_refresh_attachment_chips/_send) — one removable chip per file
        self.attachments_row = QWidget()
        self.attachments_layout = QHBoxLayout(self.attachments_row)
        self.attachments_layout.setContentsMargins(0, 0, 0, 4)
        self.attachments_layout.setSpacing(6)
        self.attachments_row.hide()
        layout.addWidget(self.attachments_row)

        self.prompt = _PromptEdit()
        self.prompt.setFixedHeight(90)
        self.prompt.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        self.prompt.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:8px; padding:8px;'
        )
        # Return sends; Ctrl+Return inserts a newline instead (see _PromptEdit)
        self.prompt.send_requested.connect(self._send)
        self.prompt.setPlaceholderText('Chiedi, modifica o analizza il codice… (Invio invia · Ctrl+Invio va a capo)')
        composer = QHBoxLayout()
        self.attach_btn = QPushButton('')
        self.attach_btn.setIcon(draw_icon('attach', 18))
        self.attach_btn.setIconSize(QSize(18, 18))
        self.attach_btn.setFixedSize(36, 42)
        self.attach_btn.setCursor(Qt.PointingHandCursor)
        self.attach_btn.setToolTip('Allega documenti o immagini da far leggere a Claude/Codex')
        self.attach_btn.clicked.connect(self._attach_files)
        composer.addWidget(self.attach_btn, 0, Qt.AlignBottom)
        composer.addWidget(self.prompt, 1)
        self.send_btn = QPushButton(' Invia')
        self.send_btn.setIcon(draw_icon('arrow-right', 14))
        self.send_btn.setIconSize(QSize(14, 14))
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setToolTip('Invia il messaggio (Invio); se un comando è in corso, lo interrompe')
        self.send_btn.clicked.connect(self._send)
        self.send_btn.setFixedHeight(42)
        composer.addWidget(self.send_btn, 0, Qt.AlignBottom)
        layout.addLayout(composer)

        self.agent_select.currentIndexChanged.connect(self._agent_changed)
        self.model_select.currentTextChanged.connect(lambda _text: self._refresh_selector_icons())
        self.effort_select.currentTextChanged.connect(lambda _text: self._refresh_selector_icons())
        if not shutil.which('claude') and shutil.which('codex'):
            self.set_agent('codex')
        else:
            self._agent_changed()
        self.apply_style()

    def apply_style(self):
        self.setStyleSheet(f'background:{theme.PANEL}; border-left:1px solid {theme.BORDER};')
        self.title.setStyleSheet(f'color:{theme.ACCENT}; letter-spacing:2px;')
        self.controls_bar.setStyleSheet(
            f'#claudeControlsBar {{ background:{theme.CODE_BG}; border:1px solid {theme.BORDER}; border-radius:9px; }}'
        )
        self.output.setStyleSheet(f'QScrollArea {{ background:{theme.CODE_BG}; border:none; border-radius:12px; }}')
        self.chat.setStyleSheet(f'background:{theme.CODE_BG};')
        self.prompt.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:8px; padding:8px;'
        )
        # solid pill background (not just a bare border) so each combo reads as its own control
        # against the controls bar's own CODE_BG fill, instead of blending flat into it
        combo_style = (
            f'QComboBox {{ background:{theme.PANEL}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:12px; padding:5px 10px; font-weight:600; }} '
            f'QComboBox:hover {{ border-color:{theme.ACCENT}; color:{theme.ACCENT}; }} '
            f'QComboBox:disabled {{ color:{theme.DIM}; border-color:#e2e8f0; background:#f1f5f9; }} '
            f'QComboBox::drop-down {{ border:none; width:16px; }}'
        )
        self.agent_select.setStyleSheet(combo_style)
        icon_combo_style = (
            f'QComboBox {{ background:{theme.PANEL}; color:transparent; border:1px solid {theme.BORDER}; '
            f'border-radius:8px; padding:4px 15px 4px 5px; }} '
            f'QComboBox:hover {{ border-color:{theme.ACCENT}; }} '
            f'QComboBox::drop-down {{ border:none; width:14px; }} '
            f'QComboBox QAbstractItemView {{ background:{theme.PANEL}; color:{theme.TEXT}; '
            f'border:1px solid {theme.BORDER}; selection-background-color:{theme.CODE_BG}; }}'
        )
        self.model_select.setStyleSheet(icon_combo_style)
        self.effort_select.setStyleSheet(icon_combo_style)
        self.auto_permissions.setStyleSheet(f'color:{theme.ACCENT}; font-weight:600; spacing:6px;')
        self.global_mode_btn.setStyleSheet(
            theme.BUTTON_STYLE + f'QPushButton:checked {{ background:{theme.ACCENT}; color:#ffffff; border-color:{theme.ACCENT}; }}'
        )
        self.focus_label.setStyleSheet(f'color:{theme.DIM};')
        self.lossy_images.setStyleSheet(f'color:{theme.DIM}; font-weight:600; spacing:6px;')
        self.attach_btn.setStyleSheet(
            f'QPushButton {{ background:{theme.CODE_BG}; color:{theme.DIM}; border:1px solid {theme.BORDER}; '
            f'border-radius:8px; font-size:15pt; }} '
            f'QPushButton:hover {{ color:{theme.ACCENT}; border-color:{theme.ACCENT}; }}'
        )
        self._refresh_attachment_chips()
        self.global_mode_btn.setIcon(draw_icon('globe', 14))
        self.attach_btn.setIcon(draw_icon('attach', 18))
        self.send_btn.setIcon(draw_icon('arrow-right', 14, '#111827'))
        self._refresh_selector_icons()
        self.send_btn.setStyleSheet(
            f'QPushButton {{ background:{theme.ACCENT}; color:#111827; border:none; border-radius:9px; '
            f'padding:7px 15px; font-weight:700; }} '
            f'QPushButton:hover {{ background:{theme.ACCENT}; }} '
            f'QPushButton:pressed {{ background:{theme.TEXT}; }} '
            f'QPushButton:disabled {{ background:#cbd5e1; color:#ffffff; }}'
        )
        for role, frame, name, label in self._messages:
            self._style_message(role, frame, name, label)

    def _agent(self):
        return self.agent_select.currentData() or 'claude'

    def set_agent(self, agent):
        idx = self.agent_select.findData(agent)
        if idx != -1:
            self.agent_select.setCurrentIndex(idx)

    def _agent_changed(self):
        is_codex = self._agent() == 'codex'
        self.prompt.setPlaceholderText('Chiedi, modifica o analizza il codice… (Invio invia · Ctrl+Invio va a capo)')
        self.auto_permissions.setEnabled(self._agent() == 'claude')
        current = self.model_select.currentText().strip()
        models = CODEX_MODELS if is_codex else CLAUDE_MODELS
        self.model_select.clear()
        self.model_select.addItems(models)
        if current in models:
            self.model_select.setCurrentText(current)
        else:
            self.model_select.setCurrentIndex(0)  # a preset from the other agent doesn't carry over
        current_effort = self.effort_select.currentText().strip()
        efforts = EFFORT_LEVELS['codex'] if is_codex else EFFORT_LEVELS['claude']
        self.effort_select.clear()
        self.effort_select.addItems(efforts)
        if current_effort in efforts:
            self.effort_select.setCurrentText(current_effort)
        else:
            self.effort_select.setCurrentIndex(0)  # e.g. xhigh/max don't carry over to codex
        self._refresh_selector_icons()

    # [CST] _EFFORT_COLORS — a low-to-high color ramp so the effort combo's face (it always shows
    # its currently selected item's icon) reads at a glance without opening the dropdown or reading
    # the tooltip: calm green at the low end, through gold/orange, to red at max, with the two
    # unusual "ultra" presets in a visually distinct purple rather than continuing the ramp.
    # [FN] _effort_color — theme color for one effort-level string, DIM (neutral) for the default
    # [FN OPEN] _effort_color
    def _effort_color(self, level):
        return {
            'low': theme.OK, 'medium': theme.ACCENT, 'high': theme.HOT,
            'xhigh': theme.DANGER, 'max': theme.DANGER,
            'ultracode': theme.WARN, 'ultra': theme.WARN,
        }.get(level, theme.DIM)
    # [FN CLOSED] _effort_color

    # [FN CATEGORY] _refresh_selector_icons — model and effort remain native, fully clickable combo
    # boxes, but their compact face is an SVG; the full selected value stays in the tooltip/menu.
    # Effort additionally gets a per-item colored icon (_effort_color) instead of one fixed color for
    # every level — the combo's closed face always shows its current item's own icon, so this alone
    # makes the selected effort level readable by color without opening the dropdown.
    # [FN] _refresh_selector_icons — refreshes AI selector icons and selected-value tooltips
    # [FN OPEN] _refresh_selector_icons
    def _refresh_selector_icons(self):
        model_icon = draw_icon('model', 18)
        for index in range(self.model_select.count()):
            self.model_select.setItemIcon(index, model_icon)
        self.model_select.setToolTip(f'Modello: {self.model_select.currentText()}')
        for index in range(self.effort_select.count()):
            level = self.effort_select.itemText(index).strip()
            self.effort_select.setItemIcon(index, draw_icon('effort', 18, self._effort_color(level)))
        self.effort_select.setToolTip(f'Effort: {self.effort_select.currentText()}')
    # [FN CLOSED] _refresh_selector_icons

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
        details = QLabel(_format_permission_input_html(request['input']))
        details.setTextFormat(Qt.RichText)
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
        button_tooltips = (
            'Nega questa singola richiesta di permesso',
            'Consenti solo questa singola richiesta, ne verra richiesto di nuovo la prossima volta',
            "Consenti questo tipo di richiesta per il resto della sessione, senza chiedere ogni volta",
        )
        # color-coded (deny=danger, allow=accept) instead of three identical plain buttons — a
        # permission decision is exactly the kind of choice a misclick shouldn't be easy to make
        deny_color = theme.TAG_COLORS['TST']
        for button, accent, tooltip in zip(buttons, (deny_color, theme.OK, theme.OK), button_tooltips):
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(tooltip)
            button.setStyleSheet(
                f'QPushButton {{ background:{theme.PANEL}; color:{accent}; border:1px solid {accent}; '
                f'border-radius:8px; padding:6px 12px; font-weight:700; }} '
                f'QPushButton:hover {{ background:{accent}; color:#ffffff; }} '
                f'QPushButton:disabled {{ color:{theme.DIM}; border-color:#e2e8f0; background:#f1f5f9; }}'
            )
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
        self._claude_session_id = None
        self._codex_resumable = False
        self._session_allowed_tools.clear()
        self._append(f'Cartella di lavoro: {cwd}')

    def refresh_focus_label(self):
        text = self.focus_hint() if self.focus_hint else None
        self.focus_label.setText(f'Focus: {text}' if text else '')

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
        label = QLabel(_markdown_to_html(text.strip()))
        label.setTextFormat(Qt.RichText)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # TextSelectableByMouse alone doesn't change the hover cursor — without this the bubble
        # (user messages included) looks like static text even though drag-select already works,
        # same IBeam pairing used for file_path_label wherever TextSelectableByMouse is set
        label.setCursor(Qt.IBeamCursor)
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
        self._write_log(text)
        if not self._stream_render_timer.isActive():
            self._stream_render_timer.start(40)

    def _flush_stream_render(self):
        if self._stream_label is None:
            return
        self._stream_label.setText(_markdown_to_html(self._stream_text.strip()))
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
        if self._pending_attachments:
            # plain absolute paths in the visible prompt text, not a hidden channel like
            # context_hint — the user deliberately chose these, so both the chat bubble and the
            # actual CLI call should show them; claude/codex read the path themselves the same way
            # context_hint already tells them to read the focused file themselves (no separate
            # upload mechanism needed for a CLI-driven integration). The RESOLVED path is what's
            # actually sent (a converted/compressed copy, when convert_attached_document or
            # compress_attached_image produced one) — the original name is only for the chip label.
            attachment_list = '\n'.join(f'- {resolved}' for _original, resolved in self._pending_attachments)
            prompt = f'{prompt}\n\n[File allegati — leggili per rispondere]\n{attachment_list}'
        hint = self.context_hint() if self.context_hint else None
        effort = self.effort_select.currentText().strip()
        if effort == MODEL_DEFAULT:
            effort = None
        if self.run_prompt(prompt, effort=effort, context_hint=hint):
            self.prompt.clear()
            self._pending_attachments = []
            self._refresh_attachment_chips()

    # [FN CATEGORY] _attach_files — lets the user pick documents/images from anywhere on disk to
    # reference in the next message; claude/codex CLIs aren't given the file's bytes directly
    # (there's no multimodal-upload flag for either `-p`/`exec`), so this works the same way
    # context_hint already does — the path is named in the prompt and the CLI's own Read tool
    # (not sandboxed to cwd for reads) opens it, image or text, when it answers. Documents always
    # go through convert_attached_document (MarkItDown, falls back to the original untouched);
    # images only go through compress_attached_image when self.lossy_images is checked — that one
    # is genuinely lossy, so it stays opt-in rather than always-on like the document conversion.
    # [FN] _attach_files — opens a file picker, resolves, and queues the chosen paths
    # [FN OPEN] _attach_files
    def _attach_files(self):
        paths, _filter = QFileDialog.getOpenFileNames(
            self, 'Allega file',
            filter='Immagini e documenti (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.svg *.pdf *.txt *.md *.csv *.json);;Tutti i file (*)',
        )
        if not paths:
            return
        existing_originals = {original for original, _resolved in self._pending_attachments}
        for path in paths:
            if path in existing_originals:
                continue
            resolved = convert_attached_document(path)
            if resolved == path and self.lossy_images.isChecked():
                resolved = compress_attached_image(path)
            self._pending_attachments.append((path, resolved))
        self._refresh_attachment_chips()
    # [FN CLOSED] _attach_files

    def _remove_attachment(self, original_path):
        self._pending_attachments = [
            pair for pair in self._pending_attachments if pair[0] != original_path
        ]
        self._refresh_attachment_chips()

    # [FN CATEGORY] _refresh_attachment_chips — the chip always shows the ORIGINAL filename the
    # user actually picked ("spec.pdf"), never the converted/compressed temp path it resolves to
    # ("kant-attach-doc-spec-a1b2c3.md") — showing the cryptic resolved name would make it look
    # like a different, unexpected file got attached instead of a transparent size-reduction step.
    # [FN] _refresh_attachment_chips — rebuilds the attachment row from _pending_attachments
    # [FN OPEN] _refresh_attachment_chips
    def _refresh_attachment_chips(self):
        while self.attachments_layout.count():
            item = self.attachments_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for original, resolved in self._pending_attachments:
            chip = QFrame()
            chip.setObjectName('attachmentChip')
            chip_layout = QHBoxLayout(chip)
            chip_layout.setContentsMargins(8, 3, 4, 3)
            chip_layout.setSpacing(4)
            name = QLabel(os.path.basename(original))
            name.setStyleSheet(f'color:{theme.TEXT}; border:none;')
            was_reduced = resolved != original
            name.setToolTip(f'{original}\n-> ridotto a: {resolved}' if was_reduced else original)
            if was_reduced:
                shrink_mark = QLabel('↓')
                shrink_mark.setToolTip('Allegato ridotto prima dell\'invio (documento convertito o immagine compressa)')
                shrink_mark.setStyleSheet(f'color:{theme.ACCENT}; border:none; font-weight:700;')
                chip_layout.addWidget(shrink_mark)
            remove_btn = QPushButton('')
            remove_btn.setIcon(draw_icon('close', 12))
            remove_btn.setIconSize(QSize(12, 12))
            remove_btn.setFixedSize(18, 18)
            remove_btn.setCursor(Qt.PointingHandCursor)
            remove_btn.setToolTip('Rimuovi questo allegato')
            remove_btn.setStyleSheet(
                f'QPushButton {{ background:transparent; color:{theme.DIM}; border:none; font-weight:700; }} '
                f'QPushButton:hover {{ color:{theme.WARN}; }}'
            )
            remove_btn.clicked.connect(lambda _checked=False, p=original: self._remove_attachment(p))
            chip_layout.addWidget(name)
            chip_layout.addWidget(remove_btn)
            chip.setStyleSheet(
                f'#attachmentChip {{ background:{theme.CODE_BG}; border:1px solid {theme.BORDER}; border-radius:9px; }}'
            )
            self.attachments_layout.addWidget(chip)
        self.attachments_layout.addStretch(1)
        self.attachments_row.setVisible(bool(self._pending_attachments))
    # [FN CLOSED] _refresh_attachment_chips

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
        self._add_message(prompt, 'user')
        self._write_log(f'\n[{agent_label}]> {prompt}\n')
        if not executable:
            self._append(
                f'{command} non trovato nel PATH. Installa e autentica la CLI, poi riavvia KANT IDE; '
                f'puoi intanto selezionare l’altro agente dal menu.\n'
            )
            return False
        self._auto_permissions_once = bool(auto_permissions_once and agent == 'claude')
        # context_hint (the coding panel's current file/element, unless GLOBAL is on) rides the
        # same hidden system-prompt channel as the KANT comment standard — never part of the
        # visible prompt/chat bubble, reaches the model the identical way for both providers
        skill_prompts = [_load_skill_prompt(name) for name in ('kant-comment-standard', *extra_skills)]
        # context_hint FIRST, skill bodies after — verified live (direct CLI runs, 6/6 vs 6/6):
        # with the hint placed after the ~3.5KB kant-comment-standard body (comment-tagging rules,
        # entirely unrelated to answering a question), the model reliably ignored it and asked the
        # user to paste code instead of reading the focused file itself — no wording of the hint
        # fixed that, including an explicit "this overrides your default instinct" flagged block.
        # Moving the hint first fixed it 3/3; a long, off-topic block placed after clearly wins out
        # over an earlier instruction more than that instruction's own wording strength does.
        system_prompt = '\n\n'.join(p for p in (context_hint, *skill_prompts) if p)
        # logged so a "the AI ignored my focus" report can be checked against what was actually sent,
        # instead of guessing — this is the one piece of the hidden system prompt that changes per
        # message and per coding-panel state, unlike the static skill bodies
        self._write_log(f'[{agent_label} context_hint]> {context_hint!r}\n')
        if agent == 'codex' and system_prompt:
            try:
                self.system_prompt_file = _write_system_prompt_file(system_prompt)
            except OSError as e:
                self._append(f'Impossibile preparare le istruzioni per Codex: {e}\n')
                return False
            name = self.system_prompt_file
            if 'kant-code-map' in extra_skills:
                prompt = (
                    f'/kant-code-map\n'
                    f'Questa e una richiesta esplicita di eseguire /kant-code-map sul progetto corrente. '
                    f'Prima leggi e segui il file temporaneo {name} come istruzioni KANT. '
                    f'Non modificare, non taggare e non includere il file temporaneo {name}; applica invece la convenzione KANT ai file sorgente '
                    f'e crea o aggiorna KANT_<nome-progetto>.md. Richiesta originale: {prompt}'
                )
            else:
                # codex has no --append-system-prompt equivalent (no such flag in `codex exec
                # --help`) — a temp file plus a "read this first" instruction is the only way to
                # deliver the KANT comment standard and the hidden context_hint (the coding panel's
                # currently isolated file/element) without either showing up in the visible chat
                # bubble. This branch must stay separate from the kant-code-map one above: framing
                # every ordinary message as "run /kant-code-map... create or update
                # KANT_<project>.md" (the old, unconditional wording) reframed every single chat
                # message as a project-wide tagging task regardless of what was actually asked,
                # burying both the real request and context_hint's scoping underneath it — which is
                # why the AI seemed to ignore the isolated element/file and "forget" what was asked.
                prompt = (
                    f'Prima leggi il file temporaneo {name}: contiene istruzioni di contesto da '
                    f'seguire per questa risposta (non modificarlo, non menzionarlo esplicitamente '
                    f'all\'utente). Poi rispondi a questa richiesta: {prompt}'
                )
        model = self.model_select.currentText().strip()
        if model == MODEL_DEFAULT:
            model = ''
        # each CLI call is otherwise a fresh, memory-less process — resume this pane's own ongoing
        # conversation (per provider) instead of starting over on every message. Claude: mint a
        # session id on the first turn, --resume it on every later turn. Codex has no equivalent
        # "assign my own id" flag, only `exec resume --last` to continue whatever it last recorded
        # for this cwd — good enough since only one conversation per provider runs from this pane.
        if agent == 'claude':
            if self._claude_session_id is None:
                self._claude_session_id = str(uuid.uuid4())
                session_args = ('--session-id', self._claude_session_id)
            else:
                session_args = ('--resume', self._claude_session_id)
        elif agent == 'codex':
            session_args = ('resume', '--last') if self._codex_resumable else ()
            self._codex_resumable = True
        else:
            session_args = ()
        _, args = _agent_command(agent, prompt, auto_permissions_once, model or None, effort, session_args)
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
        self._append_stream(_normalize_ai_text(self.process.readAllStandardOutput()))

    def _read_stderr(self):
        self._append_stream(_normalize_ai_text(self.process.readAllStandardError()))

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
        if self._stream_render_timer.isActive():
            self._stream_render_timer.stop()
            self._flush_stream_render()  # a pending throttled render must land before the label is dropped below
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

    # [FN CATEGORY] offer_ai_review — a small inline chat card (same shape as a permission-request
    # card) instead of popping the full review window unasked: an AI turn that changed files just
    # gets a quiet "N file changed, want to look?" — "Rivedi" opens show_ai_review's full window,
    # "Accetta tutto"/"Rifiuta tutto" resolve immediately without ever opening it. Never called at
    # all when auto_permissions is on (see workspace._finish_ai_review) — automatic mode means the
    # user asked not to be interrupted for this either.
    # [FN] offer_ai_review — posts the review as a dismissible chat card instead of a popped window
    # [FN OPEN] offer_ai_review
    def offer_ai_review(self, review, render_text, on_resolved):
        total_add = sum(item.get('additions', 0) for item in review)
        total_del = sum(item.get('deletions', 0) for item in review)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setMaximumWidth(540)
        frame.setStyleSheet(f'QFrame {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; border-radius:12px; }}')
        content = QVBoxLayout(frame)
        content.setContentsMargins(12, 9, 12, 10)
        title = QLabel(f'Modifiche AI pronte per la revisione — {len(review)} file (+{total_add} −{total_del})')
        title.setStyleSheet(f'color:{theme.TEXT}; font-weight:600; border:none;')
        file_list = QLabel('\n'.join(f"{item['status']}: {item['path']}" for item in review)[:600])
        file_list.setWordWrap(True)
        file_list.setStyleSheet(f'color:{theme.DIM}; border:none;')
        content.addWidget(title)
        content.addWidget(file_list)
        actions = QHBoxLayout()
        buttons = [QPushButton(label) for label in ('Rifiuta tutto', 'Rivedi', 'Accetta tutto')]
        button_tooltips = (
            'Annulla tutte le modifiche di questo turno e ripristina lo snapshot',
            "Apri la revisione completa (diff per file/blocco, risultato modificabile)",
            'Applica tutte le modifiche cosi come sono, senza aprire la revisione',
        )
        for button, accent, tooltip in zip(buttons, (theme.TAG_COLORS['TST'], theme.ACCENT, theme.OK), button_tooltips):
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(tooltip)
            button.setStyleSheet(
                f'QPushButton {{ background:{theme.PANEL}; color:{accent}; border:1px solid {accent}; '
                f'border-radius:8px; padding:6px 12px; font-weight:700; }} '
                f'QPushButton:hover {{ background:{accent}; color:#ffffff; }}'
            )
            actions.addWidget(button)
        content.addLayout(actions)
        row_layout.addWidget(frame, 0, Qt.AlignLeft)
        row_layout.addStretch(1)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
        QTimer.singleShot(0, lambda: self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum()))

        def resolve_inline(action):
            row.setParent(None)
            if action == 'apply':
                accepted = {item['path']: set(range(len(item['hunks']))) for item in review}
                on_resolved('apply', accepted, {})
            else:
                on_resolved('cancel', {}, {})

        buttons[0].clicked.connect(lambda: resolve_inline('cancel'))
        buttons[1].clicked.connect(lambda: (row.setParent(None), self.show_ai_review(review, render_text, on_resolved)))
        buttons[2].clicked.connect(lambda: resolve_inline('apply'))
    # [FN CLOSED] offer_ai_review

    # [FN CATEGORY] show_ai_review — a large separate window (framed header, no native title bar —
    # same look as the Git panel/MAPPA dialog) instead of embedding the card in the chat transcript,
    # so a multi-file review gets real screen space instead of a 720px-wide bubble. Non-modal like
    # the app's other internal windows, not blocking the rest of the IDE. on_resolved(action,
    # accepted, manual_text) fires exactly once, when the user applies or cancels; the caller does
    # the actual apply/rollback and reports the outcome as a follow-up chat message.
    # [FN] show_ai_review — opens the AI review in its own window
    # [FN OPEN] show_ai_review
    def show_ai_review(self, review, render_text, on_resolved):
        dialog = QDialog(self)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.resize(1100, 760)
        dialog.setStyleSheet(f'QDialog {{ background:{theme.BG}; border:1px solid {theme.BORDER}; }}')

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(34)
        header.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(14, 0, 8, 0)
        title = QLabel(f"Revisione modifiche AI — {len(review)} file")
        title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        title.setStyleSheet(f'color:{theme.TEXT}; letter-spacing:1px; border:none;')
        header_row.addWidget(title)
        header_row.addStretch(1)
        close_btn = QPushButton('×')
        close_btn.setFixedSize(26, 24)
        close_btn.setToolTip('Chiudi (annulla le modifiche proposte, come Annulla ↶)')
        close_btn.setStyleSheet(theme.BUTTON_STYLE)
        header_row.addWidget(close_btn)
        outer.addWidget(header)

        card = _AiReviewCard(review, render_text, in_dialog=True)
        outer.addWidget(card, 1)

        def fire(action):
            card.set_resolved()
            on_resolved(action, card.accepted(), card.manual_text)
            dialog.close()

        card.resolved.connect(fire)
        close_btn.clicked.connect(lambda: card.resolved.emit('cancel'))
        dialog.show()
    # [FN CLOSED] show_ai_review


# [FN CATEGORY] _AiReviewCard — the AI change review UI: a compact summary (file count, +/- totals,
# per-file rows) that "Controllo" expands into a file/hunk checklist plus a diff/editable-result
# tab view. In its own window (in_dialog=True, the only way it's used now — see show_ai_review),
# the summary's own Controllo/Annulla buttons are redundant with the details panel's own action row
# and the window's own close button, so they're skipped and details are shown immediately instead
# of waiting for a click. Emits resolved('apply'|'cancel') exactly once and then locks its action
# buttons.
# [FN] _AiReviewCard — AI review widget (file/hunk selection, diff, editable result)
# [FN OPEN] _AiReviewCard
class _AiReviewCard(QWidget):
    DATA_ROLE = Qt.UserRole
    resolved = Signal(str)

    def __init__(self, review, render_text, in_dialog=False):
        super().__init__()
        self.review = review
        self.render_text = render_text
        self.in_dialog = in_dialog
        self.by_path = {item['path']: item for item in review}
        self.file_items = {}
        self.manual_text = {}
        self._current_path = None
        self._loading_editor = False
        self._action_buttons = []
        if not in_dialog:
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
        root.addWidget(self.details, 1)
        if in_dialog:
            self._show_details()

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
        if not self.in_dialog:
            # same name-label treatment as a normal assistant chat bubble (_style_message), so this
            # reads as another message in the conversation rather than a separate card bolted on
            sender = QLabel('Assistente AI')
            sender.setStyleSheet(f'color:{theme.TEXT}; font-weight:600; background:transparent;')
            layout.addWidget(sender)
        header = QHBoxLayout()
        totals = QVBoxLayout()
        if not self.in_dialog:
            # in a dialog this is redundant with the window's own title bar ("... — N file")
            title = QLabel(f"{len(self.review)} file {'modificato' if len(self.review) == 1 else 'modificati'}")
            title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
            totals.addWidget(title)
        added = sum(item['additions'] for item in self.review)
        deleted = sum(item['deletions'] for item in self.review)
        counts = QLabel(f'<span style="color:{theme.OK}">+{added}</span>  <span style="color:#ef4444">-{deleted}</span>')
        totals.addWidget(counts)
        header.addLayout(totals)
        header.addStretch(1)
        if not self.in_dialog:
            # redundant in a dialog: the window's own close button cancels, and details are
            # already shown immediately instead of waiting for a "Controllo" click
            cancel_btn = self._button('Annulla ↶', lambda: self.resolved.emit('cancel'), action=True)
            cancel_btn.setToolTip('Annulla tutte le modifiche proposte dall\'AI, ripristinando i file com\'erano')
            header.addWidget(cancel_btn)
            review_btn = self._button('Controllo', self._show_details)
            review_btn.setToolTip('Apri la revisione dettagliata riga per riga delle modifiche proposte')
            header.addWidget(review_btn)
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
            path_button.setToolTip('Apri la revisione dettagliata di questo file')
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
        self.more_btn.setToolTip('Mostra o nascondi il resto dei file modificati')
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
        self.diff_highlighter = DiffHighlighter(self.diff_view.document())
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
        reject_selected_btn = self._button('Rifiuta selezionati', lambda: self._set_selected(False))
        reject_selected_btn.setToolTip("Deseleziona i blocchi correntemente evidenziati nell'albero (non li applica)")
        actions.addWidget(reject_selected_btn)
        accept_selected_btn = self._button('Accetta selezionati', lambda: self._set_selected(True))
        accept_selected_btn.setToolTip("Seleziona i blocchi correntemente evidenziati nell'albero (li applica)")
        actions.addWidget(accept_selected_btn)
        actions.addStretch(1)
        cancel_all_btn = self._button('Annulla tutto', lambda: self.resolved.emit('cancel'), action=True)
        cancel_all_btn.setToolTip("Scarta tutte le modifiche proposte, i file restano com'erano")
        actions.addWidget(cancel_all_btn)
        accept_all_btn = self._button('Accetta tutto', self._accept_all, action=True)
        accept_all_btn.setToolTip('Seleziona ogni blocco di ogni file (non applica ancora)')
        actions.addWidget(accept_all_btn)
        apply_btn = self._button('Applica scelte', lambda: self.resolved.emit('apply'), action=True)
        apply_btn.setToolTip('Scrive su disco solo i blocchi attualmente selezionati')
        actions.addWidget(apply_btn)
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
def _build_header_row(owner, node, show_label=True):
    header_row = QHBoxLayout()
    header_row.setContentsMargins(0, 0, 0, 0)
    header_row.setSpacing(1)
    header = None
    if show_label:
        # the name/short-description is the element's headline — bigger than the extended
        # [TAG CATEGORY] description below it (CODE_FONT_PT - 1), not just the same size in a bolder weight
        header = QLabel(_tag_header_html(node.tag, node.name, node.desc))
        header.setTextFormat(Qt.RichText)
        header.setFont(QFont('Consolas', theme.CODE_FONT_PT + 1))
        header.setWordWrap(True)
        header_row.addWidget(header, 1)
    else:
        header_row.addStretch(1)
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

    # show_header=False skips the "[TAG] name" title and fold arrow — used for the outermost
    # element of an isolated/whole-file view, whose identity is already announced by the tab label
    # / split header / title bar, so repeating it here would be pure redundancy. The panel then
    # starts directly with the category description. The ⋮ metadata button stays, right-aligned on
    # its own row — it's still the only way to edit this element's tag/name/description.
    def __init__(self, node: Node, show_header=True):
        super().__init__()
        self.setObjectName('collapsible')
        self.setStyleSheet(
            f'#collapsible {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; '
            f'border-left:4px solid {theme.ACCENT}; border-radius:10px; }}'
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(3, 1, 2, 1)
        outer.setSpacing(1)

        if show_header:
            self.toggle_btn = QToolButton()
            self.toggle_btn.setArrowType(Qt.DownArrow)
            self.toggle_btn.setCheckable(True)
            self.toggle_btn.setChecked(True)
            self.toggle_btn.setToolTip('Comprimi/espandi questa sezione')
            self.toggle_btn.setStyleSheet(f'border:none; color:{theme.TEXT}; background:transparent; padding:0; margin:0;')
            self.toggle_btn.setMaximumWidth(16)
            self.toggle_btn.clicked.connect(self._on_toggle)

            header_row, header = _build_header_row(self, node)
            header.setCursor(Qt.PointingHandCursor)
            header.mousePressEvent = lambda _event: self.toggle_btn.click()
            header_row.insertWidget(0, self.toggle_btn)
            outer.addLayout(header_row)
        else:
            self.toggle_btn = None
            meta_row, _label = _build_header_row(self, node, show_label=False)
            outer.addLayout(meta_row)

        if node.category_desc:
            cat = QLabel(html_escape(node.category_desc))
            cat.setWordWrap(True)
            cat.setStyleSheet(f'color:{theme.DIM}; margin-left: 12px;')
            cat.setFont(QFont('Consolas', theme.CODE_FONT_PT))
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
        if self.toggle_btn is None:
            self.content.setVisible(True)  # no fold arrow to collapse it with — always shown
            return
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

    # show_header=False skips the "[TAG] name" title (same redundancy this is skipped for on
    # CollapsibleSection) — the outermost element of an isolated/whole-file view already has its
    # identity shown in the tab label / split header / title bar. The ⋮ metadata button still
    # shows, right-aligned on its own row — still the only way to edit this element's metadata.
    def __init__(self, node: Node, compact=False, show_header=True):
        super().__init__()
        self.setObjectName('leafCompact' if compact else 'leafSection')
        self.setStyleSheet(
            f'#leafCompact {{ background:transparent; border:0; }} '
            f'#leafSection {{ background:transparent; border:0; }}'
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0 if compact else 3, 1, 0, 1)
        outer.setSpacing(1)

        if show_header:
            header_row, header = _build_header_row(self, node)
            if compact:
                header.setStyleSheet(f'padding:4px 0; border-bottom:1px solid #eef2f7;')
            outer.addLayout(header_row)
        else:
            meta_row, _label = _build_header_row(self, node, show_label=False)
            outer.addLayout(meta_row)

        if node.category_desc:
            cat = QLabel(html_escape(node.category_desc))
            cat.setWordWrap(True)
            cat.setStyleSheet(f'color:{theme.DIM}; margin-left: 4px;')
            cat.setFont(QFont('Consolas', theme.CODE_FONT_PT))
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


# [CST] _APP_ICON_PATH — the app/window icon, a real image file the user supplied (a stylized
# side-profile bust, gold/cream/black), bundled at kant/assets/app_icon.png rather than drawn —
# unlike make_star_icon (the icon this replaces), the user asked for this exact image, pixel for
# pixel, not a recreation.
_APP_ICON_PATH = Path(__file__).resolve().parent / 'assets' / 'app_icon.png'


# [FN CATEGORY] make_app_pixmap / make_app_icon — the source file is a plain opaque square (no
# alpha channel at all — confirmed: PIL reports mode 'RGB'), including its own four corner
# triangles outside the gold rounded border drawn INTO the image. Displayed as-is, those corners
# render as a hard black square poking out from behind the rounded badge against anything but a
# pure-black background — this clips every requested size to a transparent rounded rect matching
# that border's own radius, so the corners are actually transparent and the badge blends into
# whatever theme background sits behind it instead of fighting it.
# [FN] make_app_pixmap / make_app_icon — the app icon, masked to a transparent rounded rect
# [FN OPEN] make_app_pixmap
def _load_masked_app_pixmap(size):
    source = QPixmap(str(_APP_ICON_PATH))
    if source.isNull():
        return QPixmap(size, size)
    scaled = source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    masked = QPixmap(scaled.size())
    masked.fill(Qt.transparent)
    painter = QPainter(masked)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    radius = scaled.width() * 0.16  # matches the source image's own drawn border radius
    path.addRoundedRect(QRectF(0, 0, scaled.width(), scaled.height()), radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, scaled)
    painter.end()
    return masked


def make_app_pixmap(size=64):
    return _load_masked_app_pixmap(size)


def make_app_icon():
    return QIcon(_load_masked_app_pixmap(256))
# [FN CLOSED] make_app_pixmap


# [FN CATEGORY] RecentFolderCard — a clickable row for the welcome screen's recent-projects list:
# bold folder name over a dim full path, the same two-tier hierarchy already used for KANT element
# name/description. QPushButton can't bold just one line of its own text, so this is a small QFrame
# with its own click handling instead (same forwarding shape as the tree row labels).
# [FN] RecentFolderCard — a two-line clickable card: folder name over its full path
# [FN OPEN] RecentFolderCard
class RecentFolderCard(QFrame):
    clicked = Signal(str)

    def __init__(self, path):
        super().__init__()
        self._path = path
        self.setObjectName('recentFolderCard')
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip('Apri questo progetto recente')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 9, 16, 9)
        layout.setSpacing(1)
        name = QLabel(os.path.basename(path.rstrip('/\\')) or path)
        name.setFont(QFont('Consolas', 12, QFont.DemiBold))
        name.setStyleSheet(f'color:{theme.TEXT}; border:none; background:transparent;')
        path_label = QLabel(path)
        path_label.setFont(QFont('Consolas', 9))
        path_label.setStyleSheet(f'color:{theme.DIM}; border:none; background:transparent;')
        layout.addWidget(name)
        layout.addWidget(path_label)
        self.apply_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._path)
        super().mousePressEvent(event)

    def apply_style(self):
        self.setStyleSheet(
            f'#recentFolderCard {{ background:{theme.CODE_BG}; border:1px solid {theme.BORDER}; border-radius:8px; }} '
            f'#recentFolderCard:hover {{ border-color:{theme.ACCENT}; }}'
        )
# [FN CLOSED] RecentFolderCard


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

        self.back_btn = QPushButton('')
        self.back_btn.setIcon(draw_icon('home', 16))
        self.back_btn.setIconSize(QSize(16, 16))
        self.back_btn.setFixedSize(32, 28)
        self.back_btn.setToolTip('Torna al menu iniziale')
        self.back_btn.clicked.connect(window._go_back_to_welcome)
        layout.addWidget(self.back_btn)

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

        # a real QMenuBar (flat text entries, no button chrome) instead of QToolButtons each
        # popping their own QMenu — this window is frameless/custom-drawn, so there's no OS menu
        # bar to fall back on; setNativeMenuBar(False) keeps it drawn inline here even on macOS,
        # where Qt would otherwise try to hijack the real system menu bar for it.
        self.menu_bar = QMenuBar(self)
        self.menu_bar.setNativeMenuBar(False)

        file_menu = self.menu_bar.addMenu('File')
        file_menu.setToolTipsVisible(True)
        self.file_menu_btn = file_menu.menuAction()
        self.file_menu_btn.setToolTip('Comandi sul file attivo: salva, annulla/ripeti, verifica KANT, esegui, esegui test')
        self.save_menu_action = file_menu.addAction('Salva')
        self.save_menu_action.setToolTip('Salva il file attivo su disco (Ctrl+S)')
        self.save_menu_action.triggered.connect(window._save_file)
        self.undo_menu_action = file_menu.addAction('Annulla file')
        self.undo_menu_action.setToolTip("Annulla l'ultima modifica al file attivo (Ctrl+Z)")
        self.undo_menu_action.triggered.connect(window._undo_file)
        self.redo_menu_action = file_menu.addAction('Ripeti file')
        self.redo_menu_action.setToolTip("Ripristina la modifica appena annullata (Ctrl+Y)")
        self.redo_menu_action.triggered.connect(window._redo_file)
        self.validate_kant_menu_action = file_menu.addAction('Verifica KANT')
        self.validate_kant_menu_action.setToolTip(
            'Controlla che i marcatori KANT (tag/#id, apertura/chiusura) di tutto il progetto siano validi'
        )
        self.validate_kant_menu_action.triggered.connect(window._run_kant_validation)
        self.run_menu_action = file_menu.addAction('Esegui')
        self.run_menu_action.setToolTip("Esegue il file attivo con l'interprete/comando adatto al suo tipo (Ctrl+R)")
        self.run_menu_action.triggered.connect(window._run_current_file)
        self.run_tests_menu_action = file_menu.addAction('Esegui test (Ctrl+Shift+T)')
        self.run_tests_menu_action.setToolTip('Esegue l\'intera suite pytest del progetto e mostra i risultati')
        self.run_tests_menu_action.triggered.connect(window._run_tests)

        search_menu = self.menu_bar.addMenu('Cerca')
        search_menu.setToolTipsVisible(True)
        self.search_menu_btn = search_menu.menuAction()
        self.search_menu_btn.setToolTip('Trova e sostituisci testo nel file attivo o in tutto il progetto')
        self.find_menu_action = search_menu.addAction('Trova nel file')
        self.find_menu_action.setToolTip('Cerca (ed eventualmente sostituisce) del testo nel file attualmente aperto')
        self.find_menu_action.triggered.connect(window._show_find_bar)
        self.project_search_menu_action = search_menu.addAction('Cerca nel progetto')
        self.project_search_menu_action.setToolTip('Cerca del testo in tutti i file del progetto aperto')
        self.project_search_menu_action.triggered.connect(window._search_project)
        self.project_replace_menu_action = search_menu.addAction('Sostituisci nel progetto')
        self.project_replace_menu_action.setToolTip('Cerca e sostituisce del testo in tutti i file del progetto aperto')
        self.project_replace_menu_action.triggered.connect(window._replace_project)

        # menu order mirrors the PyCharm-style convention: File, then editing/view-level menus
        # (Cerca ~ Edit's find/replace, Aspetto ~ View), with Git (~ VCS) last — version control is
        # its own concern, not part of the editing flow, so it sits at the end of the row
        appearance_menu = self.menu_bar.addMenu('Aspetto')
        appearance_menu.setToolTipsVisible(True)
        self.appearance_menu_btn = appearance_menu.menuAction()
        self.appearance_menu_btn.setToolTip("Tema chiaro/scuro e la palette comandi")
        self.theme_menu_action = appearance_menu.addAction('Notte')
        self.theme_menu_action.setToolTip('Passa dal tema chiaro a quello scuro (o viceversa)')
        self.theme_menu_action.triggered.connect(window._toggle_theme)
        self.command_palette_menu_action = appearance_menu.addAction('Palette comandi (Ctrl+Shift+P)')
        self.command_palette_menu_action.setToolTip('Apre un elenco cercabile di tutti i comandi disponibili')
        self.command_palette_menu_action.triggered.connect(window._show_command_palette)
        self.vim_mode_menu_action = appearance_menu.addAction('Modalità VIM')
        self.vim_mode_menu_action.setCheckable(True)
        self.vim_mode_menu_action.setChecked(vim_mode_enabled())
        self.vim_mode_menu_action.setToolTip(
            "Editing modale stile VIM nei blocchi di codice: Normal/Insert/Visual, motion "
            "h/j/k/l/w/b/e, operatori d/y/c, navigazione strutturale j/k/gg/G tra gli elementi, "
            "za per piegare, / e : per cercare ed eseguire comandi. Disattivala per digitare "
            "sempre normalmente, come prima."
        )
        self.vim_mode_menu_action.toggled.connect(set_vim_mode)

        lsp_menu = self.menu_bar.addMenu('LSP')
        lsp_menu.setToolTipsVisible(True)
        self.lsp_menu_btn = lsp_menu.menuAction()
        self.lsp_menu_btn.setToolTip('Funzioni del language server: hover, definizione, rename, formattazione, lint, dipendenze')
        self.lsp_hover_menu_action = lsp_menu.addAction('Hover (o passa il mouse su un simbolo)')
        self.lsp_hover_menu_action.setToolTip('Mostra le informazioni del language server per il simbolo sotto il cursore')
        self.lsp_hover_menu_action.triggered.connect(lambda: window._lsp_command('hover'))
        self.lsp_definition_menu_action = lsp_menu.addAction('Vai alla definizione (Ctrl+Click)')
        self.lsp_definition_menu_action.setToolTip('Salta alla definizione del simbolo sotto il cursore')
        self.lsp_definition_menu_action.triggered.connect(lambda: window._lsp_command('definition'))
        self.lsp_references_menu_action = lsp_menu.addAction('References')
        self.lsp_references_menu_action.setToolTip('Elenca tutti i punti del progetto che usano il simbolo sotto il cursore')
        self.lsp_references_menu_action.triggered.connect(lambda: window._lsp_command('references'))
        self.lsp_rename_menu_action = lsp_menu.addAction('Rename symbol (F2)')
        self.lsp_rename_menu_action.setToolTip('Rinomina il simbolo sotto il cursore in tutto il progetto')
        self.lsp_rename_menu_action.triggered.connect(lambda: window._lsp_command('rename'))
        self.lsp_format_menu_action = lsp_menu.addAction('Formatta documento')
        self.lsp_format_menu_action.setToolTip('Formatta il file attivo tramite il language server configurato')
        self.lsp_format_menu_action.triggered.connect(lambda: window._lsp_command('format'))
        self.lsp_format_external_menu_action = lsp_menu.addAction('Formatta con black/ruff')
        self.lsp_format_external_menu_action.setToolTip(
            "Formatta il file Python attivo con black o ruff (dell'interprete del progetto, o del PATH di sistema)"
        )
        self.lsp_format_external_menu_action.triggered.connect(window._format_with_external_tool)
        self.lsp_install_deps_menu_action = lsp_menu.addAction('Installa dipendenze')
        self.lsp_install_deps_menu_action.setToolTip(
            'Installa le dipendenze da requirements.txt o pyproject.toml nell\'interprete del progetto'
        )
        self.lsp_install_deps_menu_action.triggered.connect(window._install_dependencies)
        self.lsp_lint_menu_action = lsp_menu.addAction('Esegui lint (ruff/flake8)')
        self.lsp_lint_menu_action.setToolTip('Analizza il progetto con ruff o flake8 e mostra i problemi trovati')
        self.lsp_lint_menu_action.triggered.connect(window._run_lint_check)

        git_menu = self.menu_bar.addMenu('Git')
        git_menu.setToolTipsVisible(True)
        self.git_menu_btn = git_menu.menuAction()
        self.git_menu_btn.setToolTip('Refresh, diff, stage/unstage, commit, cambio branch; "Altro..." apre il pannello Git completo')
        self.git_refresh_menu_action = git_menu.addAction('Refresh')
        self.git_refresh_menu_action.setToolTip('Aggiorna lo stato Git mostrato nella barra e nella struttura del progetto')
        self.git_refresh_menu_action.triggered.connect(window._git_refresh)
        self.git_diff_menu_action = git_menu.addAction('Diff file')
        self.git_diff_menu_action.setToolTip('Mostra le differenze non salvate del file attivo rispetto a Git')
        self.git_diff_menu_action.triggered.connect(window._git_diff_active_file)
        self.git_stage_menu_action = git_menu.addAction('Stage file')
        self.git_stage_menu_action.setToolTip('Aggiunge il file attivo alla staging area (git add)')
        self.git_stage_menu_action.triggered.connect(window._git_stage_active_file)
        self.git_unstage_menu_action = git_menu.addAction('Unstage file')
        self.git_unstage_menu_action.setToolTip('Rimuove il file attivo dalla staging area (git reset)')
        self.git_unstage_menu_action.triggered.connect(window._git_unstage_active_file)
        self.git_commit_menu_action = git_menu.addAction('Commit...')
        self.git_commit_menu_action.setToolTip('Crea un commit con i file attualmente in staging')
        self.git_commit_menu_action.triggered.connect(window._git_commit)
        self.git_branch_menu_action = git_menu.addAction('Cambia branch...')
        self.git_branch_menu_action.setToolTip('Cambia il branch Git attivo per questo progetto')
        self.git_branch_menu_action.triggered.connect(window._git_switch_branch)
        git_menu.addSeparator()
        self.git_more_menu_action = git_menu.addAction('Altro...')
        self.git_more_menu_action.setToolTip('Apri il pannello Git completo (branch/stage/diff/commit assieme, o il flusso git-init se il progetto non ha ancora un repo)')
        self.git_more_menu_action.triggered.connect(window._open_git_panel)
        layout.addWidget(self.menu_bar)

        self.filename_label = QLabel('')
        self.filename_label.setStyleSheet(f'color:{theme.DIM};')
        layout.addWidget(self.filename_label)

        self.syntax_label = QLabel('')
        layout.addWidget(self.syntax_label)

        layout.addStretch(1)
        self.buttons = [self.back_btn]

        # top-right corner: window chrome only (minimize/maximize/close) — Run/Debug used to share
        # this corner in their own row, but that made them small (28x24) and easy to miss; they now
        # live in the action toolbar row directly below the title bar instead, sized up there.
        chrome_row = QHBoxLayout()
        chrome_row.setContentsMargins(0, 0, 0, 0)
        chrome_row.setSpacing(0)
        chrome_tooltips = {'−': 'Riduci a icona', '□': 'Massimizza/ripristina la finestra', '×': "Chiudi l'IDE"}
        for text, callback in (('−', window.showMinimized), ('□', self._toggle_maximized), ('×', window.close)):
            btn = QPushButton(text)
            btn.setFixedSize(36, 26)
            btn.setToolTip(chrome_tooltips[text])
            btn.clicked.connect(callback)
            self.buttons.append(btn)
            btn.setStyleSheet(theme.BUTTON_STYLE)
            chrome_row.addWidget(btn)
        layout.addLayout(chrome_row)
        self.apply_style()

    def apply_style(self):
        self.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        self.back_btn.setIcon(draw_icon('home', 16))
        self.theme_menu_action.setText('Giorno' if self.window.night_mode else 'Notte')
        # flat text entries (no button chrome/border) — a real menu bar, not a row of buttons
        self.menu_bar.setStyleSheet(
            f'QMenuBar {{ background:transparent; border:none; spacing:4px; }} '
            f'QMenuBar::item {{ background:transparent; color:{theme.TEXT}; padding:6px 10px; '
            f'border-radius:6px; font-weight:600; }} '
            f'QMenuBar::item:selected {{ background:{theme.CODE_BG}; color:{theme.ACCENT}; }} '
            f'QMenuBar::item:pressed {{ background:{theme.ACCENT}; color:#ffffff; }}'
        )
        tool_button_style = theme.BUTTON_STYLE.replace('QPushButton', 'QToolButton')
        # back_btn is icon-only now — theme.BUTTON_STYLE's 7px/13px padding (sized for a text
        # label) was squeezing its 16px icon down to a sliver inside the fixed 32x28 button
        icon_button_style = theme.BUTTON_STYLE.replace('padding:7px 13px;', 'padding:4px;')
        for btn in self.buttons:
            if btn is self.back_btn:
                btn.setStyleSheet(icon_button_style.replace('QPushButton', 'QToolButton') if isinstance(btn, QToolButton) else icon_button_style)
            else:
                btn.setStyleSheet(tool_button_style if isinstance(btn, QToolButton) else theme.BUTTON_STYLE)
        active = self.window.active_tab if hasattr(self.window, 'tabs') else None
        dirty = active.dirty if active else False
        self.filename_label.setStyleSheet(f'color:{theme.ACCENT if dirty else theme.DIM};')

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
        already_dirty = self.dirty
        self.dirty = True
        self.autosave_timer.start(2000)
        # dirtyChanged fans out into a real rebuild (tab title HTML + a QTabBar relayout) — this
        # is called on every keystroke via CodeEdit.textChanged, so re-emitting once already dirty
        # (i.e. every keystroke after the first) redid all of that for no actual state change
        if not already_dirty:
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
