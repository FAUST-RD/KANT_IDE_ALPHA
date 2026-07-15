"""Git actions mixed into MainWindow: status refresh, diff/stage, commit, and branch switch.

AI navigation: every method here shells out to the real `git` CLI (via `_run_git`) rather than
parsing .git internals. `self.git_root`/`self.git_status` are set by `_refresh_git_status` and
read (not owned) here; the tree/status-badge rendering that consumes them stays in mainwindow.py.
`GitPanelDialog` is the one-window "everything git" surface opened by clicking the Git button;
the individual menu actions below it stay wired too (right-click tree menu, command palette).
"""
import os
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from kant import theme
from kant.gitutil import find_git_root, git_status_map


_ROLE_PATH = Qt.UserRole + 1


# [CLS CATEGORY] GitPanelDialog — the single-window "everything git" surface: branch switch, a
# flat checkbox list of every changed file (checked = staged), a diff preview for whichever file
# was last clicked, and a commit box, instead of the six separate Git-menu actions each needing
# their own click-and-remember-which-file. Non-modal (like the MAPPA dialog) so it can stay open
# while the user keeps editing; every git call goes through MainWindow._run_git, so this dialog
# owns no git-invocation logic of its own, only the list/diff/commit UI around it.
# [CLS] GitPanelDialog — one window for branch switch, stage/unstage, diff, and commit
# [CLS OPEN] GitPanelDialog
class GitPanelDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._selected_rel = None
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.resize(560, 660)
        self.setStyleSheet(f'QDialog {{ background:{theme.BG}; border:1px solid {theme.BORDER}; }}')

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(34)
        header.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(14, 0, 8, 0)
        title = QLabel('Git')
        title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        title.setStyleSheet(f'color:{theme.TEXT}; letter-spacing:2px; border:none;')
        header_row.addWidget(title)
        header_row.addStretch(1)
        refresh_btn = QPushButton('⟳')
        refresh_btn.setFixedSize(26, 24)
        refresh_btn.setToolTip('Aggiorna')
        refresh_btn.setStyleSheet(theme.BUTTON_STYLE)
        refresh_btn.clicked.connect(self.refresh)
        header_row.addWidget(refresh_btn)
        close_btn = QPushButton('×')
        close_btn.setFixedSize(26, 24)
        close_btn.setToolTip('Chiudi il pannello Git')
        close_btn.setStyleSheet(theme.BUTTON_STYLE)
        close_btn.clicked.connect(self.close)
        header_row.addWidget(close_btn)
        outer.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(14, 12, 14, 12)
        body.setSpacing(8)
        combo_style = (
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:6px;'
        )

        branch_row = QHBoxLayout()
        branch_label = QLabel('Branch:')
        branch_label.setStyleSheet(f'color:{theme.TEXT}; border:none;')
        branch_row.addWidget(branch_label)
        self.branch_combo = QComboBox()
        self.branch_combo.setStyleSheet(combo_style)
        branch_row.addWidget(self.branch_combo, 1)
        switch_btn = QPushButton('Cambia')
        switch_btn.setToolTip('Passa al branch selezionato nell\'elenco')
        switch_btn.setStyleSheet(theme.BUTTON_STYLE)
        switch_btn.clicked.connect(self._switch_branch)
        branch_row.addWidget(switch_btn)
        body.addLayout(branch_row)

        files_label = QLabel('File modificati (spunta = in stage):')
        files_label.setStyleSheet(f'color:{theme.TEXT}; border:none;')
        body.addWidget(files_label)
        self.files_list = QTreeWidget()
        self.files_list.setHeaderHidden(True)
        self.files_list.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:6px;'
        )
        self.files_list.itemChanged.connect(self._on_item_toggled)
        self.files_list.itemClicked.connect(self._on_item_clicked)
        body.addWidget(self.files_list, 2)

        self.diff_view = QPlainTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setFont(QFont('Consolas', 8))
        self.diff_view.setPlaceholderText('Seleziona un file per vedere il diff')
        self.diff_view.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:6px;'
        )
        body.addWidget(self.diff_view, 3)

        message_label = QLabel('Messaggio di commit:')
        message_label.setStyleSheet(f'color:{theme.TEXT}; border:none;')
        body.addWidget(message_label)
        self.message_field = QTextEdit()
        self.message_field.setFixedHeight(60)
        self.message_field.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:6px;'
        )
        body.addWidget(self.message_field)

        commit_row = QHBoxLayout()
        self.status_label = QLabel('')
        self.status_label.setStyleSheet(f'color:{theme.DIM}; border:none;')
        commit_row.addWidget(self.status_label, 1)
        self.commit_btn = QPushButton('Commit')
        self.commit_btn.setToolTip('Crea un commit con i file attualmente in stage e questo messaggio')
        self.commit_btn.setStyleSheet(theme.BUTTON_STYLE)
        self.commit_btn.clicked.connect(self._commit)
        commit_row.addWidget(self.commit_btn)
        body.addLayout(commit_row)

        outer.addLayout(body)

    # [FN CATEGORY] refresh — re-reads branch list/current branch and the full changed-file set
    # (git status --porcelain=v1, parsed here rather than via gitutil.parse_git_status, which
    # collapses " M"/"M " to the same single-char code and can't tell staged from unstaged) —
    # blocks briefly like the commit dialog's "Stage tutto" already does, status is always fast.
    # [FN] refresh — repopulates branch combo and the changed-file list from git status
    # [FN OPEN] refresh
    def refresh(self):
        window = self._window
        if not window.git_root:
            return
        branches_result = window._run_git(['branch', '--format=%(refname:short)'])
        branches = [line.strip() for line in (branches_result.stdout.splitlines() if branches_result else []) if line.strip()]
        current_result = window._run_git(['rev-parse', '--abbrev-ref', 'HEAD'])
        current = current_result.stdout.strip() if current_result else ''
        self.branch_combo.blockSignals(True)
        self.branch_combo.clear()
        self.branch_combo.addItems(branches)
        if current in branches:
            self.branch_combo.setCurrentText(current)
        self.branch_combo.blockSignals(False)

        status_result = window._run_git(['status', '--porcelain=v1', '--untracked-files=normal'])
        staged_paths = set()
        entries = {}
        for line in (status_result.stdout.splitlines() if status_result else []):
            if len(line) < 4:
                continue
            index_ch, work_ch, rel = line[0], line[1], line[3:]
            if ' -> ' in rel:
                rel = rel.rsplit(' -> ', 1)[1]
            code = index_ch if index_ch != ' ' else work_ch
            entries[rel] = code
            if index_ch not in (' ', '?'):
                staged_paths.add(rel)

        self.files_list.blockSignals(True)
        self.files_list.clear()
        for rel in sorted(entries):
            item = QTreeWidgetItem([f'{entries[rel]}  {rel}'])
            item.setData(0, _ROLE_PATH, rel)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if rel in staged_paths else Qt.Unchecked)
            self.files_list.addTopLevelItem(item)
        self.files_list.blockSignals(False)
        self.status_label.setText(f'{len(staged_paths)} in stage, {len(entries) - len(staged_paths)} non in stage')
    # [FN CLOSED] refresh

    def _on_item_toggled(self, item, _column):
        rel = item.data(0, _ROLE_PATH)
        staged = item.checkState(0) == Qt.Checked
        self._window._run_git(['add', '--', rel] if staged else ['restore', '--staged', '--', rel])
        self._window._refresh_after_fs_change()
        self.refresh()

    def _on_item_clicked(self, item, _column):
        rel = item.data(0, _ROLE_PATH)
        self._selected_rel = rel
        result = self._window._run_git(['diff', '--', rel])
        text = result.stdout if result else ''
        if not text.strip():
            cached = self._window._run_git(['diff', '--cached', '--', rel])
            text = cached.stdout if cached else ''
        self.diff_view.setPlainText(text or '(nessuna differenza testuale — file binario o nuovo file vuoto)')

    def _switch_branch(self):
        branch = self.branch_combo.currentText().strip()
        if not branch:
            return
        result = self._window._run_git(['checkout', branch])
        if result is None or result.returncode:
            error = (result.stderr or result.stdout) if result else 'Git non disponibile'
            self.status_label.setText(f'Errore checkout: {error.strip()[:120]}')
            return
        self._window._refresh_after_fs_change()
        self.refresh()

    def _commit(self):
        message = self.message_field.toPlainText().strip()
        if not message:
            self.status_label.setText('Scrivi un messaggio di commit prima.')
            return
        result = self._window._run_git(['commit', '-m', message])
        if result is None or result.returncode:
            error = (result.stderr or result.stdout) if result else 'Git non disponibile'
            self.status_label.setText(f'Errore commit: {error.strip()[:120]}')
            return
        self.message_field.clear()
        self._window._refresh_after_fs_change()
        self.refresh()
