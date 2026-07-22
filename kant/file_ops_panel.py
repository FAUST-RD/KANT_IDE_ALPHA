# [MOD CATEGORY] file_ops_panel.py — extracted from mainwindow.py's "tabs" and "file open/save"
# sections; FileOpsMixin is composed into MainWindow alongside the other panel mixins, so every
# method here still reaches MainWindow state (self.tabs, self.open_tabs, self.lsp_client, etc.)
# [MOD] file_ops_panel.py — active-tab lifecycle, file open/save, syntax status, LSP dispatch
# [MOD OPEN] file_ops_panel.py
"""Active-tab lifecycle, file open/save, syntax status, and LSP command dispatch (including the
local-scan fallback when no language server is configured for a file type).

AI navigation: split out of mainwindow.py's "tabs" and "file open/save" sections. LSP *response*
handling (completion/hover/workspace-edit results) stays in mainwindow.py next to the rest of the
coding-board rendering it feeds.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QTabBar, QTreeWidgetItem

from kant import theme
from kant.model import Node, Run, parse_kant, serialize_kant, KantParseError
from kant.fileio import detect_line_ending
from kant.syntax import check_file_syntax
from kant.lsp import LSP_SERVERS_BY_EXT, file_uri, lsp_server_for_path
from kant.projectops import definition_locations, reference_locations
from kant.pyenv import has_module
from kant.workspace import ROLE_PATH
from kant.widgets import CodeEdit, FileTab, _TabLabel, _tag_header_html

ROLE_KIND = Qt.UserRole
ROLE_LINE = Qt.UserRole + 5
ROLE_TEXT = Qt.UserRole + 6


# [CLS CATEGORY] FileOpsMixin — mixed into MainWindow (alongside IdeDialogsMixin, WorkspaceMixin,
# GitOpsMixin, ProjectPanelMixin) so file/tab lifecycle logic lives in its own file instead of
# growing mainwindow.py further; every method still reaches MainWindow state (self.tabs,
# self.open_tabs, self.lsp_client, etc.) the same as if it were defined directly on the class.
# [CLS] FileOpsMixin — active-tab lifecycle, file open/save, syntax status, LSP command dispatch
# [CLS OPEN] FileOpsMixin
class FileOpsMixin:

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
        self._drop_lsp_widget_requests(tab)
        self.lsp_pending_requests = {
            request_id: pending for request_id, pending in self.lsp_pending_requests.items()
            if pending[1] != tab.path
        }
        if tab.path in self.open_tabs:
            del self.open_tabs[tab.path]
        if tab.path in self.fs_watcher.files():
            self.fs_watcher.removePath(tab.path)
        self._close_element_tabs_for(tab)
        if self._preview_file_tab is tab:
            self._preview_file_tab = None
        if self._ai_context_page is tab:
            # unlike _close_element_tab's equivalent guard, this can't reassign to self.active_tab
            # here: removeTab() hasn't run yet, so if `tab` is still the current tab, active_tab
            # would just resolve back to `tab` itself — the same stale reference. None is safe:
            # _ai_context_target() already falls back to self.active_page (evaluated fresh, after
            # removal, whenever it's next read) when _ai_context_page is None. Without this, a
            # background callback (e.g. a git-status refresh landing after this tab closed) that
            # reads the AI focus hint would still hold this now-deleted FileTab and crash trying to
            # touch its (also deleted) scroll_area — reproduced via a real crash log.
            self._ai_context_page = None
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

    # [FN] _run_target_text — KANT "[TAG] name" of whatever Ctrl+R would run, shown next to the
    # Run button. Only ever names a parent element (one with nested KANT children) since a leaf
    # has no run identity of its own — blank whenever the coding board is isolated on a leaf.
    def _run_target_text(self):
        tab = self.active_tab
        if tab is None or tab.tree is None:
            return ''
        uid = self._active_filter_uid()
        node = self._find_node_by_uid(tab.tree, uid) if uid else None
        if node is None:
            node = next((item for item in tab.tree.body if isinstance(item, Node)), None)
        if node is None or not any(isinstance(c, Node) for c in node.body):
            return ''
        return f'[{node.tag}] {node.name}'

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
        tab._tab_label.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding-right:6px;')
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

    # [FN CATEGORY] _open_file — opens a file as a new tab, or just switches to it if it's already
    # open (an already-open tab's live edits are never discarded/re-read from disk by re-clicking it)
    # [FN] _open_file — opens or activates a file's tab
    # [FN OPEN] _open_file
    def _open_file(self, path):
        existing = self.open_tabs.get(path)
        if existing is not None:
            if existing is not self._preview_file_tab:
                index = self.tabs.indexOf(existing)
                if index != -1:
                    self.tabs.setTabVisible(index, True)
                self._release_preview()
                if not getattr(existing, '_pinned', False):
                    self._set_preview_file_tab(existing)
            self.tabs.setCurrentWidget(existing)
            self._set_ai_context_page(existing)
            return True
        # always re-read from disk rather than trusting a tree parsed earlier for the sidebar —
        # the file may have changed since then (fs-watcher debounce, external edit), and a stale
        # tree here would silently overwrite newer disk content on the next save
        try:
            with open(path, 'r', encoding='utf-8', newline='') as f:
                text = f.read()
            # in File view mode the left tree shows plain filenames, no KANT compartmentalization
            # — the coding board should match: open as one plain editable block (markers, if any,
            # stay as literal text) instead of the collapsible per-element breakdown. Same tree
            # shape the "invalid markers" fallback below already uses, so every other code path
            # (save, dirty-tracking, syntax check) needs no special-casing for it.
            if self.view_mode == 'file':
                tree = Node(tag='ROOT', name='', open_raw=None, body=[Run(lines=text.split('\n'))])
            else:
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

        # Replace the one unpinned preview regardless of whether it is a file or element. Closing a
        # dirty FileTab flushes its pending save; only an explicit pin makes a visible tab survive.
        insert_index = self._release_preview()

        tab = FileTab(path, tree, detect_line_ending(path))
        tab._pinned = False
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
        # a KANT-mode left tree labels files/sections by their tags — for a file with none at all,
        # that view has nothing to show; File mode (plain names) is the useful one to land on, and
        # it's what puts the deterministic-tagging button in the action toolbar's sparkle slot
        # (see _style_kant_quick_button) instead of the AI-fill-blanks one
        if self.view_mode == 'code' and not any(isinstance(item, Node) for item in tree.body):
            self._set_view_mode('file')
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

    def _on_lsp_server_error(self, message):
        self.lsp_pending_requests.clear()
        self.lsp_completion_requests.clear()
        self.lsp_hover_requests.clear()
        if hasattr(self, 'terminal'):
            self.terminal.write_info(f'\n# LSP: {message}\n')

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

    def _run_line_offsets(self, node):
        offsets = {}

        def walk(current, count):
            for item in current.body:
                if isinstance(item, Run):
                    offsets[id(item)] = count
                    count += len(item.lines)
                    continue
                count += sum(bool(raw) for raw in (item.category_raw, item.tag_raw, item.open_raw))
                count = walk(item, count)
                count += sum(bool(raw) for raw in (item.closed_raw, item.incoming_raw, item.outgoing_raw))
            return count

        walk(node, 0)
        return offsets

    def _line_count_before_run(self, node, target):
        return self._run_line_offsets(node).get(id(target))

    def _refresh_code_line_offsets(self, tab):
        offsets = self._run_line_offsets(tab.tree)
        container = self.tabs if hasattr(self, 'tabs') else tab.view_container
        for edit in container.findChildren(CodeEdit):
            if getattr(edit, 'kant_tab', None) is tab:
                edit.set_line_number_offset(offsets.get(id(getattr(edit, 'kant_item', None)), 0))

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

    def _lsp_command(self, action, retry=12, expected_path=None):
        tab = self.active_tab
        if tab is None:
            self._ide_message('LSP', 'Apri un file prima di usare i comandi LSP.')
            return
        if expected_path is not None and tab.path != expected_path:
            return
        expected_path = tab.path
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
                QTimer.singleShot(
                    350, lambda: self._lsp_command(action, retry=retry - 1, expected_path=expected_path),
                )
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
# [CLS CLOSED] FileOpsMixin
# [MOD CLOSED] file_ops_panel.py
