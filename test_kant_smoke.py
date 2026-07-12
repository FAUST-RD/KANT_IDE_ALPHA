import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PySide6.QtCore import Qt, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsItem, QToolButton, QTreeWidgetItem

from kant.mainwindow import MainWindow, ROLE_KIND, ROLE_PATH
from kant.lsp import file_uri, LspClient
from kant.model import Run, parse_kant, serialize_kant, read_top_level_label_result
from kant.xref import build_xref, XrefElement
from kant.widgets import FileTab, XrefMapDialog, XrefMapView, _agent_command, _force_layout_positions
from kant.workspace import (
    apply_ai_review, build_ai_review, create_snapshot, discard_snapshot, rollback_snapshot,
    render_review_text, safe_project_path,
)
from kant.dialogs import AiReviewDialog
from kant.permission_mcp import handle_message


class LabelStub:
    def setText(self, *_args):
        pass

    def setStyleSheet(self, *_args):
        pass

    def write_info(self, *_args):
        pass


def main():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    assert window.splitter.orientation() == Qt.Horizontal
    assert window.splitter.widget(1) is window.claude_pane
    assert window.main_splitter.orientation() == Qt.Vertical
    assert window.main_splitter.widget(0) is window.workspace_splitter
    assert window.main_splitter.widget(1) is window.terminal
    assert window.workspace_splitter.widget(0).layout().indexOf(window.map_label_btn) == 1
    assert window.workspace_splitter.widget(0).layout().indexOf(window.kant_map_label) == 3
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
        app.processEvents()
        time.sleep(0.01)
    permission_thread.join(timeout=0.1)
    assert bridge_result and bridge_result[0]['behavior'] == 'allow'
    window.claude_pane.auto_permissions.setChecked(False)
    manual_request = {
        'tool_name': 'Edit', 'input': {'file_path': 'sample.py'},
        'event': threading.Event(), 'response': None,
    }
    window.claude_pane._permission_requested(manual_request)
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
    launch_args = []
    launch_window = MainWindow.__new__(MainWindow)
    launch_window.claude_pane = type('Pane', (), {'run_prompt': lambda _self, *args, **kwargs: launch_args.append((args, kwargs))})()
    MainWindow._launch_kant_code_map(launch_window, 'claude')
    assert launch_args[0][1]['auto_permissions_once'] is True
    assert '--full-auto' in _agent_command('codex', 'tagga', True)[1]
    assert not hasattr(window, 'results_label_btn')
    assert window._tree_label('MOD', 'short').testAttribute(Qt.WA_TransparentForMouseEvents)
    assert window.title_bar.file_menu_btn.menu() is not None
    assert window.title_bar.file_menu_btn.popupMode() == QToolButton.DelayedPopup
    window.close()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_dir = root / 'src'
        source_dir.mkdir()
        source = source_dir / 'app.py'
        source.write_text('\n'.join([
            '# [MOD CATEGORY] shop/__init__.py — exposes the server module from the package namespace',
            '# [MOD shop/__init__.py] — package exports',
            '# [MOD OPEN #abc12345] shop/__init__.py',
            'print(1)',
            '# [MOD CLOSED #abc12345] shop/__init__.py',
        ]), encoding='utf-8')
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
        assert 'ERRORI' in tree_window._validate_kant_project()
        assert tree_window.results_view.topLevelItemCount() == 1
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

        tab = FileTab(str(source), parse_kant(source.read_text(encoding='utf-8')))
        top = next(node for node in tab.tree.body if hasattr(node, 'body'))
        run = next(item for item in top.body if isinstance(item, Run))
        tab.remember_undo_state()
        run.lines = ['print(2)']
        assert tab.undo_file() and 'print(1)' in serialize_kant(tab.tree)
        assert tab.redo_file() and 'print(2)' in serialize_kant(tab.tree)
        tab.autosave_timer.stop()

        lsp_window = MainWindow()
        lsp_window.project_root_path = str(root)
        lsp_window._render_view = lambda *_args, **_kwargs: None
        lsp_window._update_tab_title = lambda *_args, **_kwargs: None
        lsp_window._update_filename_label = lambda *_args, **_kwargs: None
        lsp_window._update_lsp_diagnostics = lambda *_args, **_kwargs: None
        lsp_window._ide_message = lambda *_args, **_kwargs: None
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

        xref_tree = parse_kant('\n'.join([
            '# [FN OPEN] alpha', 'def alpha():', '    """beta()"""',
            '    /* beta() */', '# [FN CLOSED] alpha',
            '# [FN OPEN] beta', 'def beta(): pass', '# [FN CLOSED] beta',
        ]))
        xref = build_xref({'sample.py': xref_tree})
        alpha = next(element for element in xref.values() if element.name == 'alpha')
        assert alpha.outgoing == []
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
        assert 'INCOMING' in map_dialog.edge_popup.incoming.text()
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
        module_graph = {
            'module': XrefElement('module', 'module', 'MOD', 'module.py', 'Modulo', 'module.py', 0),
            'child': XrefElement('child', 'child', 'FN', 'work', 'Funzione', 'module.py', 1),
        }
        expand_dialog = XrefMapDialog()
        expand_dialog.resize(900, 650)
        expand_dialog.set_graph(module_graph, 'expand', str(root / 'expand-project'))
        expand_dialog.show()
        app.processEvents()
        assert len(expand_dialog._display) == 1
        node = expand_dialog.view._node_items['module']
        click = expand_dialog.view.mapFromScene(node.sceneBoundingRect().center())
        scale_before = expand_dialog.view.transform().m11()
        center_before = expand_dialog.view.mapToScene(expand_dialog.view.viewport().rect().center())
        QTest.mouseDClick(expand_dialog.view.viewport(), Qt.LeftButton, Qt.NoModifier, click)
        app.processEvents()
        assert len(expand_dialog._display) == 2
        assert abs(expand_dialog.view.transform().m11() - scale_before) < 0.001
        center_after = expand_dialog.view.mapToScene(expand_dialog.view.viewport().rect().center())
        assert abs(center_after.x() - center_before.x()) < 2 and abs(center_after.y() - center_before.y()) < 2
        expand_dialog.close()
        QSettings('KANT', 'KANT Editor').remove(expand_dialog._position_key)

        replace_target = source_dir / 'replace.txt'
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

        delete_target = source_dir / 'delete.txt'
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

        w = MainWindow.__new__(MainWindow)
        w.project_root_path = str(root)
        w.kant_map_path = None
        w.kant_map_label = LabelStub()
        w._xref_cache = None
        w._xref_generation = 0
        w._xref_pending_generation = None
        w.map_dialog = None
        w._map_sync_generation = 0
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
        review_dialog = AiReviewDialog(window, review, render_review_text)
        assert review_dialog.details.objectName() == 'aiReviewDetails'
        assert review_dialog.accepted_hunks('sample.txt') == {0, 1}
        review_dialog.file_items['sample.txt'].child(1).setCheckState(0, Qt.Unchecked)
        assert review_dialog.accepted_hunks('sample.txt') == {0}
        review_dialog.close()
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

    print('KANT smoke: OK')


class KantSmokeTest(unittest.TestCase):
    def test_smoke(self):
        main()


if __name__ == '__main__':
    main()