# [CLS CLOSED] GitPanelDialog


# [CLS CATEGORY] GitOpsMixin — mixed into MainWindow (alongside IdeDialogsMixin, WorkspaceMixin)
# so git actions live in their own file instead of growing mainwindow.py further; every method
# still reaches MainWindow state (self.git_root, self.terminal, self._run_background, etc.) the
# same as if it were defined directly on the class.
# [CLS] GitOpsMixin — git status/diff/stage/commit/branch actions for MainWindow
# [CLS OPEN] GitOpsMixin
class GitOpsMixin:
    # [FN CATEGORY] _open_git_panel — the Git title-bar button's primary click action. With no
    # repository yet, routes into a `git init` flow
    # instead of just refusing — the panel is useless with nothing to show, but "no repo" is a
    # starting state to walk the user out of, not a dead end. One dialog instance is reused across
    # opens, like map_dialog, so branch/file-list state doesn't have to be rebuilt from scratch.
    # [FN] _open_git_panel — opens the Git panel, or routes to `git init` if there's no repo yet
    # [FN OPEN] _open_git_panel
    def _open_git_panel(self):
        if not self.git_root:
            if not self.project_root_path:
                self._ide_message('Git', 'Apri prima una cartella di progetto.')
                return
            if not self._ide_yes_no('Git', 'Questa cartella non è un repository git. Inizializzarne uno ora?'):
                return
            result = self._run_git(['init'], self.project_root_path)
            if result is None or result.returncode:
                error = (result.stderr or result.stdout) if result else 'Git non disponibile (è installato ed è nel PATH?)'
                self._ide_message('Git', f'Impossibile inizializzare il repository:\n{error}')
                return
            # set synchronously so the panel below can open immediately instead of waiting for the
            # background refresh below to complete its round-trip
            self.git_root = self.project_root_path
            self.git_status = {}
            self._refresh_after_fs_change()
            self._refresh_git_status()
        if self.git_panel is None:
            self.git_panel = GitPanelDialog(self)
        self.git_panel.refresh()
        self.git_panel.show()
        self.git_panel.raise_()
        self.git_panel.activateWindow()
    # [FN CLOSED] _open_git_panel

    def _refresh_git_status(self):
        if self._git_refresh_pending or not self.project_root_path:
            return
        project_root = self.project_root_path
        self._git_refresh_pending = True

        def read_status():
            git_root = find_git_root(project_root)
            return git_root, git_status_map(git_root)

        def apply_status(result, error):
            self._git_refresh_pending = False
            if self.project_root_path != project_root:
                self._refresh_git_status()
                return
            if error:
                return
            self.git_root, self.git_status = result
            self._update_action_buttons()
            self._rebuild_tree(refresh_git=False)

        self._run_background(read_status, apply_status)

    def _git_status_for_path(self, path):
        if not self.git_root:
            return ''
        rel = os.path.relpath(path, self.git_root)
        return self.git_status.get(rel, '')

    def _git_status_for_dir(self, path):
        if not self.git_root:
            return ''
        rel = os.path.relpath(path, self.git_root)
        prefix = '' if rel == '.' else rel + os.sep
        return 'M' if any(p.startswith(prefix) for p in self.git_status) else ''

    def _git_relpath(self, path):
        if not self.git_root or not path:
            return None
        return os.path.relpath(path, self.git_root)

    def _run_git(self, args, git_root=None):
        git_root = git_root or self.git_root
        if not git_root:
            return None
        return subprocess.run(
            ['git', '-C', git_root, *args],
            capture_output=True,
            text=True,
            timeout=8,
        )

    def _git_diff_file(self, path):
        rel = self._git_relpath(path)
        if not rel:
            return
        git_root = self.git_root
        def diff():
            result = self._run_git(['diff', '--', rel], git_root)
            text = result.stdout.strip() if result else ''
            if not text:
                cached = self._run_git(['diff', '--cached', '--', rel], git_root)
                text = cached.stdout.strip() if cached else ''
            return text

        self._run_background(
            diff,
            lambda text, error: self.terminal.write_info(
                f'\n# git diff -- {rel}\n{("Errore: " + str(error)) if error else (text or "Nessuna differenza")}\n'
            ),
        )

    def _git_stage_file(self, path, staged):
        rel = self._git_relpath(path)
        if not rel:
            return
        args = ['add', '--', rel] if staged else ['restore', '--staged', '--', rel]
        action = 'stage' if staged else 'unstage'
        git_root = self.git_root

        def done(result, error):
            if error or result is None or result.returncode:
                message = str(error) if error else ((result.stderr or result.stdout) if result else 'Git non disponibile')
                self.terminal.write_info(f'\n# git {action} {rel}\n{message}\n')
                return
            self._refresh_after_fs_change()
            self.terminal.write_info(f'\n# git {action} {rel}: OK\n')

        self._run_background(lambda: self._run_git(args, git_root), done)

    def _active_file_path(self):
        tab = self.active_tab
        return tab.path if tab is not None else None

    def _git_refresh(self):
        self._refresh_after_fs_change()
        self.terminal.write_info('\n# Git refresh: OK\n')

    def _git_diff_active_file(self):
        path = self._active_file_path()
        if path:
            self._git_diff_file(path)

    def _git_stage_active_file(self):
        path = self._active_file_path()
        if path:
            self._git_stage_file(path, staged=True)

    def _git_unstage_active_file(self):
        path = self._active_file_path()
        if path:
            self._git_stage_file(path, staged=False)

    # [FN CATEGORY] _git_commit — reads the staged-file list fresh (git diff --cached --name-only)
    # before opening the dialog rather than trusting self.git_status, since that map collapses " M"
    # (unstaged) and "M " (staged) to the same single-char code and can't tell them apart.
    # [FN] _git_commit — opens the commit dialog and runs `git commit -m`
    # [FN OPEN] _git_commit
    def _git_commit(self):
        if not self.git_root:
            return
        result = self._run_git(['diff', '--cached', '--name-only'])
        staged = [line for line in (result.stdout.splitlines() if result else []) if line.strip()]
        message = self._ide_git_commit_form(staged)
        if not message:
            return
        result = self._run_git(['commit', '-m', message])
        if result is None or result.returncode:
            error = (result.stderr or result.stdout) if result else 'Git non disponibile'
            self.terminal.write_info(f'\n# git commit\n{error}\n')
            return
        self._refresh_after_fs_change()
        self.terminal.write_info(f'\n# git commit: OK\n{result.stdout}\n')
    # [FN CLOSED] _git_commit

    # [FN CATEGORY] _git_switch_branch — lists local branches (git branch --format), reuses the
    # existing combo-box picker dialog (_ide_item) instead of a bespoke one, then checks out the pick.
    # [FN] _git_switch_branch — branch picker + `git checkout`
    # [FN OPEN] _git_switch_branch
    def _git_switch_branch(self):
        if not self.git_root:
            return
        result = self._run_git(['branch', '--format=%(refname:short)'])
        branches = [line.strip() for line in (result.stdout.splitlines() if result else []) if line.strip()]
        if not branches:
            self.terminal.write_info('\n# git branch\nNessun branch trovato\n')
            return
        branch, ok = self._ide_item('Cambia branch', 'Branch:', branches)
        if not ok or not branch:
            return
        result = self._run_git(['checkout', branch])
        if result is None or result.returncode:
            error = (result.stderr or result.stdout) if result else 'Git non disponibile'
            self.terminal.write_info(f'\n# git checkout {branch}\n{error}\n')
            return
        self._refresh_after_fs_change()
        self.terminal.write_info(f'\n# git checkout {branch}: OK\n')
    # [FN CLOSED] _git_switch_branch
# [CLS CLOSED] GitOpsMixin
