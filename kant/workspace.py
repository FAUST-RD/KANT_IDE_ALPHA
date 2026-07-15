"""Filesystem trust boundary and AI-change transaction lifecycle.

AI navigation: pure snapshot/review/apply helpers come first; ``WorkspaceMixin`` then owns file
watchers, conflict handling, create/rename/trash, and rollback orchestration for ``MainWindow``.
All caller-provided relative paths must pass ``safe_project_path`` before mutation. Symlinks are
excluded from snapshots and removed after agent runs to prevent writes escaping the project root.
"""
import difflib
import os
import shutil
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt

from kant import theme
from kant.fileio import file_fingerprint, is_safe_child_name, write_bytes_atomic, write_file_atomic
from kant.model import KantParseError, parse_kant


ROLE_PATH = Qt.UserRole + 1


def iter_workspace_files(root, ignored=()):
    root = os.path.abspath(root)
    for current, subdirs, files in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in ignored]
        for name in files:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                yield os.path.relpath(path, root), path


def _find_symlinks(root, ignored=()):
    found = []
    for current, subdirs, files in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in ignored]
        for name in (*subdirs, *files):
            path = os.path.join(current, name)
            if os.path.islink(path):
                found.append(path)
    return found


# [FN CATEGORY] strip_unsafe_symlinks — create_snapshot refuses to start if the project already
# contains a symlink, so the pre-run snapshot is guaranteed symlink-free; any symlink found in the
# project afterward was therefore created during the AI run. It could point outside the project
# and is never safe to silently accept, so it is removed here rather than offered for review.
# [FN] strip_unsafe_symlinks — removes any symlink an AI run created, before review/rollback
# [FN OPEN] strip_unsafe_symlinks
def strip_unsafe_symlinks(root, ignored=()):
    removed = []
    for path in _find_symlinks(root, ignored):
        try:
            if os.name == 'nt' and os.path.isdir(path):
                os.rmdir(path)  # removes the reparse point itself, not the target's contents
            else:
                os.remove(path)  # unlink — correct for a symlink to a dir on POSIX too
        except OSError:
            continue
        removed.append(os.path.relpath(path, root).replace(os.sep, '/'))
    return removed
# [FN CLOSED] strip_unsafe_symlinks


