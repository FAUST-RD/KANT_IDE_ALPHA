# [MOD CATEGORY] project_panel.py — extracted from mainwindow.py's "project tree" section;
# ProjectPanelMixin is composed into MainWindow alongside the other panel mixins, so every
# method here still reaches MainWindow state (self.project_root_path, self.tabs, etc.) directly
# [MOD] project_panel.py — project lifecycle, KANT map sync/validation, tree/tab preview slot
# [MOD OPEN] project_panel.py
"""Project lifecycle: pure-AI conversation storage, recent folders, KANT map/flow sync and
validation, project tree construction, and the file/element tab preview slot.

AI navigation: split out of mainwindow.py's "project tree" section — everything a project-open
needs before the coding board itself takes over (file tabs, LSP, rendering).
"""
import json
import os
import time
import uuid
from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QFileDialog, QTabBar, QToolButton, QTreeWidgetItem

from kant import theme, skeleton
from kant.icons import draw_icon
from kant.model import Node, ELEMENT_LANGUAGES, build_new_file_content, read_top_level_label_result
from kant.fileio import write_file_atomic, is_safe_child_name
from kant.groupings import load_reconciled_groupings
from kant.projectops import (
    _canonical_map_text, _kant_error_lookup, build_kant_flow_csv, build_kant_map, has_any_kant_tags,
    iter_kant_tagged_files, validate_kant_project,
)
from kant.pyenv import is_python_majority_project
from kant.workspace import ROLE_PATH
from kant.widgets import CodeEdit, RecentFolderCard, _TabLabel, _TreeItemLabel, _tag_header_html, MODEL_DEFAULT, CLAUDE_MODELS, CODEX_MODELS

ROLE_KIND = Qt.UserRole
ROLE_UID = Qt.UserRole + 2
ROLE_LINE = Qt.UserRole + 5
ROLE_TEXT = Qt.UserRole + 6
ROLE_KEY = Qt.UserRole + 7
ROLE_ORDER = Qt.UserRole + 8


