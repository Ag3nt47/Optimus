"""Angerona-style source editor with save, history, revert and ready-checked restart."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMainWindow,
                             QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)
from .source_workspace import SourceWorkspace
from .widgets.code_highlighter import PythonHighlighter
from .widgets.theme import build_qss
from .widgets.flow_layout import FlowLayout

class SandboxWindow(QMainWindow):
    def __init__(self, backend, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle('Optimus - Source Sandbox')
        self.resize(1280, 850)
        self.setMinimumSize(640, 480)
        self.setStyleSheet(build_qss('cyber'))
        source_root = backend.source_root()
        self.workspace = SourceWorkspace(source_root, backend.data_dir)
        self.current = None
        self.restarting = False
        root = QWidget()
        layout = QVBoxLayout(root)
        self.description = QLabel('Full Optimus source working copy. Save and Revert keep version history. Restart Optimus runs the saved working copy. Installed release files remain available.')
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        toolbar = FlowLayout()
        for label, callback in [('Save', self.save), ('Revert', self.revert), ('Original version', self.original), ('History', self.history), ('Restart Optimus', self.restart), ('Export project', self.export)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        splitter = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel('Optimus source')
        self.tree.setMinimumWidth(160)
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont('Cascadia Mono', 10))
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setMinimumWidth(180)
        self.highlighter = PythonHighlighter(self.editor.document())
        splitter.addWidget(self.tree)
        splitter.addWidget(self.editor)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([240, 900])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)
        self.status = QLabel(str(self.workspace.root))
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.status)
        self.setCentralWidget(root)
        QShortcut(QKeySequence.Save, self, activated=self.save)
        self.tree.itemClicked.connect(self.open_item)
        self._build_tree()

    def _build_tree(self):
        nodes = {}
        for relative in self.workspace.files():
            parent = self.tree.invisibleRootItem()
            parts = relative.split('/')
            for index, part in enumerate(parts):
                key = '/'.join(parts[:index+1])
                if key not in nodes:
                    nodes[key] = QTreeWidgetItem(parent, [part])
                    if index == len(parts)-1:
                        nodes[key].setData(0, Qt.UserRole, relative)
                parent = nodes[key]
        self.tree.expandToDepth(1)
        first = next((item for item in nodes.values() if item.data(0, Qt.UserRole) == 'src/optimus/ui.py'), None)
        if first:
            self.tree.setCurrentItem(first)
            self.open_item(first)

    def _unsaved(self):
        if not self.editor.document().isModified():
            return True
        answer = QMessageBox.question(self, 'Unsaved code', 'Save the current edits before continuing?', QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def open_item(self, item, column=0):
        relative = item.data(0, Qt.UserRole)
        if not relative or not self._unsaved():
            return
        try:
            self.editor.setPlainText(self.workspace.read(relative))
            self.editor.document().setModified(False)
            self.current = relative
            self.status.setText(relative)
        except Exception as error:
            QMessageBox.warning(self, 'Open source', str(error))

    def save(self):
        if not self.current:
            return True
        try:
            self.workspace.save(self.current, self.editor.toPlainText())
            self.editor.document().setModified(False)
            self.status.setText('Saved: ' + self.current + ' - previous version retained')
            return True
        except Exception as error:
            QMessageBox.warning(self, 'Code was not saved', str(error))
            return False

    def revert(self):
        if not self.current or not self._unsaved():
            return
        try:
            self.editor.setPlainText(self.workspace.revert(self.current))
            self.editor.document().setModified(False)
            self.status.setText('Reverted: ' + self.current + ' - Restart Optimus to use this code')
        except Exception as error:
            QMessageBox.warning(self, 'Revert', str(error))

    def original(self):
        if not self.current or not self._unsaved():
            return
        try:
            self.editor.setPlainText(self.workspace.restore_original(self.current))
            self.editor.document().setModified(False)
            self.status.setText('Original version restored in sandbox. Restart Optimus to use it.')
        except Exception as error:
            QMessageBox.warning(self, 'Original version', str(error))

    def history(self):
        if not self.current:
            return
        entries = self.workspace.versions(self.current)
        selected, accepted = QInputDialog.getItem(self, 'Saved code versions', 'Restore a saved version:', entries, 0, False)
        if accepted and selected:
            try:
                self.editor.setPlainText(self.workspace.revert(self.current, selected))
                self.editor.document().setModified(False)
            except Exception as error:
                QMessageBox.warning(self, 'History', str(error))

    def export(self):
        folder = QFileDialog.getExistingDirectory(self, 'Choose an empty project destination')
        if folder:
            try:
                result = self.workspace.export(folder)
                self.status.setText('Exported editable project: ' + result['destination'])
            except Exception as error:
                QMessageBox.warning(self, 'Export', str(error))

    def _operation_active(self):
        """Keep the current GUI alive while any shared-backend operation runs."""
        if bool(getattr(getattr(self.backend, 'workflow', None), 'active', False)):
            return True
        bridge = getattr(self.backend, 'bridge', None)
        if bridge is not None:
            if bool(getattr(bridge, '_busy', False)) or bool(getattr(bridge, 'busy', False)):
                return True
            # Requests can remain pending after a UI timeout. They must finish
            # before restarting even when the requesting worker has returned.
            if bool(getattr(bridge, '_pending', {})) or bool(getattr(bridge, 'pending', {})):
                return True
        widgets = []
        owner = self.parent()
        if owner is not None:
            widgets.append(owner)
        enumerate_windows = getattr(QApplication, 'topLevelWidgets', None)
        if callable(enumerate_windows):
            widgets.extend(enumerate_windows())
        for widget in widgets:
            try:
                if getattr(widget, 'backend', None) is not self.backend:
                    continue
                if bool(getattr(widget, 'busy', False)):
                    return True
                details = getattr(widget, '_settings_window', None)
                if details is not None and bool(getattr(details, 'busy', False)):
                    return True
            except RuntimeError:
                continue  # A just-destroyed Qt wrapper cannot own active work.
        return False

    def restart(self):
        if self.restarting:
            return
        if self._operation_active():
            QMessageBox.warning(self, 'Operation in progress', 'Wait for every Optimus operation to finish before restarting. Pending Windows operations can continue after a timeout.')
            return
        if not self.save():
            return
        errors = self.workspace.validate_all()
        if errors:
            QMessageBox.warning(self, 'Source validation', '\n'.join(errors[:10]))
            return
        if QMessageBox.question(self, 'Restart with saved code', 'Run the saved source working copy in a new Optimus process? It will use your current account permissions. The current window stays open if the new version fails to start.', QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        ready_dir = self.backend.data_dir / 'Sandbox' / 'restart'
        ready_dir.mkdir(parents=True, exist_ok=True)
        marker = ready_dir / (uuid.uuid4().hex + '.json')
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--source-workspace', str(self.workspace.root), '--restart-ready-file', str(marker)]
        else:
            command = [sys.executable, '-B', str(self.workspace.root / 'launch.py'), '--source-workspace', str(self.workspace.root), '--restart-ready-file', str(marker)]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', OPTIMUS_ORIGINAL_SOURCE=str(self.workspace.source_root))
        self.child = subprocess.Popen(command, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.restarting = True
        self.status.setText('Starting saved code; waiting for the new interface...')
        deadline = time.monotonic() + 25
        self.restart_timer = QTimer(self)
        def check():
            if marker.is_file():
                try:
                    response = json.loads(marker.read_text(encoding='utf-8'))
                    if response.get('ready') and response.get('pid') == self.child.pid:
                        self.restart_timer.stop()
                        QApplication.instance().quit()
                        return
                except (OSError, ValueError):
                    pass
            if self.child.poll() is not None or time.monotonic() > deadline:
                self.restart_timer.stop()
                self.restarting = False
                self.status.setText('The new version did not report ready. Current Optimus kept open; edit or revert the saved code.')
        self.restart_timer.timeout.connect(check)
        self.restart_timer.start(250)

    def closeEvent(self, event):
        if self.restarting or self._unsaved():
            event.accept()
        else:
            event.ignore()