def create_snapshot(root, ignored=()):
    parent = tempfile.mkdtemp(prefix='kant-ai-snapshot-')
    snapshot = os.path.join(parent, 'project')
    try:
        os.makedirs(snapshot)
        for current, subdirs, files in os.walk(root):
            subdirs[:] = [name for name in subdirs if name not in ignored]
            links = [name for name in (*subdirs, *files) if os.path.islink(os.path.join(current, name))]
            if links:
                raise OSError(f'snapshot AI non sicuro: link simbolico {os.path.join(current, links[0])}')
            os.makedirs(os.path.join(snapshot, os.path.relpath(current, root)), exist_ok=True)
        for rel, source in iter_workspace_files(root, ignored):
            target = os.path.join(snapshot, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
        return snapshot
    except Exception:
        shutil.rmtree(parent, ignore_errors=True)
        raise


def build_ai_review(root, snapshot, ignored=()):
    """Return file/hunk data needed by the review dialog and deterministic apply step."""
    before = {rel: path for rel, path in iter_workspace_files(snapshot)}
    after = {rel: path for rel, path in iter_workspace_files(root, ignored)}
    review = []
    for rel in sorted(before.keys() | after.keys()):
        old = Path(before[rel]).read_bytes() if rel in before else None
        new = Path(after[rel]).read_bytes() if rel in after else None
        if old == new:
            continue
        status = 'creato' if old is None else ('eliminato' if new is None else 'modificato')
        item = {'path': rel, 'status': status, 'old': old, 'new': new, 'hunks': [], 'opcodes': [], 'binary': False}
        try:
            old_lines = (old or b'').decode('utf-8').splitlines(keepends=True)
            new_lines = (new or b'').decode('utf-8').splitlines(keepends=True)
        except UnicodeDecodeError:
            item['binary'] = True
            item['hunks'] = [{'title': 'File binario', 'diff': '[contenuto binario]'}]
            item['additions'] = item['deletions'] = 0
            review.append(item)
            continue
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        item['opcodes'] = matcher.get_opcodes()
        unified = list(difflib.unified_diff(old_lines, new_lines, f'a/{rel}', f'b/{rel}', n=3))
        header, diff_groups, current = ''.join(unified[:2]), [], []
        for line in unified[2:]:
            if line.startswith('@@') and current:
                diff_groups.append(current)
                current = []
            current.append(line)
        if current:
            diff_groups.append(current)
        additions = deletions = 0
        for number, group in enumerate(matcher.get_grouped_opcodes(3), 1):
            changed = {opcode for opcode in group if opcode[0] != 'equal'}
            if not changed:
                continue
            diff = header + ''.join(diff_groups[number - 1])
            item['hunks'].append({'title': f'Blocco {number}', 'diff': diff, 'opcodes': changed})
            additions += sum(j2 - j1 for tag, _i1, _i2, j1, j2 in changed if tag in ('insert', 'replace'))
            deletions += sum(i2 - i1 for tag, i1, i2, _j1, _j2 in changed if tag in ('delete', 'replace'))
        item['old_lines'], item['new_lines'] = old_lines, new_lines
        item['additions'], item['deletions'] = additions, deletions
        review.append(item)
    return review


def render_review_text(item, accepted_hunks):
    accepted = set().union(*(item['hunks'][index]['opcodes'] for index in accepted_hunks)) if accepted_hunks else set()
    output = []
    for opcode in item['opcodes']:
        tag, i1, i2, j1, j2 = opcode
        output.extend(item['new_lines'][j1:j2] if tag != 'equal' and opcode in accepted else item['old_lines'][i1:i2])
    return ''.join(output)


def apply_ai_review(root, review, accepted, manual_text=None):
    """Keep only accepted AI hunks, using edited final text where the reviewer supplied it."""
    manual_text = manual_text or {}
    for item in review:
        target = safe_project_path(root, item['path'])
        current = Path(target).read_bytes() if os.path.isfile(target) else None
        if current != item['new']:
            raise OSError(f"{item['path']} e cambiato durante la revisione; nessuna scelta applicata")
    for item in review:
        rel = item['path']
        selected = set(accepted.get(rel, ()))
        if rel in manual_text and not item['binary']:
            result = manual_text[rel].encode('utf-8')
        elif item['binary']:
            result = item['new'] if selected else item['old']
        else:
            text = render_review_text(item, selected)
            if item['old'] is None and not selected:
                result = None
            elif item['new'] is None and len(selected) == len(item['hunks']):
                result = None
            else:
                result = text.encode('utf-8')
        target = safe_project_path(root, rel)
        if result is None:
            if os.path.exists(target):
                os.remove(target)
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            write_bytes_atomic(target, result)


def rollback_snapshot(root, snapshot, ignored=()):
    before = {rel: path for rel, path in iter_workspace_files(snapshot)}
    after = {rel: path for rel, path in iter_workspace_files(root, ignored)}
    before_dirs = {os.path.relpath(path, snapshot) for path, _dirs, _files in os.walk(snapshot)}
    after_dirs = set()
    for path, dirs, _files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in ignored]
        after_dirs.add(os.path.relpath(path, root))
    for rel in after.keys() - before.keys():
        os.remove(after[rel])
    for rel, source in before.items():
        target = os.path.join(root, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
    for rel in before_dirs:
        os.makedirs(os.path.join(root, rel), exist_ok=True)
    for rel in sorted(after_dirs - before_dirs, key=lambda value: value.count(os.sep), reverse=True):
        try:
            os.rmdir(os.path.join(root, rel))
        except OSError:
            pass


def discard_snapshot(snapshot):
    if snapshot:
        shutil.rmtree(os.path.dirname(snapshot), ignore_errors=True)


def safe_project_path(root, relative):
    root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, relative))
    if os.path.normcase(os.path.commonpath((root, target))) != os.path.normcase(root):
        raise ValueError(f'percorso fuori dal progetto: {relative}')
    return target


