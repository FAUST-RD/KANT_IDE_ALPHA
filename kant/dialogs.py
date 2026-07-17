"""Small themed modal dialogs shared by the main window."""
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from kant import theme
from kant.model import (
    ELEMENT_LANGUAGES, ELEMENT_TAG_LABELS, element_skeleton, FILE_KIND_LABELS, build_new_file_content,
)


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
    def _dialog(self, title, message, width=460, accent=False):
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
        # WARN (a distinct purple) is the default "this is a decision point" heading color shared
        # by every _ide_choice/_ide_yes_no caller (discard changes, git init, ...); accent=True is
        # an opt-in for the few that should read as on-brand rather than as a warning — the app-close
        # confirmation (_confirm_close) specifically, on request, not every confirmation dialog
        heading.setStyleSheet(f'color:{theme.ACCENT if accent else theme.WARN};')
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
        ok.setDefault(True)  # Enter in a field submits, matching every native dialog's convention
        for button in (cancel, ok):
            button.setStyleSheet(theme.BUTTON_STYLE)
            row.addWidget(button)
        cancel.clicked.connect(dialog.reject)
        ok.clicked.connect(dialog.accept)
        layout.addLayout(row)

    # [FN CATEGORY] _internal_window — the shared chrome every "internal window" dialog in this
    # file builds: a frameless modal with its own Consolas title + × (rejects), not the OS's native
    # title bar — same look as the Git panel/MAPPA dialog (kant/gitops.py, kant/mappa.py). Was
    # hand-duplicated in 6 places (each with the exact same 25 lines, differing only in title/
    # width/tooltip) before this existed.
    # [FN] _internal_window — builds the header chrome; returns (dialog, outer_layout, body_layout)
    # [FN OPEN] _internal_window
    def _internal_window(self, title, width, close_tooltip='Chiudi senza salvare'):
        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.setFixedWidth(width)
        dialog.setStyleSheet(f'QDialog {{ background:{theme.BG}; border:1px solid {theme.BORDER}; }}')

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(34)
        header.setStyleSheet(f'background:{theme.PANEL}; border-bottom:1px solid {theme.BORDER};')
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(14, 0, 8, 0)
        title_label = QLabel(title)
        title_label.setFont(QFont('Consolas', theme.CODE_FONT_PT, QFont.DemiBold))
        title_label.setStyleSheet(f'color:{theme.TEXT}; letter-spacing:2px; border:none;')
        header_row.addWidget(title_label)
        header_row.addStretch(1)
        close_btn = QPushButton('×')
        close_btn.setFixedSize(26, 24)
        close_btn.setToolTip(close_tooltip)
        close_btn.setStyleSheet(theme.BUTTON_STYLE)
        close_btn.clicked.connect(dialog.reject)
        header_row.addWidget(close_btn)
        outer.addWidget(header)

        return dialog, outer, QVBoxLayout()
    # [FN CLOSED] _internal_window

    def _ide_choice(self, title, message, choices, accent=False):
        """choices: (label, value) pairs, or (label, value, tooltip) triples for a consequential
        choice where the label alone doesn't make what happens obvious."""
        dialog, layout = self._dialog(title, message, accent=accent)
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

    def _ide_yes_no(self, title, message, accent=False):
        return self._ide_choice(title, message, [('No', False), ('Si', True)], accent=accent) is True

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
        dialog, outer, body = self._internal_window('Metadati KANT', 420, 'Chiudi senza salvare')
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
        ok.setDefault(True)
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

    # [FN CATEGORY] _ide_new_element_form — the "+" block at the bottom of a KANT outline (and the
    # equivalent one for whole new files) asks four things: what kind of element (the 8 KANT tags,
    # shown by name not bare code), what language (determines both the comment leader for the
    # marker lines and the generated code's actual syntax), name, and a short description. A live
    # preview re-renders on every keystroke/selection change so the user sees exactly what they're
    # about to get before committing — this is deliberately not a bare name prompt.
    # [FN] _ide_new_element_form — tag/language/name/description picker with a live code preview
    # [FN OPEN] _ide_new_element_form
    def _ide_new_element_form(self, default_tag='FN', default_language='Python'):
        dialog, outer, body = self._internal_window('Nuovo elemento', 520, 'Chiudi senza creare')
        body.setContentsMargins(18, 16, 18, 16)
        body.setSpacing(8)

        field_style = (
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:6px;'
        )

        def field_label(text):
            label = QLabel(text)
            label.setStyleSheet(f'color:{theme.TEXT}; border:none; margin-top:4px;')
            body.addWidget(label)

        field_label('Tipo di elemento:')
        tag_box = QComboBox()
        for tag, label in ELEMENT_TAG_LABELS.items():
            tag_box.addItem(f'{label}  ·  {tag}', tag)
        tag_box.setCurrentIndex(max(0, tag_box.findData(default_tag)))
        tag_box.setStyleSheet(field_style)
        body.addWidget(tag_box)

        field_label('Linguaggio (determina la sintassi generata):')
        lang_box = QComboBox()
        lang_box.addItems(list(ELEMENT_LANGUAGES))
        lang_box.setCurrentText(default_language)
        lang_box.setStyleSheet(field_style)
        body.addWidget(lang_box)

        field_label('Nome tecnico:')
        name_field = QLineEdit()
        name_field.setPlaceholderText('es. calcola_totale')
        name_field.setStyleSheet(field_style)
        body.addWidget(name_field)

        field_label('Descrizione breve:')
        desc_field = QLineEdit()
        desc_field.setPlaceholderText('cosa fa questo elemento, in poche parole')
        desc_field.setStyleSheet(field_style)
        body.addWidget(desc_field)

        field_label('Anteprima:')
        preview = QTextEdit()
        preview.setReadOnly(True)
        preview.setFixedHeight(110)
        preview.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        preview.setStyleSheet(field_style)
        body.addWidget(preview)

        def refresh_preview():
            tag = tag_box.currentData()
            language = lang_box.currentText()
            name = name_field.text().strip() or 'nome'
            preview.setPlainText(element_skeleton(tag, name, language))

        tag_box.currentIndexChanged.connect(refresh_preview)
        lang_box.currentIndexChanged.connect(refresh_preview)
        name_field.textChanged.connect(refresh_preview)
        refresh_preview()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton('Annulla')
        cancel.setToolTip('Chiudi senza creare nulla')
        ok = QPushButton('Crea')
        ok.setToolTip('Crea il nuovo elemento con questi parametri')
        ok.setDefault(True)
        for button in (cancel, ok):
            button.setStyleSheet(theme.BUTTON_STYLE)
            buttons.addWidget(button)
        cancel.clicked.connect(dialog.reject)
        ok.clicked.connect(dialog.accept)
        body.addLayout(buttons)
        outer.addLayout(body)

        name_field.setFocus()
        if dialog.exec() != QDialog.Accepted:
            return None
        name = name_field.text().strip()
        if not name:
            return None
        return tag_box.currentData(), name, desc_field.text().strip(), lang_box.currentText()
    # [FN CLOSED] _ide_new_element_form

    # [FN CATEGORY] _ide_new_file_form — the "+" button at the bottom of the project tree's file
    # view. Same shape as _ide_new_element_form (kind, language, name, live preview) one level up:
    # instead of picking a KANT tag for an element inside an already-open file, this picks a KIND
    # of file to create from scratch (FILE_KIND_LABELS) — the three KANT-tagged kinds reuse the
    # exact same element machinery, plus README/.gitignore/empty for what a project needs besides
    # tagged source.
    # [FN] _ide_new_file_form — kind/language/filename picker with a live content preview
    # [FN OPEN] _ide_new_file_form
    def _ide_new_file_form(self, default_language='Python'):
        dialog, outer, body = self._internal_window('Nuovo file', 520, 'Chiudi senza creare')
        body.setContentsMargins(18, 16, 18, 16)
        body.setSpacing(8)

        field_style = (
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:6px;'
        )

        def field_label(text):
            label = QLabel(text)
            label.setStyleSheet(f'color:{theme.TEXT}; border:none; margin-top:4px;')
            body.addWidget(label)

        field_label('Tipo di file:')
        kind_box = QComboBox()
        for kind, label in FILE_KIND_LABELS.items():
            kind_box.addItem(label, kind)
        kind_box.setStyleSheet(field_style)
        body.addWidget(kind_box)

        field_label('Linguaggio (determina sintassi/estensione consigliata):')
        lang_box = QComboBox()
        lang_box.addItems(list(ELEMENT_LANGUAGES))
        lang_box.setCurrentText(default_language)
        lang_box.setStyleSheet(field_style)
        body.addWidget(lang_box)

        field_label('Nome del file:')
        name_field = QLineEdit()
        name_field.setStyleSheet(field_style)
        body.addWidget(name_field)

        field_label('Anteprima:')
        preview = QTextEdit()
        preview.setReadOnly(True)
        preview.setFixedHeight(130)
        preview.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        preview.setStyleSheet(field_style)
        body.addWidget(preview)

        _name_is_default = [True]  # whether the user has hand-edited the filename yet

        def suggested_name():
            kind = kind_box.currentData()
            language = lang_box.currentText()
            ext = ELEMENT_LANGUAGES.get(language, ELEMENT_LANGUAGES['Generico'])['ext']
            if kind == 'readme':
                return 'README.md'
            if kind == 'gitignore':
                return '.gitignore'
            if kind == 'empty':
                return f'nuovo{ext}'
            return f'nuovo_modulo{ext}' if kind == 'module' else f'{kind}{ext}'

        def refresh_preview():
            if _name_is_default[0]:
                name_field.blockSignals(True)
                name_field.setText(suggested_name())
                name_field.blockSignals(False)
            kind = kind_box.currentData()
            language = lang_box.currentText()
            stem = os.path.splitext(name_field.text().strip() or 'nuovo')[0]
            preview.setPlainText(build_new_file_content(kind, language, stem) or '(file vuoto)')

        def name_hand_edited():
            _name_is_default[0] = False
            refresh_preview()

        kind_box.currentIndexChanged.connect(refresh_preview)
        lang_box.currentIndexChanged.connect(refresh_preview)
        name_field.textEdited.connect(name_hand_edited)
        refresh_preview()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton('Annulla')
        cancel.setToolTip('Chiudi senza creare nulla')
        ok = QPushButton('Crea')
        ok.setToolTip('Crea il file con questi parametri')
        ok.setDefault(True)
        for button in (cancel, ok):
            button.setStyleSheet(theme.BUTTON_STYLE)
            buttons.addWidget(button)
        cancel.clicked.connect(dialog.reject)
        ok.clicked.connect(dialog.accept)
        body.addLayout(buttons)
        outer.addLayout(body)

        if dialog.exec() != QDialog.Accepted:
            return None
        name = name_field.text().strip()
        if not name:
            return None
        kind = kind_box.currentData()
        language = lang_box.currentText()
        stem = os.path.splitext(name)[0]
        return name, build_new_file_content(kind, language, stem)
    # [FN CLOSED] _ide_new_file_form

    # [FN CATEGORY] _ide_agent_choice_form — the /kant-code-map launch prompt: provider, specific
    # model, and reasoning effort together in one internal window instead of a plain 3-button
    # choice, with an explicit Cancel. Model lists and the "no override" sentinel are passed in by
    # the caller (mainwindow.py, which already imports them from widgets.py) rather than imported
    # here, so this stays independent of widgets.py.
    # [FN] _ide_agent_choice_form — provider/model/effort picker with Cancel
    # [FN OPEN] _ide_agent_choice_form
    def _ide_agent_choice_form(self, claude_models, codex_models, model_default):
        dialog, outer, body = self._internal_window('Applica /kant-code-map', 420, 'Annulla senza avviare')
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
        ok.setDefault(True)
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
        dialog, outer, body = self._internal_window('Git commit', 460, 'Annulla senza fare commit')
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
        commit_btn.setDefault(True)
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
        dialog, outer, body = self._internal_window('Comandi', 460, 'Chiudi la palette comandi (Esc)')
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

    # [FN CATEGORY] _ide_new_grouping_form — a grouping bundles elements from anywhere in the
    # project (any tag, any file, any parent) under one name — this picker is name + a filterable,
    # checkable list of every element the caller hands in. `elements` is [(key, tag, desc, file),
    # ...]; this dialog only presents/filters/collects the checked keys, kant/groupings.py owns
    # what a valid key means and how it round-trips.
    # [FN] _ide_new_grouping_form — grouping name + filterable multi-select element picker
    # [FN OPEN] _ide_new_grouping_form
    def _ide_new_grouping_form(self, elements, preselected=()):
        dialog, outer, body = self._internal_window('Nuovo gruppo', 480, 'Chiudi senza creare')
        body.setContentsMargins(18, 14, 18, 14)
        body.setSpacing(8)

        field_style = (
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:6px;'
        )

        name_label = QLabel('Nome del gruppo:')
        name_label.setStyleSheet(f'color:{theme.TEXT}; border:none;')
        body.addWidget(name_label)
        name_field = QLineEdit()
        name_field.setPlaceholderText('es. Autenticazione')
        name_field.setStyleSheet(field_style)
        body.addWidget(name_field)

        members_label = QLabel(f'Elementi da includere ({len(elements)} nel progetto):')
        members_label.setStyleSheet(f'color:{theme.TEXT}; border:none; margin-top:4px;')
        body.addWidget(members_label)

        listbox = QListWidget()
        listbox.setStyleSheet(field_style)
        listbox.setMinimumHeight(220)
        preselected = set(preselected)
        for key, tag, desc, file in elements:
            item = QListWidgetItem(f'[{tag}] {desc}  —  {file}')
            item.setData(Qt.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if key in preselected else Qt.Unchecked)
            listbox.addItem(item)

        filter_field = _PaletteInput(listbox)
        filter_field.setPlaceholderText('Filtra per tag, nome o file…')
        filter_field.setStyleSheet(field_style)

        def apply_filter(text):
            needle = text.strip().lower()
            for i in range(listbox.count()):
                row = listbox.item(i)
                row.setHidden(bool(needle) and needle not in row.text().lower())

        filter_field.textChanged.connect(apply_filter)
        body.addWidget(filter_field)
        body.addWidget(listbox)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton('Annulla')
        cancel.setToolTip('Chiudi senza creare nulla')
        ok = QPushButton('Crea')
        ok.setToolTip('Crea il gruppo con gli elementi selezionati')
        ok.setDefault(True)
        for button in (cancel, ok):
            button.setStyleSheet(theme.BUTTON_STYLE)
            buttons.addWidget(button)
        cancel.clicked.connect(dialog.reject)
        ok.clicked.connect(dialog.accept)
        body.addLayout(buttons)
        outer.addLayout(body)

        name_field.setFocus()
        if dialog.exec() != QDialog.Accepted:
            return None
        name = name_field.text().strip()
        if not name:
            return None
        selected_keys = [
            listbox.item(i).data(Qt.UserRole) for i in range(listbox.count())
            if listbox.item(i).checkState() == Qt.Checked
        ]
        return name, selected_keys
    # [FN CLOSED] _ide_new_grouping_form

    # [FN CATEGORY] _ide_new_project_form — the welcome page's "+" button: name, where to create it,
    # primary language (reuses ELEMENT_LANGUAGES/build_new_file_content — the exact same machinery
    # the "+" element/file dialogs already use, so a brand-new project's starter module is
    # language-correct and KANT-tagged from the first line), whether to seed a starter module, and
    # whether to run `git init`. Live preview of the resulting folder layout, same "show exactly
    # what you're about to get" spirit as the other creation dialogs.
    # [FN] _ide_new_project_form — new-project name/location/language/starter/git picker
    # [FN OPEN] _ide_new_project_form
    def _ide_new_project_form(self, default_parent_dir, default_language='Python'):
        dialog, outer, body = self._internal_window('Nuovo progetto', 520, 'Chiudi senza creare')
        body.setContentsMargins(18, 16, 18, 16)
        body.setSpacing(8)

        field_style = (
            f'background:{theme.CODE_BG}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; '
            f'border-radius:6px; padding:6px;'
        )

        def field_label(text):
            label = QLabel(text)
            label.setStyleSheet(f'color:{theme.TEXT}; border:none; margin-top:4px;')
            body.addWidget(label)

        field_label('Nome del progetto:')
        name_field = QLineEdit()
        name_field.setPlaceholderText('es. shop-backend')
        name_field.setStyleSheet(field_style)
        body.addWidget(name_field)

        field_label('Cartella principale (il progetto verrà creato al suo interno):')
        location_row = QHBoxLayout()
        location_field = QLineEdit(default_parent_dir)
        location_field.setStyleSheet(field_style)
        location_row.addWidget(location_field, 1)
        browse_btn = QPushButton('Sfoglia...')
        browse_btn.setStyleSheet(theme.BUTTON_STYLE)

        def browse():
            chosen = QFileDialog.getExistingDirectory(dialog, 'Cartella principale', location_field.text())
            if chosen:
                location_field.setText(chosen)

        browse_btn.clicked.connect(browse)
        location_row.addWidget(browse_btn)
        body.addLayout(location_row)

        field_label('Linguaggio principale (determina il modulo di esempio):')
        lang_box = QComboBox()
        lang_box.addItems(list(ELEMENT_LANGUAGES))
        lang_box.setCurrentText(default_language)
        lang_box.setStyleSheet(field_style)
        body.addWidget(lang_box)

        starter_check = QCheckBox('Crea un modulo di esempio con tag KANT')
        starter_check.setChecked(True)
        starter_check.setStyleSheet(f'color:{theme.TEXT};')
        body.addWidget(starter_check)

        git_check = QCheckBox('Inizializza un repository Git')
        git_check.setChecked(True)
        git_check.setStyleSheet(f'color:{theme.TEXT};')
        body.addWidget(git_check)

        field_label('Anteprima:')
        preview = QTextEdit()
        preview.setReadOnly(True)
        preview.setFixedHeight(120)
        preview.setFont(QFont('Consolas', theme.CODE_FONT_PT))
        preview.setStyleSheet(field_style)
        body.addWidget(preview)

        def refresh_preview():
            name = name_field.text().strip() or 'nome-progetto'
            language = lang_box.currentText()
            ext = ELEMENT_LANGUAGES.get(language, ELEMENT_LANGUAGES['Generico'])['ext']
            lines = [f'{name}/']
            if starter_check.isChecked():
                lines.append(f'  main{ext}')
            if git_check.isChecked():
                lines.append('  .git/')
            if not starter_check.isChecked() and not git_check.isChecked():
                lines.append('  (cartella vuota)')
            preview.setPlainText('\n'.join(lines))
            if starter_check.isChecked():
                stem = 'main'
                preview.append('\n--- main' + ext + ' ---\n' + build_new_file_content('module', language, name))

        name_field.textChanged.connect(refresh_preview)
        lang_box.currentIndexChanged.connect(refresh_preview)
        starter_check.toggled.connect(refresh_preview)
        git_check.toggled.connect(refresh_preview)
        refresh_preview()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton('Annulla')
        cancel.setToolTip('Chiudi senza creare nulla')
        ok = QPushButton('Crea progetto')
        ok.setToolTip('Crea la cartella con queste opzioni e aprila')
        ok.setDefault(True)
        for button in (cancel, ok):
            button.setStyleSheet(theme.BUTTON_STYLE)
            buttons.addWidget(button)
        cancel.clicked.connect(dialog.reject)
        ok.clicked.connect(dialog.accept)
        body.addLayout(buttons)
        outer.addLayout(body)

        name_field.setFocus()
        if dialog.exec() != QDialog.Accepted:
            return None
        name = name_field.text().strip()
        location = location_field.text().strip()
        if not name or not location:
            return None
        return {
            'name': name, 'parent_dir': location, 'language': lang_box.currentText(),
            'create_starter': starter_check.isChecked(), 'init_git': git_check.isChecked(),
        }
    # [FN CLOSED] _ide_new_project_form
