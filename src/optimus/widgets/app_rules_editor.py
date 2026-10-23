"""Draft-only per-app choices shared by setup and Settings."""
from __future__ import annotations

import copy
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..software_app_rules import (SOURCES, apply_update_rules, rule_identity, validate_app_rules)

UPDATE_CHOICES = (('Follow global', 'follow_global'), ('Always update', 'always_update'), ('Always ignore', 'always_ignore'))
PRESENCE_CHOICES = (('Leave installed state', 'keep_current'), ('Always install if missing', 'keep_installed'),
                    ('Always uninstall if installed', 'keep_uninstalled'))


class AppRulesEditor(QWidget):
    changed = Signal()

    def __init__(self, settings=None, *, native=False, parent=None):
        super().__init__(parent)
        self.native, self._rows, self._candidates = native, {}, []
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Exact app / source', 'Updates', 'Installed state', 'Saved / draft'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(140)
        self.table.setMaximumHeight(300)
        body.addWidget(self.table)
        edit = QHBoxLayout()
        edit.addWidget(QLabel('Selected app:'))
        self.update_choice, self.presence_choice = QComboBox(), QComboBox()
        for label, value in UPDATE_CHOICES:
            self.update_choice.addItem(label, value)
        for label, value in PRESENCE_CHOICES:
            self.presence_choice.addItem(label, value)
        edit.addWidget(self.update_choice, 1)
        edit.addWidget(self.presence_choice, 1)
        self.clear_rule = QPushButton('Follow global / leave installed state')
        edit.addWidget(self.clear_rule)
        body.addLayout(edit)
        add = QHBoxLayout()
        self.source = QComboBox()
        self.source.addItems(list(SOURCES) if native else ['winget'])
        if native:
            self.source.setCurrentText('brew' if sys.platform == 'darwin' else 'apt')
        self.identity = QLineEdit()
        self.identity.setPlaceholderText('Exact manager-qualified ID, e.g. apt:package' if native else 'Exact package ID, e.g. Vendor.Application')
        self.add_button = QPushButton('Add exact app')
        add.addWidget(self.source)
        add.addWidget(self.identity, 1)
        add.addWidget(self.add_button)
        body.addLayout(add)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        body.addWidget(self.status)
        note = QLabel('Choices take effect only after Save, with Software Updater enabled and saved package agreements. '
            'Only offered newer versions update; pins, held offers and protected apps remain enforced. '
            'Saved rules apply on each maintenance run. Uninstalling can permanently remove app-local data; reinstalling does not restore that data. '
            'Saving a changed choice removes that app’s matching legacy exclusion when you choose Follow global or Always update.'
            + (' Automatic install/uninstall is currently available only on Windows with WinGet; native presence rules are retained for review.' if native else ''))
        note.setWordWrap(True)
        body.addWidget(note)
        self.table.itemSelectionChanged.connect(self._selected)
        self.update_choice.currentIndexChanged.connect(lambda: self._edit('update'))
        self.presence_choice.currentIndexChanged.connect(lambda: self._edit('presence'))
        self.clear_rule.clicked.connect(self._clear)
        self.add_button.clicked.connect(self._add)
        self.reset(settings or {})

    def reset(self, settings):
        self._settings = copy.deepcopy(settings)
        self._rows = {}
        for rule in validate_app_rules(settings.get('software_app_rules', []), native=self.native):
            self._put(rule, explicit=True)
        for identity in settings.get('software_update_ids', []) + settings.get('software_update_exclude_ids', []):
            source = identity.split(':', 1)[0] if self.native and ':' in identity else 'winget'
            try:
                key = rule_identity(identity, source, native=self.native)
            except ValueError:
                continue  # Existing advanced values remain in settings unchanged.
            if key not in self._rows:
                self._put({'id': identity, 'source': source, 'update_policy': 'always_ignore' if identity.casefold() in {
                    item.casefold() for item in settings.get('software_update_exclude_ids', [])} else 'follow_global', 'presence': 'keep_current'})
        self.set_candidates(self._candidates)
        self.status.setText('Saved choices loaded. Editing does not install, uninstall or save anything.')

    def _put(self, rule, *, explicit=False, name=None):
        key = rule_identity(rule['id'], rule['source'], native=self.native)
        if key not in self._rows:
            self._rows[key] = dict(copy.deepcopy(rule), _explicit=explicit, _edited=False, _name=name or rule['id'])
        elif name:
            self._rows[key]['_name'] = name
        return key

    def set_candidates(self, records):
        self._candidates = copy.deepcopy(records[:1000]) if isinstance(records, list) else []
        for row in self._candidates:
            if not isinstance(row, dict):
                continue
            identity = row.get('id') or row.get('package_id')
            source = row.get('source') or ('winget' if not self.native else str(identity).split(':', 1)[0])
            try:
                self._put({'id': identity, 'source': source, 'update_policy': 'follow_global', 'presence': 'keep_current'},
                          name=str(row.get('name') or row.get('Name') or identity))
            except ValueError:
                continue
        self._render()

    def _render(self, selected=None):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._rows))
        for index, (key, row) in enumerate(self._rows.items()):
            excluded = row['id'].casefold() in {identity.casefold() for identity in self._settings.get('software_update_exclude_ids', [])}
            values = (row['_name'] + ' | ' + row['id'] + ' [' + row['source'] + ']',
                dict((value, text) for text, value in UPDATE_CHOICES)[row['update_policy']],
                dict((value, text) for text, value in PRESENCE_CHOICES)[row['presence']],
                'Unsaved' if row['_edited'] else 'Saved; legacy exclusion wins' if excluded and row['update_policy'] == 'always_update'
                else 'Saved rule' if row['_explicit'] else 'Global / existing scope')
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, key)
                self.table.setItem(index, column, item)
            if selected == key:
                self.table.selectRow(index)
        self.table.blockSignals(False)
        if self.table.currentRow() < 0 and self.table.rowCount():
            self.table.selectRow(0)
        self._selected()

    def _key(self):
        item = self.table.item(self.table.currentRow(), 0)
        value = item.data(Qt.UserRole) if item else None
        return tuple(value) if isinstance(value, (list, tuple)) else value

    def _selected(self):
        row = self._rows.get(self._key())
        for control in (self.update_choice, self.presence_choice, self.clear_rule):
            control.setEnabled(row is not None)
        if not row:
            return
        for control, value in ((self.update_choice, row['update_policy']), (self.presence_choice, row['presence'])):
            control.blockSignals(True)
            control.setCurrentIndex(control.findData(value))
            control.blockSignals(False)
        self.presence_choice.setEnabled(not self.native and row['source'] == 'winget')

    def _edit(self, origin):
        key = self._key()
        if key is None:
            return
        row = self._rows[key]
        row['update_policy'], row['presence'] = self.update_choice.currentData(), self.presence_choice.currentData()
        if origin == 'presence' and row['presence'] == 'keep_uninstalled':
            row['update_policy'] = 'always_ignore'
        elif origin == 'update' and row['update_policy'] != 'always_ignore' and row['presence'] == 'keep_uninstalled':
            row['presence'] = 'keep_current'
        row['_edited'], row['_explicit'] = True, True
        self._render(key)
        self.status.setText('Unsaved choice. Save to apply this exact app rule; current work and programs are unchanged.')
        self.changed.emit()

    def _clear(self):
        key = self._key()
        if key is None:
            return
        self._rows[key].update(update_policy='follow_global', presence='keep_current', _explicit=False, _edited=True)
        self._render(key)
        self.changed.emit()

    def _add(self):
        try:
            key = self._put({'id': self.identity.text().strip(), 'source': self.source.currentText(),
                'update_policy': 'follow_global', 'presence': 'keep_current'})
        except ValueError as error:
            self.status.setText(str(error)); return
        self._render(key)
        self.identity.clear()

    def apply_to(self, settings):
        result = copy.deepcopy(settings)
        result['software_app_rules'] = validate_app_rules([{key: row[key] for key in ('id', 'source', 'update_policy', 'presence')}
            for row in self._rows.values() if row['_explicit']], native=self.native)
        removed = {row['id'].casefold() for row in self._rows.values() if row['_edited'] and row['update_policy'] != 'always_ignore'}
        result['software_update_exclude_ids'] = [identity for identity in result.get('software_update_exclude_ids', []) if identity.casefold() not in removed]
        apply_update_rules(result)  # Check the combined bounded scope without changing persisted legacy lists.
        return result

    def has_automatic_actions(self):
        """Draft display hint only; Save still performs strict validation."""
        return any(row['_explicit'] and (row['update_policy'] == 'always_update' or row['presence'] != 'keep_current')
                   for row in self._rows.values())
