"""Small themed modal dialogs shared by the main window."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from kant import theme


class AiReviewDialog(QDialog):
    """Compact AI change summary that expands into file/hunk review and final-text editing."""

    DATA_ROLE = Qt.UserRole

    def __init__(self, parent, review, render_text):
        super().__init__(parent)
        self.review = review
        self.render_text = render_text
        self.by_path = {item['path']: item for item in review}
        self.file_items = {}
        self.manual_text = {}
        self.action = 'cancel'
        self._current_path = None
        self._loading_editor = False
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setMinimumWidth(920)
        self.setStyleSheet(
            f'QDialog {{ background:{theme.CODE_BG}; border:1px solid {theme.BORDER}; border-radius:16px; }} '
            f'#aiReviewBubble, #aiReviewDetails {{ background:{theme.PANEL}; border:1px solid {theme.BORDER}; border-radius:16px; }} '
            f'QLabel {{ color:{theme.TEXT}; }} QTreeWidget, QPlainTextEdit {{ background:{theme.CODE_BG}; '
            f'color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:10px; }} '
            f'QTabWidget::pane {{ border:1px solid {theme.BORDER}; border-radius:10px; background:{theme.CODE_BG}; }}'
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(self._build_summary())
        self.details = self._build_details()
        self.details.hide()
        root.addWidget(self.details, 1)

    def _button(self, text, slot):
        button = QPushButton(text)
        button.setStyleSheet(theme.BUTTON_STYLE)
        button.clicked.connect(slot)
        return button

    def _build_summary(self):
        panel = QWidget()
        panel.setObjectName('aiReviewBubble')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.setSpacing(10)
        sender = QLabel('Assistente AI')
        sender.setStyleSheet(f'color:{theme.WARN}; font-weight:700; letter-spacing:1px;')
        layout.addWidget(sender)
        header = QHBoxLayout()
        icon = QLabel('AI')
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(48, 48)
        icon.setStyleSheet(f'background:{theme.CODE_BG}; border-radius:12px; font-size:22px; color:{theme.TEXT};')
        header.addWidget(icon)
        totals = QVBoxLayout()
        title = QLabel(f"{len(self.review)} file {'modificato' if len(self.review) == 1 else 'modificati'}")
        title.setFont(QFont('Consolas', theme.TREE_FONT_PT + 1, QFont.DemiBold))
        totals.addWidget(title)
        added = sum(item['additions'] for item in self.review)
        deleted = sum(item['deletions'] for item in self.review)
        counts = QLabel(f'<span style="color:{theme.OK}">+{added}</span>  <span style="color:#ef4444">-{deleted}</span>')
        totals.addWidget(counts)
        header.addLayout(totals)
        header.addStretch(1)
        header.addWidget(self._button('Annulla ↶', self.reject))
        header.addWidget(self._button('Controllo', self._show_details))
        layout.addLayout(header)

        self.summary_rows = []
        for index, item in enumerate(self.review):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            path_button = QPushButton(item['path'])
            path_button.setStyleSheet(
                f'QPushButton {{ text-align:left; padding:8px 0; border:none; color:{theme.TEXT}; background:transparent; }} '
                f'QPushButton:hover {{ color:{theme.ACCENT}; }}'
            )
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
        self.more_btn.setVisible(bool(hidden))
        layout.addWidget(self.more_btn)
        return panel

    def _toggle_summary(self):
        expanded = not self.summary_rows[-1].isVisible()
        for row in self.summary_rows[3:]:
            row.setVisible(expanded)
        self.more_btn.setText('Mostra meno ⌃' if expanded else f'Mostra altri {len(self.review) - 3} file ⌄')
        self.adjustSize()

    def _build_details(self):
        panel = QWidget()
        panel.setObjectName('aiReviewDetails')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 8, 16, 14)
        splitter = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['File e blocchi da mantenere'])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setMinimumWidth(300)
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
        self.result_editor = QPlainTextEdit()
        self.result_editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.result_editor.textChanged.connect(self._editor_changed)
        self.tabs.addTab(self.diff_view, 'Differenze')
        self.tabs.addTab(self.result_editor, 'Risultato modificabile')
        splitter.addWidget(self.tabs)
        splitter.setSizes([330, 800])
        layout.addWidget(splitter, 1)

        note = QLabel('I blocchi selezionati saranno mantenuti. Cambiare una selezione ripristina il risultato del file e scarta i ritocchi manuali su quel file.')
        note.setWordWrap(True)
        note.setStyleSheet(f'color:{theme.DIM};')
        layout.addWidget(note)
        actions = QHBoxLayout()
        actions.addWidget(self._button('Rifiuta selezionati', lambda: self._set_selected(False)))
        actions.addWidget(self._button('Accetta selezionati', lambda: self._set_selected(True)))
        actions.addStretch(1)
        actions.addWidget(self._button('Annulla tutto', self.reject))
        actions.addWidget(self._button('Accetta tutto', self._accept_all))
        actions.addWidget(self._button('Applica scelte', self._apply))
        layout.addLayout(actions)
        return panel

    def _show_details(self, path=None):
        self.details.show()
        self.setMinimumSize(1120, 720)
        self.resize(1200, 780)
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
        self._apply()

    def _apply(self):
        self.action = 'apply'
        self.accept()


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
        ok = QPushButton('OK')
        for button in (cancel, ok):
            button.setStyleSheet(theme.BUTTON_STYLE)
            row.addWidget(button)
        cancel.clicked.connect(dialog.reject)
        ok.clicked.connect(dialog.accept)
        layout.addLayout(row)

    def _ide_choice(self, title, message, choices):
        dialog, layout = self._dialog(title, message)
        layout.setSpacing(14)
        result = {'value': None}
        row = QHBoxLayout()
        row.addStretch(1)

        def choose(value):
            result['value'] = value
            dialog.accept()

        for label, value in choices:
            button = QPushButton(label)
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
