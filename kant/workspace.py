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

from PySide6.QtCore import Qt, QEasingCurve, QPropertyAnimation, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsColorizeEffect

from kant import theme
from kant.fileio import file_fingerprint, is_safe_child_name, write_bytes_atomic, write_file_atomic
from kant.groupings import migrate_member_paths
from kant.mappa import migrate_position_keys
from kant.model import KantParseError, parse_kant, serialize_kant


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
    try:
        parent = tempfile.mkdtemp(prefix='kant-ai-snapshot-')
    except FileNotFoundError:
        # same transient-missing-base-tempdir issue test_kant_smoke.py's _mkdtemp_safe works around
        # (observed on some macOS CI runners) — fall back to a directory inside the project itself,
        # guaranteed to exist, rather than losing the AI-review snapshot entirely
        fallback = os.path.join(root, '.kant', 'tmp')
        os.makedirs(fallback, exist_ok=True)
        parent = tempfile.mkdtemp(prefix='kant-ai-snapshot-', dir=fallback)
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
        # a list() copy, not the bare return value: SequenceMatcher.get_opcodes() caches and hands
        # back its OWN internal list, and get_grouped_opcodes() below (used for item['hunks']) trims
        # that same cached list's first/last 'equal' opcode down to n=3 lines of context IN PLACE —
        # without this copy, item['opcodes'] would silently lose whatever old/unchanged lines sit
        # outside that 3-line window the moment hunks are built, and render_review_text (which
        # leans on item['opcodes'] being the FULL, untrimmed partition) would drop them for real on
        # every apply, not just display them wrong.
        item['opcodes'] = list(matcher.get_opcodes())
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
    """Keep only accepted AI hunks, using edited final text where the reviewer supplied it. Returns
    the rel-paths that survive the apply as non-binary text (still exist, not binary) — the input
    normalize_missing_ids needs to know which files it may touch."""
    manual_text = manual_text or {}
    for item in review:
        target = safe_project_path(root, item['path'])
        current = Path(target).read_bytes() if os.path.isfile(target) else None
        if current != item['new']:
            raise OSError(f"{item['path']} e cambiato durante la revisione; nessuna scelta applicata")
    kept = []
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
            if not item['binary']:
                kept.append(rel)
    return kept


# [FN CATEGORY] normalize_missing_ids — after an accepted AI review, stamp missing #ids into the
# kept files so a later reparse doesn't mint a fresh one every time (kant/model.py's _assign_uids
# mints in-memory only; it's serialize_kant's write-back that makes an id stick). Deliberately scoped
# to exactly the paths the caller passes in (apply_ai_review's own `kept` return value) — never all
# legacy files project-wide, and never anything the review rejected or a rollback restored.
# [FN] normalize_missing_ids — persists missing #ids for the given rel-paths; reports what happened
# [FN OPEN] normalize_missing_ids
def normalize_missing_ids(root, paths):
    normalized, skipped = [], []
    for rel in paths:
        try:
            target = safe_project_path(root, rel)
        except ValueError:
            skipped.append((rel, 'percorso fuori dal progetto'))
            continue
        if not os.path.isfile(target):
            skipped.append((rel, 'file non piu presente'))
            continue
        try:
            original = Path(target).read_bytes()
        except OSError as error:
            skipped.append((rel, f'lettura fallita: {error}'))
            continue
        fingerprint_before = file_fingerprint(target)
        try:
            text = original.decode('utf-8')
        except UnicodeDecodeError:
            skipped.append((rel, 'file binario'))
            continue
        newline = '\r\n' if '\r\n' in text else '\n'
        normalized_text = text.replace('\r\n', '\n')
        try:
            tree = parse_kant(normalized_text)
        except KantParseError:
            skipped.append((rel, 'marker non validi'))
            continue
        serialized = serialize_kant(tree)
        if serialized == normalized_text:
            continue  # no id was missing — nothing to normalize, not a skip either
        if file_fingerprint(target) != fingerprint_before:
            skipped.append((rel, 'modificato esternamente durante la normalizzazione'))
            continue
        try:
            write_bytes_atomic(target, serialized.replace('\n', newline).encode('utf-8'))
        except OSError as error:
            skipped.append((rel, f'scrittura fallita: {error}'))
            continue
        normalized.append(rel)
    return normalized, skipped