class WorkspaceMixin:
    """Main-window coordination for watched files and reversible AI edits."""

    def _watch_project_tree(self):
        watched = self.fs_watcher.directories()
        if watched:
            self.fs_watcher.removePaths(watched)
        if not self.project_root_path:
            return
        directories = [self.project_root_path]
        for root, subdirs, _files in os.walk(self.project_root_path):
            subdirs[:] = [name for name in subdirs if name not in theme.IGNORE_DIRS]
            directories.extend(os.path.join(root, name) for name in subdirs)
        if directories:
            self.fs_watcher.addPaths(directories)
        for path in self.open_tabs:
            self._watch_open_file(path)

    def _on_fs_directory_changed(self, _path):
        self.fs_refresh_timer.start(400)

    def _watch_open_file(self, path):
        if os.path.isfile(path) and path not in self.fs_watcher.files():
            self.fs_watcher.addPath(path)

    def _tab_for_path(self, path):
        wanted = os.path.normcase(os.path.abspath(path))
        return next((tab for name, tab in self.open_tabs.items()
                     if os.path.normcase(os.path.abspath(name)) == wanted), None)

    def _on_fs_file_changed(self, path):
        tab = self._tab_for_path(path)
        if tab is None:
            return
        if self._ai_snapshot is not None and self.claude_pane.process is not None:
            self._watch_open_file(path)
            return
        current = file_fingerprint(path)
        if current == tab.disk_fingerprint:
            self._watch_open_file(path)
            return
        if current is None and not tab.dirty:
            index = self.tabs.indexOf(tab)
            if index != -1:
                self._close_tab(index, flush=False)
            return
        if tab.dirty:
            self._on_tab_save_conflict(tab)
        else:
            self._reload_tab_from_disk(tab)
        self._watch_open_file(path)

    def _reload_tab_from_disk(self, tab):
        try:
            with open(tab.path, 'r', encoding='utf-8', newline='') as source:
                tree = parse_kant(source.read())
        except (OSError, UnicodeDecodeError, KantParseError) as error:
            self._ide_message('File modificato esternamente', f'Impossibile ricaricare {tab.path}: {error}')
            return False
        if tab.dirty:
            tab.remember_undo_state()
        tab.autosave_timer.stop()
        tab.tree = tree
        tab.disk_fingerprint = file_fingerprint(tab.path)
        tab.dirty = False
        tab.dirtyChanged.emit()
        self._render_view(tab, tab.filter_uid)
        self._update_tab_title(tab)
        self._invalidate_xref()
        return True

    def _on_tab_save_conflict(self, tab):
        if getattr(tab, '_conflict_dialog_open', False):
            return
        tab._conflict_dialog_open = True
        try:
            choice = self._ide_choice(
                'Conflitto sul file',
                f'{os.path.basename(tab.path)} e cambiato fuori da KANT IDE.',
                [
                    ('Annulla', None, "Non fare nulla, decidi la prossima volta che salvi o chiudi il file"),
                    ('Ricarica dal disco', 'reload', "Scarta le modifiche non salvate nell'editor e ricarica la versione su disco"),
                    ('Sovrascrivi il disco', 'overwrite', "Salva la versione dell'editor, sovrascrivendo il cambiamento esterno su disco"),
                ],
            )
            if choice == 'reload':
                self._reload_tab_from_disk(tab)
            elif choice == 'overwrite':
                tab.save(force=True)
        finally:
            tab._conflict_dialog_open = False

    def _on_tab_saved(self, tab):
        self._watch_open_file(tab.path)
        self._sync_kant_map()

    def _refresh_after_fs_change(self):
        if not self.project_root_path:
            return
        self._invalidate_xref()
        self._rebuild_tree()
        self._check_kant_map(self.project_root_path)
        self._watch_project_tree()

    def _refresh_and_validate_after_ai(self):
        if self._closing:
            return
        self._refresh_after_fs_change()
        if not getattr(self.claude_pane, 'validate_after_finish', False):
            return
        self.claude_pane.validate_after_finish = False
        result = self._validate_kant_project()
        if result:
            self.claude_pane.write_info('\n' + result + '\n')

    def _prepare_ai_snapshot(self):
        if not self.project_root_path or not self._flush_all_tabs():
            return False
        if self._ai_snapshot:
            self._ide_message('Snapshot AI', 'Esiste una revisione precedente non conclusa. Riavvia l’IDE per ripristinarla in sicurezza.')
            return False
        try:
            # ponytail: full copy is safest; make it incremental only if profiling proves necessary.
            self._ai_snapshot = create_snapshot(
                self.project_root_path, theme.IGNORE_DIRS | {'.kant-trash'},
            )
        except OSError as error:
            self._ai_snapshot = None
            self._ide_message('Snapshot AI', f'Impossibile creare lo snapshot: {error}')
            return False
        self.settings.setValue('ai/pendingSnapshot', self._ai_snapshot)
        self.settings.setValue('ai/pendingSnapshotProject', self.project_root_path)
        self.settings.sync()
        self.tabs.setEnabled(False)
        return True

    def _clear_ai_snapshot_marker(self):
        self.settings.remove('ai/pendingSnapshot')
        self.settings.remove('ai/pendingSnapshotProject')

    def _finish_ai_review(self):
        snapshot = self._ai_snapshot
        if not snapshot or not self.project_root_path:
            return
        ignored = theme.IGNORE_DIRS | {'.kant-trash'}
        # snapshots are guaranteed symlink-free (create_snapshot refuses to start otherwise), so
        # any symlink here was created during the run — never safe to accept, strip before anything
        stray_symlinks = strip_unsafe_symlinks(self.project_root_path, ignored)
        if self._closing:
            try:
                rollback_snapshot(self.project_root_path, snapshot, ignored)
            except OSError:
                pass
            finally:
                discard_snapshot(snapshot)
                self._ai_snapshot = None
                self._clear_ai_snapshot_marker()
            return
        self.tabs.setEnabled(True)
        try:
            review = build_ai_review(self.project_root_path, snapshot, ignored)
        except OSError as error:
            self._ide_message('Verifica modifiche AI', str(error))
            return
        if stray_symlinks:
            self.claude_pane.write_info(
                '\nSimlink creati durante la sessione AI rimossi per sicurezza: ' + ', '.join(stray_symlinks)
            )
        if not review:
            discard_snapshot(snapshot)
            self._ai_snapshot = None
            self._clear_ai_snapshot_marker()
            return
        summary = '\n'.join(f"- {item['status']}: {item['path']}" for item in review)
        self.claude_pane.write_info(f'\nModifiche AI pronte per il controllo:\n{summary}')

        def resolved(action, accepted, manual_text):
            try:
                if action == 'apply':
                    apply_ai_review(self.project_root_path, review, accepted, manual_text)
                    result_message = 'Modifiche AI revisionate e applicate'
                else:
                    rollback_snapshot(self.project_root_path, snapshot, ignored)
                    result_message = 'Modifiche AI annullate e snapshot ripristinato'
            except OSError as error:
                self._ide_message('Revisione AI', f'Operazione incompleta: {error}\nSnapshot conservato in {snapshot}')
                return
            discard_snapshot(snapshot)
            self._ai_snapshot = None
            self._clear_ai_snapshot_marker()
            for tab in list(self.open_tabs.values()):
                self._on_fs_file_changed(tab.path)
            self.claude_pane.write_info(result_message)

        self.claude_pane.show_ai_review(review, render_review_text, resolved)

    def _create_new_file(self, target_dir):
        if not target_dir:
            return
        name, ok = self._ide_text('Nuovo file', 'Nome del file:')
        name = name.strip()
        if not ok or not name:
            return
        if not is_safe_child_name(name):
            self._ide_message('Nuovo file', 'Usa solo un nome file, senza percorsi.')
            return
        path = os.path.join(target_dir, name)
        if os.path.exists(path):
            self._ide_message('Nuovo file', 'Esiste gia un file o una cartella con questo nome.')
            return
        try:
            Path(path).touch(exist_ok=False)
        except OSError as error:
            self._ide_message('Nuovo file', f'Impossibile creare il file: {error}')
            return
        self._refresh_after_fs_change()
        self._open_file(path)

    def _create_new_folder(self, target_dir):
        if not target_dir:
            return
        name, ok = self._ide_text('Nuova cartella', 'Nome della cartella:')
        name = name.strip()
        if not ok or not name:
            return
        if not is_safe_child_name(name):
            self._ide_message('Nuova cartella', 'Usa solo un nome cartella, senza percorsi.')
            return
        try:
            os.makedirs(os.path.join(target_dir, name), exist_ok=False)
        except OSError as error:
            self._ide_message('Nuova cartella', f'Impossibile creare la cartella: {error}')
            return
        self._refresh_after_fs_change()

    def _rename_tree_item(self, item, kind):
        old_path = item.data(0, ROLE_PATH)
        if not old_path:
            return
        old_name = os.path.basename(old_path)
        new_name, ok = self._ide_text('Rinomina', 'Nuovo nome:', text=old_name)
        new_name = new_name.strip()
        if not ok or not new_name or new_name == old_name:
            return
        if not is_safe_child_name(new_name):
            self._ide_message('Rinomina', 'Usa solo un nome, senza percorsi.')
            return
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        if os.path.exists(new_path):
            self._ide_message('Rinomina', 'Esiste gia un file o una cartella con questo nome.')
            return
        is_dir = kind == 'dir'
        affected = [tab for path, tab in self.open_tabs.items()
                    if path == old_path or (is_dir and path.startswith(old_path + os.sep))]
        if any(not tab.flush_pending_save() for tab in affected):
            return
        try:
            os.rename(old_path, new_path)
        except OSError as error:
            self._ide_message('Rinomina', f'Impossibile rinominare: {error}')
            return
        for tab in affected:
            self._retarget_tab(tab, new_path + tab.path[len(old_path):])
        self._refresh_after_fs_change()

    def _retarget_tab(self, tab, new_path):
        old_path = tab.path
        if old_path in self.fs_watcher.files():
            self.fs_watcher.removePath(old_path)
        del self.open_tabs[old_path]
        tab.path = new_path
        tab.disk_fingerprint = file_fingerprint(new_path)
        self.open_tabs[new_path] = tab
        self._watch_open_file(new_path)
        index = self.tabs.indexOf(tab)
        if index != -1:
            self.tabs.setTabToolTip(index, new_path)
            self._update_tab_title(tab)
        if tab is self.active_tab:
            self._update_filename_label()

    def _trash_dir(self):
        return os.path.join(self.project_root_path or os.getcwd(), '.kant-trash')

    def _restore_candidates(self):
        trash_dir = self._trash_dir()
        if not os.path.isdir(trash_dir):
            return []
        candidates = []
        for entry in sorted(os.scandir(trash_dir), key=lambda value: value.name.lower()):
            if entry.name.endswith('.restore'):
                continue
            metadata = entry.path + '.restore'
            relative = os.path.basename(entry.path).split('-', 2)[-1]
            try:
                if os.path.exists(metadata):
                    relative = Path(metadata).read_text(encoding='utf-8').strip() or relative
            except OSError:
                pass
            candidates.append((f'{entry.name} -> {relative}', entry.path, relative))
        return candidates

    def _restore_from_trash(self):
        candidates = self._restore_candidates()
        if not candidates:
            self._ide_message('Ripristina', 'Cestino vuoto.')
            return
        choice, ok = self._ide_item('Ripristina', 'Elemento:', [candidate[0] for candidate in candidates])
        if not ok:
            return
        _label, source, relative = next(candidate for candidate in candidates if candidate[0] == choice)
        try:
            target = safe_project_path(self.project_root_path, relative)
        except ValueError as error:
            self._ide_message('Ripristina', str(error))
            return
        if os.path.exists(target):
            self._ide_message('Ripristina', f'Esiste gia: {target}')
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            shutil.move(source, target)
            try:
                os.remove(source + '.restore')
            except OSError:
                pass
        except OSError as error:
            self._ide_message('Ripristina', f'Impossibile ripristinare: {error}')
            return
        self._refresh_after_fs_change()
        self.terminal.write_info(f'\n# cestino KANT\nRipristinato: {target}\n')

    def _trash_target(self, path):
        trash_dir = self._trash_dir()
        os.makedirs(trash_dir, exist_ok=True)
        base = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.path.basename(path)}"
        target = os.path.join(trash_dir, base)
        suffix = 1
        while os.path.exists(target):
            target = os.path.join(trash_dir, f'{base}-{suffix}')
            suffix += 1
        return target

    def _move_to_trash(self, path):
        target = self._trash_target(path)
        relative = os.path.relpath(path, self.project_root_path or os.path.dirname(path)).replace(os.sep, '/')
        shutil.move(path, target)
        try:
            write_file_atomic(target + '.restore', relative)
        except OSError:
            pass
        return target

    def _delete_tree_item(self, item, kind):
        path = item.data(0, ROLE_PATH)
        if not path:
            return
        is_dir = kind == 'dir'
        if not self._ide_yes_no(
            'Elimina',
            f'Eliminare {"la cartella" if is_dir else "il file"} "{os.path.basename(path)}"? '
            'Sara spostato nel cestino locale .kant-trash.',
        ):
            return
        affected = [tab for name, tab in self.open_tabs.items()
                    if name == path or (is_dir and name.startswith(path + os.sep))]
        if any(not tab.flush_pending_save() for tab in affected):
            return
        for tab in affected:
            index = self.tabs.indexOf(tab)
            if index != -1:
                self._close_tab(index, flush=False)
        try:
            trashed = self._move_to_trash(path)
        except OSError as error:
            self._ide_message('Elimina', f'Impossibile eliminare: {error}')
            return
        self.terminal.write_info(f'\n# cestino KANT\nNel cestino: {trashed}\nPer ripristinare: {path}\n')
        self._refresh_after_fs_change()
