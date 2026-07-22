# [MOD CATEGORY] lsp_panel.py — extracted from mainwindow.py; LspPanelMixin is composed into
# MainWindow alongside the other panel mixins, so every method here still reaches MainWindow
# state (self.lsp_client, self.lsp_completion_requests, self.open_tabs, etc.) directly
# [MOD] lsp_panel.py — LSP response handling: completion, hover, locations, workspace edits
# [MOD OPEN] lsp_panel.py
"""LSP response handling: routes textDocument/* results back to completion popups, hover
tooltips, jump-to-location lists, and workspace-edit application (format/rename).

AI navigation: split out of mainwindow.py, right after the request-dispatch half
(_lsp_command, _active_lsp_position) that stayed in file_ops_panel.py.
"""
import os
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTreeWidgetItem

from kant import theme
from html import escape as html_escape
from kant.model import parse_kant, serialize_kant, KantParseError
from kant.fileio import write_file_atomic
from kant.syntax import KEYWORD_DOCS
from kant.lsp import path_from_file_uri, lsp_server_for_path
from kant.workspace import ROLE_PATH
from kant.widgets import _markdown_to_html, show_code_hover_popup, hide_code_hover_popup

ROLE_KIND = Qt.UserRole
ROLE_LINE = Qt.UserRole + 5
ROLE_TEXT = Qt.UserRole + 6


# [CLS CATEGORY] LspPanelMixin — mixed into MainWindow (alongside IdeDialogsMixin, WorkspaceMixin,
# GitOpsMixin, ProjectPanelMixin, FileOpsMixin) so LSP response handling lives in its own file
# instead of growing mainwindow.py further; every method still reaches MainWindow state
# (self.lsp_client, self.open_tabs, etc.) the same as if it were defined directly on the class.
# [CLS] LspPanelMixin — routes LSP textDocument/* responses to completion/hover/edit handlers
# [CLS OPEN] LspPanelMixin
class LspPanelMixin:

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
            self.lsp_completion_requests = {
                pending_id: pending_edit for pending_id, pending_edit in self.lsp_completion_requests.items()
                if pending_edit is not edit
            }
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
        # a language keyword (def/for/try/...) explains its own syntax role, not a project symbol
        # to resolve — checked first and unconditionally (regardless of LSP availability), since a
        # real language server's own hover is about identifiers/types, not keyword syntax, and
        # would come back empty for these anyway
        if symbol in KEYWORD_DOCS:
            show_code_hover_popup(
                global_pos,
                f'<b style="font-size:larger">{html_escape(symbol)}</b><br>'
                f'{_markdown_to_html(KEYWORD_DOCS[symbol])}',
            )
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
            self.lsp_hover_requests = {
                pending_id: pending for pending_id, pending in self.lsp_hover_requests.items()
                if pending[0] is not edit
            }
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
# [CLS CLOSED] LspPanelMixin
# [MOD CLOSED] lsp_panel.py