# [FN CLOSED] normalize_missing_ids


def rollback_snapshot(root, snapshot, ignored=()):
    """Restores root to snapshot's state. Returns the list of directories that were expected to be
    removed (present after the AI run, absent before it) but couldn't be — e.g. something else
    wrote a file into one after this rollback started walking it. Non-fatal: the file-level restore
    above already succeeded, so the rollback overall still worked, but the caller should say so
    instead of the directory just silently surviving with no trace anywhere."""
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
    skipped_dirs = []
    for rel in sorted(after_dirs - before_dirs, key=lambda value: value.count(os.sep), reverse=True):
        try:
            os.rmdir(os.path.join(root, rel))
        except OSError:
            skipped_dirs.append(rel)
    return skipped_dirs


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
        # a pending snapshot alone means "there's an unresolved AI review in flight" — requiring
        # claude_pane.process too (as this used to) left a real gap: the CLI process exits (process
        # becomes None) well before the review is resolved (_finish_ai_review's own diff-building,
        # then — outside auto_permissions — however long the user takes to click Accetta/Rivedi/
        # Rifiuta). Any fs_watcher event landing in that window used to run straight through to
        # conflict-detection/reload-from-disk on a tab that was really just showing the AI's own
        # still-pending, not-yet-reviewed edit.
        if self._ai_snapshot is not None:
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
        # _render_view below tears down and rebuilds every CodeEdit/section widget from scratch —
        # necessary (the tree changed), but it silently resets the scroll position to the top and
        # gives no visual sign anything happened. Most callers of this are an external tool (most
        # often an AI edit landing) rewriting the file the user is currently looking at, so losing
        # their place and getting a silent swap both read as jarring. Preserve the former, flash the
        # latter.
        scroll_bar = tab.scroll_area.verticalScrollBar()
        scroll_value = scroll_bar.value()
        tab.tree = tree
        tab.disk_fingerprint = file_fingerprint(tab.path)
        tab.dirty = False
        tab.dirtyChanged.emit()
        self._render_view(tab, tab.filter_uid)
        self._update_tab_title(tab)
        self._invalidate_xref()
        QTimer.singleShot(0, lambda: scroll_bar.setValue(scroll_value))
        self._flash_tab_update(tab)
        return True

    # [FN] _flash_tab_update — a brief accent-colored fade over a tab's content, so an externally
    # reloaded file (_reload_tab_from_disk above) reads as "this just updated" instead of a silent,
    # unexplained content swap
    # [FN OPEN] _flash_tab_update
    def _flash_tab_update(self, tab):
        effect = QGraphicsColorizeEffect(tab.view_container)
        effect.setColor(QColor(theme.ACCENT))
        effect.setStrength(0.0)
        tab.view_container.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b'strength', tab.view_container)
        animation.setDuration(700)
        animation.setStartValue(0.55)
        animation.setEndValue(0.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(lambda: tab.view_container.setGraphicsEffect(None))
        # PySide doesn't keep a QPropertyAnimation alive on its own — without a live reference it can
        # be garbage-collected mid-flight, silently stopping the animation partway
        tab._ai_flash_animation = animation
        animation.start()
    # [FN CLOSED] _flash_tab_update

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
        # a save can add/rename/delete KANT elements (new element via "+", a metadata edit, a
        # hand-typed marker) that the left "Codice" tree needs to reflect — don't rely solely on the
        # QFileSystemWatcher noticing an in-place atomic replace of an already-watched file (an
        # os.replace over an existing name doesn't reliably read as a directory change on every
        # OS/Qt backend); arm the same debounce timer directly, exactly like
        # _create_new_file/_prompt_add_file/_create_new_folder/_rename_tree_item already do for
        # their own filesystem mutations
        self.fs_refresh_timer.start(400)

    def _refresh_after_fs_change(self):
        if not self.project_root_path:
            return
        # a caller that just made the change itself (create/rename/delete) calls this directly,
        # right before the QFileSystemWatcher's own directoryChanged signal fires for that same
        # change and arms fs_refresh_timer's 400ms debounce — without stopping it here, that
        # debounced call still fires afterward and redoes the exact same full os.walk rebuild for
        # nothing. Whichever path gets here first now cancels the other's redundant follow-up.
        self.fs_refresh_timer.stop()
        self._invalidate_xref()
        self._rebuild_tree()
        self._check_kant_map(self.project_root_path)
        self._watch_project_tree()

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
        # kept disabled (not re-enabled yet) through the diff-building pass below — same "don't let
        # someone edit a file mid-comparison" reasoning _prepare_ai_snapshot already applies to the
        # snapshot copy, just extended to cover the read side of the diff too
        try:
            review = build_ai_review(self.project_root_path, snapshot, ignored)
        except OSError as error:
            self.tabs.setEnabled(True)
            self._ide_message('Verifica modifiche AI', str(error))
            return
        self.tabs.setEnabled(True)
        if stray_symlinks:
            self.claude_pane.write_info(
                '\nSimlink creati durante la sessione AI rimossi per sicurezza: ' + ', '.join(stray_symlinks)
            )
        if not review:
            discard_snapshot(snapshot)
            self._ai_snapshot = None
            self._clear_ai_snapshot_marker()
            # nothing to accept/reject — no review is pending, so it's safe to do the definitive
            # refresh right away instead of waiting on a resolution that will never come
            self._refresh_after_fs_change()
            if getattr(self.claude_pane, 'validate_after_finish', False):
                self.claude_pane.validate_after_finish = False
                result = self._validate_kant_project()
                if result:
                    self.claude_pane.write_info('\n' + result + '\n')
            return
        summary = '\n'.join(f"- {item['status']}: {item['path']}" for item in review)
        self.claude_pane.write_info(f'\nModifiche AI pronte per il controllo:\n{summary}')

        # [FN CATEGORY] resolved — the user's (or auto-permissions') final accept/reject decision on
        # the pending AI review. Everything here is deliberately gated behind this closure actually
        # firing: no definitive validation, map sync, tree refresh, or xref rebuild happens before
        # it — those used to run unconditionally right after the CLI process exited, racing ahead of
        # a still-unresolved manual review. apply and rollback each get their own exact sequence.
        # [FN] resolved — applies or rolls back the pending AI review, then follows the matching sequence
        # [FN OPEN] resolved
        def resolved(action, accepted, manual_text):
            skipped_dirs = []
            self.tabs.setEnabled(False)
            try:
                if action == 'apply':
                    kept = apply_ai_review(self.project_root_path, review, accepted, manual_text)
                else:
                    skipped_dirs = rollback_snapshot(self.project_root_path, snapshot, ignored)
            except OSError as error:
                self.tabs.setEnabled(True)
                self._ide_message('Revisione AI', f'Operazione incompleta: {error}\nSnapshot conservato in {snapshot}')
                return
            self.tabs.setEnabled(True)
            if skipped_dirs:
                self.claude_pane.write_info(
                    '\nAlcune cartelle non sono state rimosse durante l\'annullamento (probabilmente non vuote): '
                    + ', '.join(skipped_dirs)
                )
            discard_snapshot(snapshot)
            self._ai_snapshot = None
            self._clear_ai_snapshot_marker()
            self._exit_ai_review_mode()
            for tab in list(self.open_tabs.values()):
                # a tab _enter_ai_review_mode put into its merged diff view needs a forced reload
                # here, not the usual fingerprint-gated _on_fs_file_changed: on 'apply' the accepted
                # content is often byte-identical to what was already on disk (no per-hunk selection
                # left to change it), so the fingerprint wouldn't move and the stale read-only diff
                # view would never clear on its own.
                if getattr(tab, '_ai_review_lines', None) is not None:
                    del tab._ai_review_lines
                    if os.path.isfile(tab.path):
                        self._reload_tab_from_disk(tab)
                    else:
                        index = self.tabs.indexOf(tab)
                        if index != -1:
                            self._close_tab(index, flush=False)
                else:
                    self._on_fs_file_changed(tab.path)
            self.claude_pane.validate_after_finish = False
            if action == 'apply':
                normalized, id_skipped = normalize_missing_ids(self.project_root_path, kept)
                # reuse the same result/warnings composition and self-heal logic the manual
                # "Verifica" button uses, just widened to also generate a map that didn't exist yet
                # (a first-time AI tagging run) — never widened to 'marker_invalidi', which must
                # never regenerate the map (kept as the shared default, not passed here)
                result = self._validate_kant_project(extra_sync_states=('assente', 'errore_generazione'))
                self._refresh_after_fs_change()
                result_message = 'Modifiche AI revisionate e applicate'
                if normalized:
                    result_message += f'\nID mancanti persistiti in: {", ".join(normalized)}'
                if id_skipped:
                    skipped_note = '; '.join(f'{rel} ({reason})' for rel, reason in id_skipped)
                    result_message += f'\nNormalizzazione ID saltata per: {skipped_note}'
                self.claude_pane.write_info('\n' + result + '\n')
            else:
                # rollback: only the restore + reload + refresh sequence — no id normalization, no
                # rewrites, no map regen (the snapshot already restored the previous map too)
                self._refresh_after_fs_change()
                result_message = 'Modifiche AI annullate e snapshot ripristinato'
            self.claude_pane.write_info(result_message)
        # [FN CLOSED] resolved

        # automatic mode means the user asked not to be interrupted for permission decisions either
        # — extending that to the review step too: accept everything silently instead of offering a
        # decision nobody's going to make by hand. Non-automatic mode shows the diff live, in place
        # (_enter_ai_review_mode: every changed file re-rendered as one merged, read-only, green/red
        # -marked block, plus the same coloring on that file's tree row) and a small inline chat card
        # with just Accetta/Annulla — no separate review window to open first.
        if self.claude_pane.auto_permissions.isChecked():
            resolved('apply', {item['path']: set(range(len(item['hunks']))) for item in review}, {})
            return
        self._enter_ai_review_mode(review)
        self.claude_pane.offer_ai_review(review, resolved)

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

    # [FN CATEGORY] _prompt_add_file — the "+" button below the project tree in file view: a richer
    # alternative to _create_new_file above (bare filename, empty content) that asks what KIND of
    # file first — see _ide_new_file_form/build_new_file_content (kant/dialogs.py, kant/model.py).
    # Always targets the project root, unlike _create_new_file's context-menu target_dir, since the
    # "+" button isn't attached to any particular folder selection.
    # [FN] _prompt_add_file — the tree's "+ Nuovo file" button click handler
    # [FN OPEN] _prompt_add_file
    def _prompt_add_file(self):
        target_dir = self.project_root_path
        if not target_dir:
            self._ide_message('Nuovo file', 'Apri prima una cartella di progetto.')
            return
        default_language = 'Python'
        result = self._ide_new_file_form(default_language=default_language)
        if result is None:
            return
        name, content = result
        if not is_safe_child_name(name):
            self._ide_message('Nuovo file', 'Usa solo un nome file, senza percorsi.')
            return
        path = os.path.join(target_dir, name)
        if os.path.exists(path):
            self._ide_message('Nuovo file', 'Esiste gia un file o una cartella con questo nome.')
            return
        try:
            write_file_atomic(path, content)
        except OSError as error:
            self._ide_message('Nuovo file', f'Impossibile creare il file: {error}')
            return
        self._refresh_after_fs_change()
        self._open_file(path)
    # [FN CLOSED] _prompt_add_file

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
        self._move_tree_path(old_path, new_path, kind == 'dir', 'Rinomina')

    def _move_tree_path(self, old_path, new_path, is_dir, title):
        if os.path.exists(new_path):
            self._ide_message(title, 'Esiste gia un file o una cartella con questo nome.')
            return False
        affected = [tab for path, tab in self.open_tabs.items()
                    if path == old_path or (is_dir and path.startswith(old_path + os.sep))]
        if any(not tab.flush_pending_save() for tab in affected):
            return False
        try:
            os.rename(old_path, new_path)
        except OSError as error:
            self._ide_message(title, f'Impossibile spostare: {error}')
            return False
        for tab in affected:
            self._retarget_tab(tab, new_path + tab.path[len(old_path):])
        if self.project_root_path:
            old_rel = os.path.relpath(old_path, self.project_root_path).replace(os.sep, '/')
            new_rel = os.path.relpath(new_path, self.project_root_path).replace(os.sep, '/')
            migrate_member_paths(self.project_root_path, old_rel, new_rel, is_dir)
            migrate_position_keys(self.project_root_path, old_rel, new_rel, is_dir)
        self._refresh_after_fs_change()
        return True

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