# [CLS CATEGORY] ProjectPanelMixin — mixed into MainWindow (alongside IdeDialogsMixin,
# WorkspaceMixin, GitOpsMixin) so project-lifecycle logic lives in its own file instead of
# growing mainwindow.py further; every method still reaches MainWindow state (self.project_root_path,
# self.tabs, self._run_background, etc.) the same as if it were defined directly on the class.
# [CLS] ProjectPanelMixin — pure-AI conversation storage, recent folders, KANT map/flow sync,
# project tree construction, and the file/element tab preview slot for MainWindow
# [CLS OPEN] ProjectPanelMixin
class ProjectPanelMixin:

    def _load_pure_ai_data(self):
        try:
            data = json.loads(self.settings.value('pureAi/conversations', '{}'))
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _persist_pure_ai_data(self):
        self.settings.setValue('pureAi/conversations', json.dumps(self._pure_ai_data, ensure_ascii=False))

    @staticmethod
    def _blank_ai_conversation():
        conversation_id = str(uuid.uuid4())
        return {
            'id': conversation_id,
            'title': 'New conversation',
            'updated': time.time(),
            'state': {'messages': [], 'agent': 'claude'},
        }

    def _ai_project_data(self, path):
        path = os.path.abspath(path)
        project = self._pure_ai_data.get(path)
        if not isinstance(project, dict) or not isinstance(project.get('conversations'), dict):
            project = {'active': None, 'conversations': {}}
            self._pure_ai_data[path] = project
        else:
            project['conversations'] = {
                str(conversation_id): conversation
                for conversation_id, conversation in project['conversations'].items()
                if isinstance(conversation, dict)
                and isinstance(conversation.get('state', {}), dict)
                and conversation.get('state', {}).get('messages')
            }
            for conversation_id, conversation in project['conversations'].items():
                conversation['id'] = conversation_id
                if not isinstance(conversation.get('title'), str):
                    conversation['title'] = 'New conversation'
                if not isinstance(conversation.get('updated'), (int, float)):
                    conversation['updated'] = 0
                if not isinstance(conversation.get('group'), str) or not conversation.get('group', '').strip():
                    conversation.pop('group', None)
                conversation.setdefault('state', {'messages': [], 'agent': 'claude'})
        if project.get('active') not in project['conversations']:
            project['active'] = next(iter(project['conversations']), None)
        return project

    def _save_active_ai_conversation(self):
        if self._loading_ai_conversation or not self.project_root_path:
            return
        state = self.claude_pane.conversation_state()
        if not state['messages']:
            return
        storage_path = self._active_ai_conversation_path or self.project_root_path
        project = self._ai_project_data(storage_path)
        if not self._active_ai_conversation_id:
            conversation = self._blank_ai_conversation()
            self._active_ai_conversation_id = conversation['id']
            project['conversations'][conversation['id']] = conversation
        conversation = project['conversations'].get(self._active_ai_conversation_id)
        if conversation is None:
            return
        conversation['state'] = state
        conversation['updated'] = time.time()
        first_user = next((m.get('text', '') for m in state['messages'] if m.get('role') == 'user'), '')
        if first_user:
            conversation['title'] = ' '.join(first_user.split())[:48]
        project['active'] = self._active_ai_conversation_id
        self._persist_pure_ai_data()
        self._refresh_ai_conversation_sidebar()

    def _switch_ai_project(self, path):
        project = self._ai_project_data(path)
        conversation_id = self._pending_ai_conversation_id or project.get('active')
        self._pending_ai_conversation_id = None
        conversation = project['conversations'].get(conversation_id)
        project['active'] = conversation_id
        self._active_ai_conversation_id = conversation_id
        self._active_ai_conversation_path = os.path.abspath(path)
        self._loading_ai_conversation = True
        try:
            self.claude_pane.set_cwd(path, announce=False)
            self.claude_pane.load_conversation(conversation.get('state', {}) if conversation else {})
        finally:
            self._loading_ai_conversation = False
        self._refresh_ai_conversation_sidebar()

    def _new_ai_conversation(self):
        if not self.project_root_path or self.claude_pane.process is not None:
            return
        self._save_active_ai_conversation()
        project = self._ai_project_data(self.project_root_path)
        project['active'] = None
        self._active_ai_conversation_id = None
        self._active_ai_conversation_path = os.path.abspath(self.project_root_path)
        self._persist_pure_ai_data()
        self._loading_ai_conversation = True
        try:
            self.claude_pane.set_cwd(self.project_root_path, announce=False)
            self.claude_pane.load_conversation({})
        finally:
            self._loading_ai_conversation = False
        self._refresh_ai_conversation_sidebar()

    def _activate_ai_conversation(self, path, conversation_id):
        if self.claude_pane.process is not None:
            self.statusBar().showMessage('Attendi la fine della risposta AI prima di cambiare conversazione.', 3500)
            return
        path = os.path.abspath(path)
        if conversation_id == self._active_ai_conversation_id and path == self._active_ai_conversation_path:
            return
        self._save_active_ai_conversation()
        if self.pure_ai_mode and path != os.path.abspath(self.project_root_path or ''):
            conversation = self._ai_project_data(path)['conversations'].get(conversation_id)
            if conversation is None:
                return
            # Keep the visible history, but never resume a provider session created in a different
            # cwd. The next message starts a fresh session against the project already on screen.
            state = dict(conversation.get('state', {}))
            state['claude_session_id'] = None
            state['codex_resumable'] = False
            self._active_ai_conversation_id = conversation_id
            self._active_ai_conversation_path = path
            self._loading_ai_conversation = True
            try:
                self.claude_pane.load_conversation(state)
            finally:
                self._loading_ai_conversation = False
            self._refresh_ai_conversation_sidebar()
            return
        if path != os.path.abspath(self.project_root_path or ''):
            self._pending_ai_conversation_id = conversation_id
            self._open_project_folder(path)
            return
        self._pending_ai_conversation_id = conversation_id
        self._switch_ai_project(path)

    def _group_ai_conversation(self, path, conversation_id):
        project = self._ai_project_data(path)
        conversation = project['conversations'].get(conversation_id)
        if conversation is None:
            return
        name, ok = self._ide_text(
            'Group chat', 'Group name (leave empty to ungroup):', text=conversation.get('group', ''),
        )
        if not ok:
            return
        name = name.strip()
        if name:
            conversation['group'] = name
        else:
            conversation.pop('group', None)
        self._persist_pure_ai_data()
        self._refresh_ai_conversation_sidebar()

    def _refresh_ai_conversation_sidebar(self):
        if not hasattr(self, 'conversation_sidebar'):
            return
        if self.project_root_path:
            self._ai_project_data(self.project_root_path)
        conversations = []
        for path in list(self._pure_ai_data):
            if not os.path.isdir(path):
                continue
            project = self._ai_project_data(path)
            for conversation in project['conversations'].values():
                conversations.append({**conversation, 'path': path})
        conversations.sort(key=lambda item: item.get('updated', 0), reverse=True)
        self.conversation_sidebar.set_conversations(
            conversations, self._active_ai_conversation_path, self._active_ai_conversation_id,
        )

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
            card = RecentFolderCard(path)
            card.clicked.connect(self._open_project_folder)
            self.recent_layout.addWidget(card)

    def _open_folder(self):
        path = QFileDialog.getExistingDirectory(self, self._tr('Apri cartella'))
        if not path:
            return
        self._open_project_folder(path)

    # [FN CATEGORY] _prompt_new_project — the welcome page's "+" button: creates a brand-new
    # project folder (not just opening an existing one, like _open_folder above) with an optional
    # KANT-tagged starter module (reusing the exact same build_new_file_content machinery the "+"
    # file/element dialogs already use — a new project's first file is language-correct and tagged
    # from line one, not an empty shell) and an optional `git init`, then opens it the same way any
    # existing folder would be.
    # [FN] _prompt_new_project — the welcome page's "+" button click handler
    # [FN OPEN] _prompt_new_project
    def _prompt_new_project(self):
        recent = self._recent_folders()
        default_parent = os.path.dirname(recent[0]) if recent else os.path.expanduser('~')
        result = self._ide_new_project_form(default_parent, default_language='Python')
        if result is None:
            return
        name = result['name']
        if not is_safe_child_name(name):
            self._ide_message('Nuovo progetto', 'Usa solo un nome, senza percorsi.')
            return
        target_dir = os.path.join(result['parent_dir'], name)
        if os.path.exists(target_dir):
            self._ide_message('Nuovo progetto', 'Esiste già una cartella con questo nome.')
            return
        try:
            os.makedirs(target_dir)
            if result['create_starter']:
                ext = ELEMENT_LANGUAGES.get(result['language'], ELEMENT_LANGUAGES['Generico'])['ext']
                content = build_new_file_content('module', result['language'], name)
                write_file_atomic(os.path.join(target_dir, f'main{ext}'), content)
        except OSError as error:
            self._ide_message('Nuovo progetto', f'Impossibile creare il progetto: {error}')
            return
        if result['init_git']:
            git_result = self._run_git(['init'], target_dir)
            if git_result is None or git_result.returncode:
                error = (git_result.stderr or git_result.stdout) if git_result else 'Git non disponibile'
                self._ide_message('Nuovo progetto', f'Progetto creato, ma "git init" non è riuscito:\n{error.strip()[:400]}')
        self._open_project_folder(target_dir)
    # [FN CLOSED] _prompt_new_project

    # [FN CATEGORY] _set_project_chrome_visible — title-bar menus and the action toolbar belong to
    # the project workspace, so the welcome screen keeps only the app identity and window controls.
    # [FN] _set_project_chrome_visible — shows/hides the project title-bar menus and toolbar
    # [FN OPEN] _set_project_chrome_visible
    def _set_project_chrome_visible(self, visible):
        for btn in (
            self.title_bar.file_menu_btn, self.title_bar.search_menu_btn,
            self.title_bar.appearance_menu_btn, self.title_bar.lsp_menu_btn,
            self.title_bar.git_menu_btn,
        ):
            btn.setVisible(visible)
        self.action_toolbar.setVisible(visible)
    # [FN CLOSED] _set_project_chrome_visible

    # [FN CATEGORY] _go_back_to_welcome — flushes any pending edit (nothing is discarded — autosave
    # means switching screens is always safe) and returns to the folder-picker/recent-folders screen
    # [FN] _go_back_to_welcome — the titlebar back arrow's action
    # [FN OPEN] _go_back_to_welcome
    def _go_back_to_welcome(self):
        if not self._flush_all_tabs():
            return
        self._save_active_ai_conversation()
        self._refresh_recent_folders()
        self.stack.setCurrentIndex(0)
        self._set_project_chrome_visible(False)
        self.map_tab_btn.setParent(self.shell)
        self.map_tab_btn.setText(' MAPPA')
        self.map_tab_btn.setProperty('kantIcon', 'arrow-up')
        self.map_tab_btn.setIcon(draw_icon('arrow-up', 12))
        self.map_tab_btn.hide()
        self.claude_tab_btn.hide()
        if self.map_dialog is not None:
            self.map_dialog.hide()
    # [FN CLOSED] _go_back_to_welcome

    def _choose_ai_agent(self):
        return self._ide_agent_choice_form(CLAUDE_MODELS, CODEX_MODELS, MODEL_DEFAULT)

    def _open_project_folder(self, path):
        path = os.path.abspath(path)
        self._save_active_ai_conversation()
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
        if self.pure_ai_mode and self._active_ai_conversation_id:
            # Changing the inspected KANT project must not replace the central archived chat.
            # set_cwd resets provider resume ids, which cannot safely cross workspaces.
            self.claude_pane.set_cwd(path, announce=False)
        else:
            self._switch_ai_project(path)
        self._auto_select_interpreter()
        if is_python_majority_project(path):
            self._switch_terminal_tab(1)
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
                # no KANT convention anywhere yet — the deterministic skeleton pass (tag, name,
                # nesting, #id from the code's own structure, no AI involved) always runs first;
                # the AI is only asked afterward, and only to fill in the descriptions the
                # skeleton pass leaves blank, never to invent the structure itself
                if self._ide_yes_no(
                    'Convenzione KANT',
                    'Questo progetto non usa ancora la convenzione KANT.\n'
                    'Generare adesso lo scheletro dei marker (tag, nesting, #id) in modo '
                    'deterministico, senza AI?',
                ):
                    changed, skipped = skeleton.apply_skeleton_to_project(path)
                    self._rebuild_tree()
                    total = sum(count for _rel, count in changed)
                    summary = f'{len(changed)} file aggiornati, {total} elementi taggati.'
                    if skipped:
                        summary += (
                            f'\n{len(skipped)} file saltati (marker già presenti non validi): '
                            + ', '.join(skipped)
                        )
                    self._ide_message('Scheletro KANT', summary)
                    self._sync_kant_map()
                    if changed and self._ide_yes_no(
                        'Convenzione KANT',
                        "Vuoi che l'AI compili adesso le descrizioni (categoria e riga breve) "
                        'rimaste vuote?',
                    ):
                        choice = self._choose_ai_agent()
                        if choice:
                            self._launch_kant_fill_blanks(choice['agent'], choice['model'], choice['effort'])
        self._watch_project_tree()
        self.stack.setCurrentIndex(1)
        self._set_project_chrome_visible(True)
        if not self.pure_ai_mode and self.splitter.sizes()[1] == 0:
            self._toggle_claude_pane()
        # map_tab_btn itself stays hidden until MAPPA is actually opened (it's now only the
        # in-dialog close handle) — the always-visible entry point is mappa_label_btn in the
        # INCOMING/OUTGOING bar, built already, no per-open show() needed
        self.claude_tab_btn.setVisible(not self.pure_ai_mode)
        self._position_claude_tab()

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
            self.kant_map_label.setStyleSheet(f'color:{theme.OK}; padding:0 8px;')
        else:
            self.kant_map_label.setText('✗ Nessuna KANT_*.md — genera con /kant-code-map')
            self.kant_map_label.setStyleSheet(f'color:{theme.DIM}; padding:0 8px;')

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
        if self._map_sync_running:
            # a build is already in flight for an older generation; it'll see this one and skip its
            # own write, so just note another sync is owed instead of queuing a second full-project
            # os.walk on top of the one already running — every save (_on_tab_saved) calls this
            self._map_sync_rerun_needed = True
            return
        self._map_sync_running = True

        def build_map():
            return build_kant_map(project_root, project_name)

        def save_map(text, error):
            self._map_sync_running = False
            if self._map_sync_rerun_needed:
                self._map_sync_rerun_needed = False
                self._sync_kant_map()
                return
            if error or generation != self._map_sync_generation or project_root != self.project_root_path:
                return
            if os.path.isfile(path):
                try:
                    existing = Path(path).read_text(encoding='utf-8')
                except OSError:
                    existing = None
                if existing is not None and _canonical_map_text(existing) == _canonical_map_text(text):
                    self.kant_map_path = path
                    self._update_kant_map_label()
                    return
            try:
                write_file_atomic(path, text)
            except OSError:
                return
            self.kant_map_path = path
            self._update_kant_map_label()

        self._run_background(build_map, save_map)
        self._sync_kant_flow_csv()
    # [FN CLOSED] _sync_kant_map

    # [FN CATEGORY] _sync_kant_flow_csv — the same generation-counter/rerun-needed/background-write
    # pattern as _sync_kant_map, kept as its own independent background job (not folded into the
    # map's own build_map/save_map closures) so a slow xref build never delays the map write, and a
    # write-only-if-changed check the same way, so an unchanged project doesn't touch the file's
    # mtime on every save. Runs every time _sync_kant_map does — same trigger points (save, project
    # open, self-heal), so the two generated artifacts never drift out of sync with each other.
    # [FN] _sync_kant_flow_csv — rewrites KANT_FLOW_<project>.csv from the current xref graph
    # [FN OPEN] _sync_kant_flow_csv
    def _sync_kant_flow_csv(self):
        if not self.project_root_path:
            return
        project_root = self.project_root_path
        project_name = os.path.basename(project_root)
        path = os.path.join(project_root, f'KANT_FLOW_{project_name}.csv')
        self._flow_sync_generation += 1
        generation = self._flow_sync_generation
        if self._flow_sync_running:
            self._flow_sync_rerun_needed = True
            return
        self._flow_sync_running = True

        def build_csv():
            return build_kant_flow_csv(project_root)

        def save_csv(text, error):
            self._flow_sync_running = False
            if self._flow_sync_rerun_needed:
                self._flow_sync_rerun_needed = False
                self._sync_kant_flow_csv()
                return
            if error or generation != self._flow_sync_generation or project_root != self.project_root_path:
                return
            if os.path.isfile(path):
                try:
                    existing = Path(path).read_text(encoding='utf-8')
                except OSError:
                    existing = None
                if existing is not None and existing.replace('\r\n', '\n') == text.replace('\r\n', '\n'):
                    return
            try:
                write_file_atomic(path, text)
            except OSError:
                return

        self._run_background(build_csv, save_csv)
    # [FN CLOSED] _sync_kant_flow_csv

    # [FN CATEGORY] _validate_kant_project — the single place that composes a validate_kant_project()
    # call into user-facing result text: refreshes kant_map_path first (a map created moments ago,
    # e.g. by an AI review, wouldn't otherwise be seen), self-heals a fixable desync, and appends
    # warnings. extra_sync_states lets a caller broaden which map_state values trigger a self-heal
    # beyond the default 'non_sincronizzata' — the post-AI-apply sequence also wants to generate a
    # brand new map when one didn't exist yet ('assente'), which the manual "Verifica" button
    # shouldn't do unprompted.
    # [FN] _validate_kant_project — validates the generated KANT map and marker structure after AI runs
    # [FN OPEN] _validate_kant_project
    def _validate_kant_project(self, extra_sync_states=()):
        if not self.project_root_path:
            return ''
        self._check_kant_map(self.project_root_path)
        raw = validate_kant_project(self.project_root_path, self.kant_map_path)
        return self._apply_kant_validation_result(raw, extra_sync_states)
    # [FN CLOSED] _validate_kant_project

    # [FN CATEGORY] _apply_kant_validation_result — the result-composition half of
    # _validate_kant_project, split out so a background-scanned result (see
    # _run_kant_validation_background) can go through the exact same self-heal/warnings-append/
    # display logic as the synchronous path, instead of a second hand-rolled copy of it.
    # [FN] _apply_kant_validation_result — turns a raw validate_kant_project() tuple into result text
    # [FN OPEN] _apply_kant_validation_result
    def _apply_kant_validation_result(self, raw, extra_sync_states=()):
        result, errors, visual_errors, map_state, warnings = raw

        # states _sync_kant_map can actually resolve outright: an out-of-date map, or none at all
        # yet. 'errore_generazione' (unreadable existing file / generation itself raised) is NOT
        # included here even when a caller widens extra_sync_states to include it — a sync attempt
        # may well hit the same underlying I/O problem, so its error is never assumed fixed/hidden.
        fixable_error_prefixes = {'non_sincronizzata': 'KANT map non coerente', 'assente': 'manca KANT_'}
        fixable_states = {state for state in ('non_sincronizzata', *extra_sync_states) if state in fixable_error_prefixes}
        if map_state in fixable_states:
            # the one validation failure that's mechanically, deterministically fixable — it's
            # exactly what _sync_kant_map already regenerates from source. No reason to surface it
            # as an error the user has to notice and manually resync themselves; self-heal instead
            # and only report whatever real (marker-syntax) errors remain, if any. Deliberately NOT
            # done when map_state == 'marker_invalidi' — regenerating a map from source that itself
            # fails validation would just write a different-but-still-wrong map.
            errors = [e for e in errors if not e.startswith(fixable_error_prefixes[map_state])]
            self._sync_kant_map()
            note = 'mappa KANT rigenerata automaticamente'
            if errors:
                sample = '\n'.join(f'- {error}' for error in errors[:8])
                extra = f'\n- ... altri {len(errors) - 8} errori' if len(errors) > 8 else ''
                result = f'# KANT verifica: ERRORI\n{sample}{extra}\n# ({note})'
            else:
                result = f'# KANT verifica: OK ({note})'
        elif map_state in extra_sync_states:
            self._sync_kant_map()  # attempt it anyway, but don't claim/hide anything unverified

        if warnings:
            sample = '\n'.join(f'- {warning}' for warning in warnings[:8])
            extra = f'\n- ... altri {len(warnings) - 8} avvisi' if len(warnings) > 8 else ''
            result = f'{result}\n# AVVISI\n{sample}{extra}'

        self._show_validation_results(errors, visual_errors)
        return result
    # [FN CLOSED] _apply_kant_validation_result

    def _run_kant_validation(self):
        result = self._validate_kant_project()
        if result:
            self.terminal.write_info('\n' + result + '\n')

    # [FN CATEGORY] _run_kant_validation_background — the KANT tab in the terminal sidebar used to
    # just show whatever _show_validation_results last left in kant_errors_view (stale, or empty if
    # "Verifica" was never run this session) — clicking it now kicks off a real scan. The scan itself
    # (validate_kant_project's full project walk) runs off the UI thread via _run_background, the
    # same as map-sync/xref-build already do; only the result composition/display touches widgets, in
    # the completion callback that _run_background marshals back onto the main thread.
    # [FN] _run_kant_validation_background — scans the project for KANT problems without blocking the UI
    # [FN OPEN] _run_kant_validation_background
    def _run_kant_validation_background(self):
        if not self.project_root_path:
            return
        self._check_kant_map(self.project_root_path)
        root, map_path = self.project_root_path, self.kant_map_path

        def scan():
            return validate_kant_project(root, map_path)

        def apply(raw, error):
            if error or root != self.project_root_path:
                return
            self._apply_kant_validation_result(raw)

        self._run_background(scan, apply)
    # [FN CLOSED] _run_kant_validation_background

    def _show_validation_results(self, errors, visual_errors):
        if not hasattr(self, 'kant_errors_view'):
            return
        self.kant_errors_view.clear()
        root = QTreeWidgetItem(self.kant_errors_view, [f'Verifica KANT: {"OK" if not errors else str(len(errors)) + " errore/i"}'])
        for path, rel, line, message in visual_errors:
            item = QTreeWidgetItem(root, [f'{rel}:{line}: {message}'])
            item.setData(0, ROLE_KIND, 'validation-result')
            item.setData(0, ROLE_PATH, path)
            item.setData(0, ROLE_LINE, line)
            item.setData(0, ROLE_TEXT, message)
            key, _explanation, _fix = _kant_error_lookup(message)
            if key is not None:
                self._kant_error_pattern_counts[key] += 1
        for message in errors:
            if not any(message.startswith(f'{rel}:') for _path, rel, _line, _msg in visual_errors):
                item = QTreeWidgetItem(root, [message])
                item.setData(0, ROLE_KIND, 'validation-result')
                item.setData(0, ROLE_TEXT, message)
        if not errors:
            QTreeWidgetItem(root, ['Nessun errore'])
        root.setExpanded(True)
        if errors:
            # Showing scan results must not call _switch_terminal_tab(3): that starts another scan
            # and loops forever (scan -> results -> scan) whenever the project has KANT errors.
            self.terminal_stack.setCurrentIndex(3)
            self.terminal_sidebar_group.button(3).setChecked(True)

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
    def _launch_kant_code_map(self, agent, model=None, effort=None):
        if model:
            self.claude_pane.set_agent(agent)  # refreshes the model combo's list for this agent first
            self.claude_pane.model_select.setCurrentText(model)
        self.claude_pane.run_prompt(
            'Applica la convenzione KANT all\'intero progetto, come il comando /kant-code-map: '
            'aggiungi i commenti tag KANT sopra ogni elemento del codice sorgente e crea o aggiorna '
            'KANT_<nome-progetto>.md alla radice del progetto.',
            extra_skills=('kant-code-map',),
            agent=agent,
            auto_permissions_once=True,
            effort=effort,
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
                self.tree.setItemWidget(file_item, 0, self._invalid_file_label(file_item, os.path.basename(file_path), error))
                continue
            if label is None:
                continue  # no KANT tags — only convention-tagged files show up in the tree
            tag, desc, _tree, top_node = label
            file_item = QTreeWidgetItem(parent_item)
            file_item.setData(0, ROLE_KIND, 'file')
            file_item.setData(0, ROLE_PATH, file_path)
            file_item.setData(0, ROLE_UID, top_node.uid)  # the file's own top-level KANT element
            rel = os.path.relpath(file_path, dir_path).replace(os.sep, '/')
            self.tree.setItemWidget(
                file_item, 0, self._tree_label(
                    file_item, tag, desc, bold=True, git_status=self._git_status_for_path(file_path),
                    detail=top_node.category_desc, ai_review=self._ai_review_status.get(rel),
                )
            )
            # start from the top node's own children, not the node itself — it's already
            # shown as this file item's own label, showing it again would duplicate it. top_node
            # itself is document order 0 (_nodes_in_order's first result), so its children start at 1.
            self._build_outline_items(file_item, top_node, file_path, [1])

    def _build_outline_items(self, parent_item, node, path, order_counter):
        for child in node.body:
            if not isinstance(child, Node):
                continue
            item = QTreeWidgetItem(parent_item)
            item.setData(0, ROLE_KIND, 'section')
            item.setData(0, ROLE_UID, child.uid)
            item.setData(0, ROLE_PATH, path)
            item.setData(0, ROLE_ORDER, order_counter[0])
            order_counter[0] += 1
            self.tree.setItemWidget(item, 0, self._tree_label(item, child.tag, child.desc or child.name, detail=child.category_desc))
            self._build_outline_items(item, child, path, order_counter)

    def _tree_label(self, item, tag, text, bold=False, git_status='', detail='', ai_review=None):
        color = theme.TAG_COLORS.get(tag, theme.TEXT)
        bg = theme.TAG_BACKGROUNDS.get(tag, theme.PANEL2)
        weight = '700' if bold else '400'
        git_html = (
            f' <span style="color:{theme.WARN}; font-weight:700">[{html_escape(git_status)}]</span>'
            if git_status else ''
        )
        compact = self.view_mode == 'code' and self.compact_kant_view
        # rich-text spans ignore QLabel.setFont proportionally, so the size needs to be explicit
        # here rather than inherited — its own dedicated (smaller) size, independent of both the
        # tree's own row font and the coding board's CATEGORY text (see theme.TREE_DETAIL_FONT_PT)
        detail_html = (
            f'<br><span style="color:{theme.DIM}; font-size:{theme.TREE_DETAIL_FONT_PT}pt">{html_escape(detail)}</span>'
            if detail and not compact else ''
        )
        # compact/list mode drops the full tag-colored pill background (too heavy for a dense list),
        # but the color itself is the whole point of the tag badge — keep it as a thin colored
        # underline instead of losing it outright
        underline = f'border-bottom:2px solid {color};' if compact else ''
        text_style = f'font-weight:{weight}'
        row_style = f'color:{theme.TEXT}; background:transparent; padding:0px {1 if compact else 4}px;'
        if ai_review is not None:
            text_style, row_style = self._ai_review_label_style(ai_review, text_style, row_style)
        lbl = _TreeItemLabel(
            self.tree, item,
            f'<span style="color:{color}; background-color:{"transparent" if compact else bg}; font-weight:700; '
            f'padding:0px {0 if compact else 4}px; border-radius:4px; {underline}">[{tag}]</span> '
            f'<span style="{text_style}">{html_escape(text)}</span>{git_html}{detail_html}'
        )
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT - 1 if compact else theme.TREE_FONT_PT))
        lbl.setMargin(0)
        lbl.setStyleSheet(row_style)
        lbl.setWordWrap(not compact)
        lbl.setCursor(Qt.PointingHandCursor)
        return lbl

    # [FN CATEGORY] _ai_review_label_style — a pending AI review's per-file status colors that file's
    # tree row: green underline for a created file or a modification with only additions, red
    # underline for a modification with only deletions, strikethrough for a whole deleted file, and
    # (a file with both additions and deletions) a delicate translucent 50/50 green/red split via a
    # QSS qlineargradient background on the row itself.
    # [FN] _ai_review_label_style — returns (text_span_style, row_stylesheet) for one review status
    # [FN OPEN] _ai_review_label_style
    def _ai_review_label_style(self, ai_review, text_style, row_style):
        kind = ai_review['kind']
        additions, deletions = ai_review.get('additions', 0), ai_review.get('deletions', 0)
        if kind == 'deleted':
            return f'{text_style}; color:{theme.DANGER}; text-decoration:line-through;', row_style
        if kind == 'modified' and additions and deletions:
            ok, danger = QColor(theme.OK), QColor(theme.DANGER)
            ok_fill = f'rgba({ok.red()},{ok.green()},{ok.blue()},14%)'
            danger_fill = f'rgba({danger.red()},{danger.green()},{danger.blue()},14%)'
            split = (
                f'qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ok_fill}, stop:0.499 {ok_fill}, '
                f'stop:0.5 {danger_fill}, stop:1 {danger_fill})'
            )
            return text_style, f'{row_style} background:{split};'
        accent = theme.DANGER if kind == 'modified' and not additions else theme.OK
        return f'{text_style}; color:{accent}; text-decoration:underline;', row_style
    # [FN CLOSED] _ai_review_label_style

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
                rel = os.path.relpath(entry.path, self.project_root_path).replace(os.sep, '/') if self.project_root_path else ''
                self.tree.setItemWidget(file_item, 0, self._plain_file_label(
                    file_item, entry.name, self._git_status_for_path(entry.path),
                    ai_review=self._ai_review_status.get(rel),
                ))
    # [FN CLOSED] _build_plain_project_tree

    # [FN CATEGORY] _build_groupings_tree — "Gruppi" mode: one top-level row per saved Grouping
    # (kant/groupings.py), its members as children — resolved against the same cross-reference
    # graph (_get_xref) the Incoming/Outgoing panel already builds, so a member row gets real tag/
    # file/description for free instead of needing its own lookup. A member whose element no longer
    # resolves (renamed/deleted since the grouping was saved) still shows, dimmed, rather than
    # silently vanishing — the grouping itself is the source of truth, not today's xref snapshot.
    # [FN] _build_groupings_tree — renders every project grouping and its members ("Gruppi" mode)
    # [FN OPEN] _build_groupings_tree
    def _build_groupings_tree(self, parent_item, dir_path):
        xref = self._get_xref()
        groupings = load_reconciled_groupings(dir_path, xref)
        if not groupings:
            empty_item = QTreeWidgetItem(parent_item, ['Nessun gruppo — usa "+ Nuovo gruppo" per crearne uno'])
            empty_item.setData(0, ROLE_KIND, 'grouping_empty')
            empty_item.setForeground(0, QColor(theme.DIM))
            return
        for grouping in groupings:
            group_item = QTreeWidgetItem(parent_item)
            group_item.setData(0, ROLE_KIND, 'grouping')
            group_item.setData(0, ROLE_KEY, grouping.id)
            label = _TreeItemLabel(
                self.tree, group_item,
                f'<span style="font-weight:700">{html_escape(grouping.name)}</span> '
                f'<span style="color:{theme.DIM}">({len(grouping.members)})</span>'
            )
            label.setFont(QFont('Consolas', theme.TREE_FONT_PT, QFont.DemiBold))
            label.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:0px 4px;')
            label.setCursor(Qt.PointingHandCursor)
            self.tree.setItemWidget(group_item, 0, label)
            for key in grouping.members:
                member_item = QTreeWidgetItem(group_item)
                member_item.setData(0, ROLE_KIND, 'grouping_member')
                member_item.setData(0, ROLE_KEY, key)
                element = xref.get(key)
                if element is not None:
                    self.tree.setItemWidget(
                        member_item, 0,
                        self._tree_label(member_item, element.tag, element.desc or element.name, detail=element.file),
                    )
                else:
                    rel = key.split('::', 1)[0]
                    dim_label = _TreeItemLabel(
                        self.tree, member_item,
                        f'<span style="color:{theme.DIM}">{html_escape(rel)} (non risolvibile)</span>',
                    )
                    dim_label.setFont(QFont('Consolas', theme.TREE_FONT_PT))
                    dim_label.setStyleSheet('background:transparent; padding:0px 4px;')
                    self.tree.setItemWidget(member_item, 0, dim_label)
            group_item.setExpanded(True)
    # [FN CLOSED] _build_groupings_tree

    def _invalid_file_label(self, item, name, error):
        lbl = _TreeItemLabel(
            self.tree, item,
            f'<span style="color:{theme.TAG_COLORS["TST"]}; font-weight:700">[ERR]</span> '
            f'{html_escape(name)} <span style="color:{theme.DIM}">{html_escape(str(error))}</span>'
        )
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        lbl.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding:0px 4px;')
        lbl.setWordWrap(True)
        lbl.setCursor(Qt.PointingHandCursor)
        return lbl

    def _plain_file_label(self, item, name, git_status='', ai_review=None):
        git_html = (
            f' <span style="color:{theme.WARN}; font-weight:700">[{html_escape(git_status)}]</span>'
            if git_status else ''
        )
        text_style = 'font-weight:400'
        row_style = f'color:{theme.TEXT}; background:transparent; padding:0px 4px;'
        if ai_review is not None:
            text_style, row_style = self._ai_review_label_style(ai_review, text_style, row_style)
        lbl = _TreeItemLabel(self.tree, item, f'<span style="{text_style}">{html_escape(name)}</span>{git_html}')
        lbl.setFont(QFont('Consolas', theme.TREE_FONT_PT))
        lbl.setStyleSheet(row_style)
        lbl.setWordWrap(True)
        lbl.setCursor(Qt.PointingHandCursor)
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
        elif kind == 'grouping_member':
            self._navigate_to_element(item.data(0, ROLE_KEY))
        elif kind == 'section':
            path = item.data(0, ROLE_PATH)
            order = item.data(0, ROLE_ORDER)
            self._open_file(path)
            tab = self.open_tabs.get(path)
            if tab is None:
                return
            uid = item.data(0, ROLE_UID)
            # a legacy (no #id) file mints a fresh uid on every reparse (_open_file always
            # re-reads from disk rather than trusting the tree's last parse), so the tree item's
            # uid can silently fail to match tab.tree — falling through to the whole-file view
            # instead of isolating. Document order survives reparse and is the reliable fallback,
            # the same pattern _navigate_to_element already uses for this exact problem.
            node = self._find_node_by_uid(tab.tree, uid)
            if node is None and order is not None:
                nodes = self._nodes_in_order(tab.tree)
                if order < len(nodes):
                    node = nodes[order]
            resolved_uid = node.uid if node is not None else uid
            self._show_element_tab(tab, resolved_uid)
            self._update_io_tabs(resolved_uid)

    # [FN] _on_tree_item_double_clicked — opens the item in the shared coding tab bar
    # [FN OPEN] _on_tree_item_double_clicked
    def _on_tree_item_double_clicked(self, item, _column):
        kind = item.data(0, ROLE_KIND)
        if kind not in ('file', 'section'):
            return
        path = item.data(0, ROLE_PATH)
        tab = self.open_tabs.get(path)
        if tab is None:
            if not self._open_file(path):
                return
            tab = self.open_tabs.get(path)
            if tab is None:
                return
        if kind == 'file':
            self.tabs.setCurrentWidget(tab)
            return
        order = item.data(0, ROLE_ORDER)
        uid = item.data(0, ROLE_UID)
        node = self._find_node_by_uid(tab.tree, uid)
        if node is None and order is not None:
            nodes = self._nodes_in_order(tab.tree)
            if order < len(nodes):
                node = nodes[order]
        self._show_element_tab(tab, node.uid if node is not None else uid)
    # [FN CLOSED] _on_tree_item_double_clicked

    # [FN CATEGORY] _show_element_tab — an unpinned child replaces its unpinned parent visually;
    # the hidden FileTab still owns the shared source/undo/save model. A pinned parent remains.
    # [FN] _show_element_tab — opens an element in the shared file/element preview slot
    # [FN OPEN] _show_element_tab
    def _show_element_tab(self, tab, uid):
        key = (tab.path, uid)
        existing = self._element_pages.get(key)
        if existing is not None:
            if self._preview_page is not None and self._preview_page is not existing:
                self._release_preview()
            if self._preview_file_tab is not None:
                if self._preview_file_tab is tab:
                    index = self.tabs.indexOf(tab)
                    if index != -1:
                        self.tabs.setTabVisible(index, False)
                    self._preview_file_tab = None
                else:
                    self._release_preview()
            self.tabs.setCurrentWidget(existing)
            self._set_ai_context_page(existing)
            return
        node = (
            next((item for item in tab.tree.body if isinstance(item, Node)), None) if uid is None
            else self._find_node_by_uid(tab.tree, uid)
        )
        if node is None:
            return
        if self._preview_file_tab is not None:
            if self._preview_file_tab is tab:
                index = self.tabs.indexOf(tab)
                if index != -1:
                    self.tabs.setTabVisible(index, False)
                self._preview_file_tab = None
            else:
                self._release_preview()
        if self._preview_page is not None:
            self._retarget_element_page(self._preview_page, tab, uid)
            return
        page, layout = self._build_element_page()
        page._element_key = key
        page._file_tab = tab
        page._view_layout = layout
        page._pinned = False
        parent_index = self.tabs.indexOf(tab)
        index = self.tabs.insertTab(parent_index + 1, page, '') if parent_index != -1 else self.tabs.addTab(page, '')
        page._tab_label = _TabLabel(self.tabs, page)
        self.tabs.setTabToolTip(index, tab.path)
        self._element_pages[key] = page
        self._update_element_tab_title(page)
        self._set_preview_page(page)
        self.tabs.setCurrentIndex(index)
        self._set_ai_context_page(page)
    # [FN CLOSED] _show_element_tab

    # [FN CATEGORY] _retarget_element_page — the actual "reuse this tab slot" move: re-key
    # _element_pages, point the page at the new (file, element), and let _render_element_page
    # rebuild its content from scratch — no risk to unsaved edits, since the underlying FileTab
    # (open_tabs, dirty state, tree) for whichever file the page WAS showing is untouched by this;
    # only which node this particular tab slot displays changes.
    # [FN] _retarget_element_page — reuses an existing (presumably preview) page for a new element
    # [FN OPEN] _retarget_element_page
    def _retarget_element_page(self, page, tab, uid):
        node = (
            next((item for item in tab.tree.body if isinstance(item, Node)), None) if uid is None
            else self._find_node_by_uid(tab.tree, uid)
        )
        if node is None:
            return
        old_tab = page._file_tab
        old_key = getattr(page, '_element_key', None)
        if old_key is not None:
            self._element_pages.pop(old_key, None)
        page._element_key = (tab.path, node.uid)
        page._file_tab = tab
        self._element_pages[page._element_key] = page
        index = self.tabs.indexOf(page)
        self.tabs.setTabToolTip(index, tab.path)
        self._render_element_page(page)
        self._update_element_tab_title(page)
        self.tabs.setCurrentWidget(page)
        self._set_ai_context_page(page)
        if old_tab is not tab:
            self._close_hidden_backing_if_unused(old_tab)
    # [FN CLOSED] _retarget_element_page

    def _release_preview(self):
        """Remove the one visible unpinned preview; pinned tabs are never referenced here."""
        insert_index = None
        if self._preview_page is not None:
            page = self._preview_page
            insert_index = self.tabs.indexOf(page)
            self._close_element_tab(page)
        if self._preview_file_tab is not None:
            tab = self._preview_file_tab
            index = self.tabs.indexOf(tab)
            has_pinned_child = any(
                page._pinned and page._file_tab is tab for page in self._element_pages.values()
            )
            if has_pinned_child:
                if index != -1:
                    self.tabs.setTabVisible(index, False)
                self._preview_file_tab = None
            elif index != -1:
                insert_index = index
                if not self._close_tab(index):
                    insert_index = None
        return insert_index

    # [FN CATEGORY] _tab_action_button — pin and close controls share the same SVG/theme behavior
    # for file and element tabs, so their construction stays in one place.
    # [FN] _tab_action_button — builds a themed icon button for a tab corner
    # [FN OPEN] _tab_action_button
    def _tab_action_button(self, kind, tooltip, callback):
        button = QToolButton()
        button.setProperty('kantIcon', kind)
        button.setIcon(draw_icon(kind, 14))
        button.setIconSize(QSize(14, 14))
        button.setAutoRaise(True)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(tooltip)
        button.clicked.connect(callback)
        return button
    # [FN CLOSED] _tab_action_button

    # [FN CATEGORY] _set_preview_page — swaps the new preview page's tab-bar × (QTabBar.RightSide)
    # for a pin button; pinning swaps it again for a plain close button (not Qt's native fallback —
    # a pinned tab has nothing left to "unpin" back to, since it's no longer anyone's preview slot,
    # so the only remaining action for that corner is closing it outright) and frees this page from
    # ever being silently retargeted again.
    # [FN] _set_preview_page — marks a page as the one reusable/unpinned preview tab
    # [FN OPEN] _set_preview_page
    def _set_preview_page(self, page):
        self._preview_page = page
        pin_btn = self._tab_action_button(
            'pin', 'Blocca questa scheda (impedisce che venga sostituita da un nuovo elemento)',
            lambda: self._pin_element_page(page),
        )
        index = self.tabs.indexOf(page)
        self.tabs.tabBar().setTabButton(index, QTabBar.RightSide, pin_btn)
        # setTabButton alone can leave the widget internally hidden with no matching re-show — the
        # exact same reproduced Qt bug _update_tab_title's _tab_label already works around (see its
        # comment); missing here meant a real mouse click could land on nothing; QTest.mouseClick
        # calling the widget directly bypassed hit-testing and never caught it.
        pin_btn.show()
    # [FN CLOSED] _set_preview_page

    def _pin_element_page(self, page):
        if page is None:
            return
        page._pinned = True
        if self._preview_page is page:
            self._preview_page = None
        index = self.tabs.indexOf(page)
        if index == -1:
            return
        close_btn = self._tab_action_button('close', 'Chiudi questa scheda', lambda: self._close_element_tab(page))
        bar = self.tabs.tabBar()
        old_btn = bar.tabButton(index, QTabBar.RightSide)
        bar.setTabButton(index, QTabBar.RightSide, close_btn)
        close_btn.show()  # same setTabButton re-show workaround as _set_preview_page above
        if old_btn is not None:
            # setTabButton only detaches the old widget, it doesn't delete it — same leak
            # _update_tab_title's _tab_label cleanup already guards against, just for this corner
            old_btn.deleteLater()

    # [FN] _set_preview_file_tab — same pin-button swap as _set_preview_page, one level up for
    # whole-file tabs (see _open_file for why files are closed-and-reopened rather than retargeted)
    # [FN OPEN] _set_preview_file_tab
    def _set_preview_file_tab(self, tab):
        self._preview_file_tab = tab
        pin_btn = self._tab_action_button(
            'pin', 'Blocca questa scheda (impedisce che venga sostituita aprendo un altro file)',
            lambda: self._pin_file_tab(tab),
        )
        index = self.tabs.indexOf(tab)
        self.tabs.tabBar().setTabButton(index, QTabBar.RightSide, pin_btn)
        pin_btn.show()  # same setTabButton re-show workaround as _set_preview_page
    # [FN CLOSED] _set_preview_file_tab

    def _pin_file_tab(self, tab):
        if tab is None:
            return
        tab._pinned = True
        if self._preview_file_tab is tab:
            self._preview_file_tab = None
        index = self.tabs.indexOf(tab)
        if index == -1:
            return
        self.tabs.setTabVisible(index, True)
        close_btn = self._tab_action_button(
            'close', 'Chiudi questa scheda', lambda: self._close_tab(self.tabs.indexOf(tab)),
        )
        bar = self.tabs.tabBar()
        old_btn = bar.tabButton(index, QTabBar.RightSide)
        bar.setTabButton(index, QTabBar.RightSide, close_btn)
        close_btn.show()  # same setTabButton re-show workaround as _set_preview_page
        if old_btn is not None:
            old_btn.deleteLater()  # same leak _update_tab_title's _tab_label cleanup already guards against

    def _render_element_page(self, page):
        layout = page._view_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        tab = page._file_tab
        node = self._find_node_by_uid(tab.tree, page._element_key[1])
        if node is not None:
            self._build_node_widgets(tab, Node(tag='ROOT', name='', open_raw=None, body=[node]), layout, 0)

    def _update_element_tab_title(self, page):
        tab = page._file_tab
        node = self._find_node_by_uid(tab.tree, page._element_key[1])
        if node is None:
            return
        html = _tag_header_html(node.tag, node.name, node.desc, bold_name=True)
        if tab.dirty:
            html += f' <span style="color:{theme.ACCENT}">●</span>'
        page._tab_label.setText(html)
        # right padding — with none, this label's own sizeHint (what the tab bar sizes the tab
        # around) fit the text exactly flush to its right edge, so it touched the pin/close button
        # sitting right next to it on the tab's RightSide with no breathing room at all
        page._tab_label.setStyleSheet(f'color:{theme.TEXT}; background:transparent; padding-right:6px;')
        page._tab_label.adjustSize()
        index = self.tabs.indexOf(page)
        self.tabs.tabBar().setTabButton(index, QTabBar.LeftSide, page._tab_label)
        page._tab_label.show()

    def _close_element_tab(self, page, cleanup_backing=True):
        if page is None:
            return
        tab = page._file_tab
        self._drop_lsp_widget_requests(page)
        self._element_pages.pop(getattr(page, '_element_key', None), None)
        if self._preview_page is page:
            self._preview_page = None
        if self._ai_context_page is page:
            self._ai_context_page = self.active_tab
        index = self.tabs.indexOf(page)
        if index != -1:
            self.tabs.removeTab(index)
        page.deleteLater()
        if cleanup_backing:
            self._close_hidden_backing_if_unused(tab)

    def _close_hidden_backing_if_unused(self, tab):
        index = self.tabs.indexOf(tab)
        if (
            index != -1 and not self.tabs.isTabVisible(index)
            and not any(page._file_tab is tab for page in self._element_pages.values())
        ):
            self._close_tab(index)

    def _close_element_tabs_for(self, tab):
        for page in list(self._element_pages.values()):
            if page._file_tab is tab:
                self._close_element_tab(page, cleanup_backing=False)

    def _drop_lsp_widget_requests(self, widget):
        edits = set(widget.findChildren(CodeEdit))
        if not edits:
            return
        self.lsp_completion_requests = {
            request_id: edit for request_id, edit in self.lsp_completion_requests.items() if edit not in edits
        }
        self.lsp_hover_requests = {
            request_id: pending for request_id, pending in self.lsp_hover_requests.items() if pending[0] not in edits
        }
# [CLS CLOSED] ProjectPanelMixin
# [MOD CLOSED] project_panel.py
