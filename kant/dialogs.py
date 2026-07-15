"""Small themed modal dialogs shared by the main window."""
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from kant import theme


# [FN CATEGORY] _PaletteInput — a QLineEdit that forwards Up/Down to the command palette's result
# list (wrapping at the ends) instead of the default no-op text-cursor movement, so arrow keys work
# while the filter field keeps focus — the same forwarding shape _TabLabel (mainwindow.py) uses.
# [FN] _PaletteInput — filter field that forwards Up/Down to a QListWidget
# [FN OPEN] _PaletteInput
class _PaletteInput(QLineEdit):
    def __init__(self, listbox):
        super().__init__()
        self._listbox = listbox

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Down, Qt.Key_Up) and self._listbox.count():
            row = self._listbox.currentRow()
            delta = 1 if event.key() == Qt.Key_Down else -1
            self._listbox.setCurrentRow((row + delta) % self._listbox.count())
            return
        super().keyPressEvent(event)
# [FN CLOSED] _PaletteInput


class IdeDialogsMixin:
    def _dialog(self, title, message, width=460):
        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.setMinimumWidth(width)
        dialog.setStyleSheet(
            f'QDialog {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; }} '
            f'QLabel {{ color:{theme.TEXT}; }}'
        )
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setFont(QFont('Consolas', theme.TREE_FONT_PT + 2, QFont.DemiBold))
        heading.setStyleSheet(f'color:{theme.WARN};')
        layout.addWidget(heading)
        prompt = QLabel(message)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        return dialog, layout

    def _dialog_buttons(self, layout, dialog):
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton('Annulla')
        cancel.setToolTip('Chiudi senza applicare')
        ok = QPushButton('OK')
        ok.setToolTip('Conferma e applica')
        for button in (cancel, ok):
            button.setStyleSheet(theme.BUTTON_STYLE)
            row.addWidget(button)
        cancel.clicked.connect(dialog.reject)
        ok.clicked.connect(dialog.accept)
        layout.addLayout(row)

    def _ide_choice(self, title, message, choices):
        """choices: (label, value) pairs, or (label, value, tooltip) triples for a consequential
        choice where the label alone doesn't make what happens obvious."""
        dialog, layout = self._dialog(title, message)
        layout.setSpacing(14)
        result = {'value': None}
        row = QHBoxLayout()
        row.addStretch(1)

        def choose(value):
            result['value'] = value
            dialog.accept()

        for choice in choices:
            label, value, *tooltip = choice
            button = QPushButton(label)
            if tooltip:
                button.setToolTip(tooltip[0])
            button.setStyleSheet(theme.BUTTON_STYLE)
            button.clicked.connect(lambda _checked=False, selected=value: choose(selected))
            row.addWidget(button)
        layout.addLayout(row)
        return result['value'] if dialog.exec() == QDialog.Accepted else None

    def _ide_yes_no(self, title, message):
        return self._ide_choice(title, message, [('No', False), ('Si', True)]) is True

    def _ide_message(self, title, message):
        self._ide_choice(title, message, [('OK', True)])

    def _ide_text(self, title, label, text=''):
        dialog, layout = self._dialog(title, label)
        field = QLineEdit(text)
        field.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:8px;'
        )
        layout.addWidget(field)
        self._dialog_buttons(layout, dialog)
        field.selectAll()
        field.setFocus()
        return (field.text(), True) if dialog.exec() == QDialog.Accepted else ('', False)

    # [FN CATEGORY] _ide_metadata_form — the ⋮ button's metadata editor: one internal window (framed
    # header bar matching the MAPPA dialog's look, not a native title bar) with all three fields
    # together, instead of three sequential single-field prompts.
    # [FN] _ide_metadata_form — tag/name/short-description editor in a single dialog
    # [FN OPEN] _ide_metadata_form
    def _ide_metadata_form(self, tag, name, desc):
        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.setFixedWidth(420)
        dialog.setStyleSheet(f'QDialog {{ background:{theme.BG}; border:1px solid {theme.BORDER}; }}')

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(34)
        header.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(14, 0, 8, 0)
        title = QLabel('Metadati KANT')
        title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        title.setStyleSheet(f'color:{theme.TEXT}; letter-spacing:2px; border:none;')
        header_row.addWidget(title)
        header_row.addStretch(1)
        close_btn = QPushButton('×')
        close_btn.setFixedSize(26, 24)
        close_btn.setToolTip('Chiudi senza salvare')
        close_btn.setStyleSheet(theme.BUTTON_STYLE)
        close_btn.clicked.connect(dialog.reject)
        header_row.addWidget(close_btn)
        outer.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(18, 16, 18, 16)
        body.setSpacing(10)

        def field_row(label_text, value):
            field_label = QLabel(label_text)
            field_label.setStyleSheet(f'color:{theme.TEXT}; border:none;')
            body.addWidget(field_label)
            field = QLineEdit(value)
            field.setStyleSheet(
                f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
                f'border-radius:6px; padding:6px;'
            )
            body.addWidget(field)
            return field

        tag_field = field_row('Tag:', tag)
        name_field = field_row('Nome tecnico:', name)
        desc_field = field_row('Descrizione breve:', desc)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton('Annulla')
        cancel.setToolTip('Chiudi senza salvare le modifiche ai metadati')
        ok = QPushButton('OK')
        ok.setToolTip('Salva tag, nome e descrizione')
        for button in (cancel, ok):
            button.setStyleSheet(theme.BUTTON_STYLE)
            buttons.addWidget(button)
        cancel.clicked.connect(dialog.reject)
        ok.clicked.connect(dialog.accept)
        body.addLayout(buttons)
        outer.addLayout(body)

        tag_field.selectAll()
        tag_field.setFocus()
        if dialog.exec() != QDialog.Accepted:
            return None
        return tag_field.text(), name_field.text(), desc_field.text()
    # [FN CLOSED] _ide_metadata_form

    # [FN CATEGORY] _ide_agent_choice_form — the /kant-code-map launch prompt: provider, specific
    # model, and reasoning effort together in one internal window instead of a plain 3-button
    # choice, with an explicit Cancel. Model lists and the "no override" sentinel are passed in by
    # the caller (mainwindow.py, which already imports them from widgets.py) rather than imported
    # here, so this stays independent of widgets.py.
    # [FN] _ide_agent_choice_form — provider/model/effort picker with Cancel
    # [FN OPEN] _ide_agent_choice_form
    def _ide_agent_choice_form(self, claude_models, codex_models, model_default):
        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.setFixedWidth(420)
        dialog.setStyleSheet(f'QDialog {{ background:{theme.BG}; border:1px solid {theme.BORDER}; }}')

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(34)
        header.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(14, 0, 8, 0)
        title = QLabel('Applica /kant-code-map')
        title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        title.setStyleSheet(f'color:{theme.TEXT}; letter-spacing:1px; border:none;')
        header_row.addWidget(title)
        header_row.addStretch(1)
        close_btn = QPushButton('×')
        close_btn.setFixedSize(26, 24)
        close_btn.setToolTip('Annulla senza avviare')
        close_btn.setStyleSheet(theme.BUTTON_STYLE)
        close_btn.clicked.connect(dialog.reject)
        header_row.addWidget(close_btn)
        outer.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(18, 16, 18, 16)
        body.setSpacing(10)
        combo_style = (
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:6px;'
        )

        def field_label(text):
            label = QLabel(text)
            label.setStyleSheet(f'color:{theme.TEXT}; border:none;')
            body.addWidget(label)

        field_label('Provider:')
        provider_combo = QComboBox()
        provider_combo.addItem('Claude Code', 'claude')
        provider_combo.addItem('Codex', 'codex')
        provider_combo.setStyleSheet(combo_style)
        body.addWidget(provider_combo)

        field_label('Modello:')
        model_combo = QComboBox()
        model_combo.setEditable(True)
        model_combo.setStyleSheet(combo_style)
        body.addWidget(model_combo)

        # both CLIs really do have an effort/reasoning-effort parameter (checked against `claude
        # --help` and codex's -c model_reasoning_effort=<level> config override), just under
        # different mechanisms — _agent_command (widgets.py) applies each correctly per provider
        field_label('Effort:')
        effort_combo = QComboBox()
        effort_combo.setEditable(True)
        effort_combo.setStyleSheet(combo_style)
        body.addWidget(effort_combo)
        effort_levels = {
            'claude': (model_default, 'low', 'medium', 'high', 'xhigh', 'max'),
            'codex': (model_default, 'low', 'medium', 'high'),
        }

        def sync_for_provider():
            provider = provider_combo.currentData()
            model_combo.clear()
            model_combo.addItems(codex_models if provider == 'codex' else claude_models)
            effort_combo.clear()
            effort_combo.addItems(effort_levels[provider])

        provider_combo.currentIndexChanged.connect(sync_for_provider)
        sync_for_provider()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton('Annulla')
        cancel.setToolTip('Annulla senza avviare')
        ok = QPushButton('Avvia')
        ok.setToolTip('Avvia /kant-code-map con il provider, modello ed effort scelti')
        for button in (cancel, ok):
            button.setStyleSheet(theme.BUTTON_STYLE)
            buttons.addWidget(button)
        cancel.clicked.connect(dialog.reject)
        ok.clicked.connect(dialog.accept)
        body.addLayout(buttons)
        outer.addLayout(body)

        if dialog.exec() != QDialog.Accepted:
            return None
        model = model_combo.currentText().strip()
        effort = effort_combo.currentText().strip()
        return {
            'agent': provider_combo.currentData(),
            'model': None if model in (model_default, '') else model,
            'effort': None if effort in (model_default, '') else effort,
        }
    # [FN CLOSED] _ide_agent_choice_form

    # [FN CATEGORY] _ide_git_commit_form — lists currently staged files (passed in by the caller, a
    # fresh `git diff --cached --name-only`) and a message box. "Stage tutto" re-queries the staged
    # list in place (git add -A, then re-read) instead of closing the dialog, so a commit with no
    # per-file staging done beforehand is still one dialog, not stage-then-reopen-commit.
    # [FN] _ide_git_commit_form — staged-file list + message box, with a stage-all shortcut
    # [FN OPEN] _ide_git_commit_form
    def _ide_git_commit_form(self, staged_files):
        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.setFixedWidth(460)
        dialog.setStyleSheet(f'QDialog {{ background:{theme.BG}; border:1px solid {theme.BORDER}; }}')

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(34)
        header.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(14, 0, 8, 0)
        title = QLabel('Git commit')
        title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        title.setStyleSheet(f'color:{theme.TEXT}; letter-spacing:2px; border:none;')
        header_row.addWidget(title)
        header_row.addStretch(1)
        close_btn = QPushButton('×')
        close_btn.setFixedSize(26, 24)
        close_btn.setToolTip('Annulla senza fare commit')
        close_btn.setStyleSheet(theme.BUTTON_STYLE)
        close_btn.clicked.connect(dialog.reject)
        header_row.addWidget(close_btn)
        outer.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(18, 16, 18, 16)
        body.setSpacing(10)

        files_label = QLabel()
        files_label.setWordWrap(True)
        files_label.setStyleSheet(f'color:{theme.DIM}; border:none;')
        body.addWidget(files_label)

        def render_staged(files):
            files_label.setText('File in stage:\n' + '\n'.join(files) if files else 'Nessun file in stage.')

        render_staged(staged_files)

        stage_all_btn = QPushButton('Stage tutto')
        stage_all_btn.setToolTip('Aggiunge tutti i file modificati alla staging area (git add -A)')
        stage_all_btn.setStyleSheet(theme.BUTTON_STYLE)

        def stage_all():
            self._run_git(['add', '-A'])
            result = self._run_git(['diff', '--cached', '--name-only'])
            files = [line for line in (result.stdout.splitlines() if result else []) if line.strip()]
            render_staged(files)
            commit_btn.setEnabled(bool(files))

        stage_all_btn.clicked.connect(stage_all)
        body.addWidget(stage_all_btn)

        message_label = QLabel('Messaggio di commit:')
        message_label.setStyleSheet(f'color:{theme.TEXT}; border:none;')
        body.addWidget(message_label)
        message_field = QTextEdit()
        message_field.setFixedHeight(80)
        message_field.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:6px;'
        )
        body.addWidget(message_field)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton('Annulla')
        cancel.setToolTip('Annulla senza fare commit')
        commit_btn = QPushButton('Commit')
        commit_btn.setToolTip('Crea un commit con i file in stage e questo messaggio')
        for button in (cancel, commit_btn):
            button.setStyleSheet(theme.BUTTON_STYLE)
            buttons.addWidget(button)
        commit_btn.setEnabled(bool(staged_files))
        cancel.clicked.connect(dialog.reject)
        commit_btn.clicked.connect(dialog.accept)
        body.addLayout(buttons)
        outer.addLayout(body)

        message_field.setFocus()
        if dialog.exec() != QDialog.Accepted:
            return None
        return message_field.toPlainText().strip() or None
    # [FN CLOSED] _ide_git_commit_form

    # [FN CATEGORY] _ide_command_palette — `entries` is a caller-built [(label, payload), ...] list
    # (the caller decides what payload means — mainwindow passes QActions and calls .trigger() on the
    # pick, reusing every menu action's existing wiring instead of a separate hand-maintained
    # registry). Filters case-insensitively as you type; Enter/double-click confirms the current row.
    # [FN] _ide_command_palette — fuzzy-filtered command list, returns the picked payload or None
    # [FN OPEN] _ide_command_palette
    def _ide_command_palette(self, entries):
        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.setFixedWidth(460)
        dialog.setStyleSheet(f'QDialog {{ background:{theme.BG}; border:1px solid {theme.BORDER}; }}')

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(34)
        header.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(14, 0, 8, 0)
        title = QLabel('Comandi')
        title.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        title.setStyleSheet(f'color:{theme.TEXT}; letter-spacing:2px; border:none;')
        header_row.addWidget(title)
        header_row.addStretch(1)
        close_btn = QPushButton('×')
        close_btn.setFixedSize(26, 24)
        close_btn.setToolTip('Chiudi la palette comandi (Esc)')
        close_btn.setStyleSheet(theme.BUTTON_STYLE)
        close_btn.clicked.connect(dialog.reject)
        header_row.addWidget(close_btn)
        outer.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(8)

        listbox = QListWidget()
        listbox.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:6px;'
        )

        field = _PaletteInput(listbox)
        field.setPlaceholderText('Filtra comandi…')
        field.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:8px;'
        )
        body.addWidget(field)
        body.addWidget(listbox)

        def populate(filter_text):
            listbox.clear()
            needle = filter_text.strip().lower()
            for label, payload in entries:
                if needle and needle not in label.lower():
                    continue
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, payload)
                listbox.addItem(item)
            if listbox.count():
                listbox.setCurrentRow(0)

        populate('')
        field.textChanged.connect(populate)

        result = {'payload': None}

        def confirm():
            item = listbox.currentItem()
            if item is not None:
                result['payload'] = item.data(Qt.UserRole)
            dialog.accept()

        field.returnPressed.connect(confirm)
        listbox.itemActivated.connect(lambda _item: confirm())
        outer.addLayout(body)

        field.setFocus()
        if dialog.exec() != QDialog.Accepted:
            return None
        return result['payload']
    # [FN CLOSED] _ide_command_palette

    def _ide_item(self, title, label, items):
        if not items:
            return '', False
        dialog, layout = self._dialog(title, label, width=520)
        combo = QComboBox()
        combo.addItems(items)
        combo.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:6px;'
        )
        layout.addWidget(combo)
        self._dialog_buttons(layout, dialog)
        return (combo.currentText(), True) if dialog.exec() == QDialog.Accepted else ('', False)

    # [FN CATEGORY] _ide_python_interpreter_form — detected venvs (kant.pyenv.detect_venvs) listed
    # first with the currently-configured one pre-selected, plus "Sfoglia..." for anything not
    # auto-detected (a venv outside the project, a pyenv/conda install, etc.) — the browse dialog
    # itself is a QFileDialog, an OS-native file picker rather than a themed one, so it isn't
    # counted as one of the "entering credentials into a form" cases needing extra care here.
    # [FN] _ide_python_interpreter_form — pick a detected venv or browse for any interpreter
    # [FN OPEN] _ide_python_interpreter_form
    def _ide_python_interpreter_form(self, candidates, current):
        dialog, layout = self._dialog(
            'Interprete Python', 'Scegli l\'interprete/venv per questo progetto:', width=520,
        )
        listbox = QListWidget()
        listbox.setStyleSheet(
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:6px;'
        )
        for path in candidates:
            item = QListWidgetItem(path)
            listbox.addItem(item)
            if current and os.path.abspath(path) == os.path.abspath(current):
                listbox.setCurrentItem(item)
        if current and all(os.path.abspath(current) != os.path.abspath(p) for p in candidates):
            item = QListWidgetItem(current)
            listbox.addItem(item)
            listbox.setCurrentItem(item)
        if listbox.currentRow() < 0 and listbox.count():
            listbox.setCurrentRow(0)
        layout.addWidget(listbox)

        browse_row = QHBoxLayout()
        browse_row.addStretch(1)
        browse_btn = QPushButton('Sfoglia...')
        browse_btn.setToolTip("Scegli un eseguibile Python non rilevato automaticamente (venv esterno, pyenv, conda...)")
        browse_btn.setStyleSheet(theme.BUTTON_STYLE)

        def browse():
            path, _filter = QFileDialog.getOpenFileName(dialog, 'Scegli l\'eseguibile Python')
            if path:
                item = QListWidgetItem(path)
                listbox.addItem(item)
                listbox.setCurrentItem(item)

        browse_btn.clicked.connect(browse)
        browse_row.addWidget(browse_btn)
        layout.addLayout(browse_row)

        self._dialog_buttons(layout, dialog)
        if dialog.exec() != QDialog.Accepted:
            return None
        chosen = listbox.currentItem()
        return chosen.text() if chosen is not None else None
    # [FN CLOSED] _ide_python_interpreter_form
