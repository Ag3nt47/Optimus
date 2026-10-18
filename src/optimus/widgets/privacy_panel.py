"""Counted browser privacy controls, embedded in the Optimus desktop window."""
from __future__ import annotations
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,QPushButton,QCheckBox,QLineEdit,QPlainTextEdit,QGroupBox


class PrivacyPanel(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._plan = None
        self._backups = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        intro = QLabel("Preview browser record counts, then clear only the data you select. Close the selected browser and its background processes first.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        profile_bar = QHBoxLayout()
        profile_bar.addWidget(QLabel("Profile"))
        self.profile = QComboBox()
        self.profile.setMinimumWidth(280)
        self.profile.currentIndexChanged.connect(self._profile_changed)
        profile_bar.addWidget(self.profile, 1)
        refresh = QPushButton("Refresh profiles")
        refresh.clicked.connect(lambda: self.refresh())
        profile_bar.addWidget(refresh)
        layout.addLayout(profile_bar)
        choices = QHBoxLayout()
        self.categories = {}
        for key, title in (("history", "Browsing history"), ("cookies", "Cookies and site sign-ins"), ("downloads", "Download list")):
            checkbox = QCheckBox(title)
            checkbox.setChecked(False)
            checkbox.toggled.connect(self._invalidate)
            self.categories[key] = checkbox
            choices.addWidget(checkbox)
        choices.addStretch()
        layout.addLayout(choices)
        self.keep_domains = QLineEdit()
        self.keep_domains.setPlaceholderText("Keep cookies for these domains: example.com, another.example")
        self.keep_domains.textChanged.connect(self._invalidate)
        layout.addWidget(self.keep_domains)
        keep_note = QLabel("Kept domains include their subdomains. Clearing other cookies can sign you out. Downloaded files, saved passwords and bookmarks are preserved.")
        keep_note.setWordWrap(True)
        layout.addWidget(keep_note)
        actions = QHBoxLayout()
        preview = QPushButton("Preview selected data")
        preview.clicked.connect(self._preview)
        actions.addWidget(preview)
        self.clear_button = QPushButton("Clear selected data")
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(self._apply)
        actions.addWidget(self.clear_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("A preview shows counts only. Browsing addresses and cookie values are not displayed.")
        self.details.setMinimumHeight(150)
        layout.addWidget(self.details, 1)
        native = QHBoxLayout()
        native.addWidget(QLabel("Browser controls:"))
        for key, title in (("edge", "Edge"), ("chrome", "Chrome"), ("brave", "Brave"), ("firefox", "Firefox")):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, browser=key: self.window._run("browser_privacy", browser))
            native.addWidget(button)
        native.addStretch()
        layout.addLayout(native)
        restore_box = QGroupBox("Private local backups")
        restore_layout = QVBoxLayout(restore_box)
        restore_note = QLabel("Original data stays in your local Optimus backup folder. Restore is blocked after new browser activity so newer history and bookmarks are not overwritten.")
        restore_note.setWordWrap(True)
        restore_layout.addWidget(restore_note)
        restore_bar = QHBoxLayout()
        self.backup = QComboBox()
        self.backup.setMinimumWidth(280)
        self.backup.currentIndexChanged.connect(self._backup_changed)
        restore_bar.addWidget(self.backup, 1)
        backup_refresh = QPushButton("Refresh backups")
        backup_refresh.clicked.connect(self.refresh_backups)
        restore_bar.addWidget(backup_refresh)
        self.restore_button = QPushButton("Restore selected backup")
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self._restore)
        restore_bar.addWidget(self.restore_button)
        restore_layout.addLayout(restore_bar)
        layout.addWidget(restore_box)

    def refresh(self):
        self.window._run("privacy_profiles", done=self._show_profiles)

    def _show_profiles(self, profiles):
        previous = self.profile.currentData()
        previous_id = previous.get("id") if isinstance(previous, dict) else None
        self.profile.blockSignals(True)
        self.profile.clear()
        for item in profiles or []:
            self.profile.addItem(f"{item['browser']} - {item['name']}", item)
            self.profile.setItemData(self.profile.count()-1, item.get("path", ""), Qt.ToolTipRole)
            if item.get("id") == previous_id:
                self.profile.setCurrentIndex(self.profile.count()-1)
        self.profile.blockSignals(False)
        self._profile_changed()
        if not self.profile.count():
            self.details.setPlainText("No supported local profiles were found in standard browser profile folders. The browser's own privacy controls remain available.")
        self.refresh_backups()

    def _profile_changed(self, *args):
        self._invalidate()
        item = self.profile.currentData()
        supported = set(item.get("supportedCategories", [])) if isinstance(item, dict) else set()
        for key, checkbox in self.categories.items():
            checkbox.setEnabled(key in supported)
            checkbox.setToolTip("" if key in supported else "Use this browser's own privacy controls for this category.")
            if key not in supported:
                checkbox.setChecked(False)

    def _invalidate(self, *args):
        self._plan = None
        self.clear_button.setEnabled(False)

    def _preview(self):
        item = self.profile.currentData()
        if not isinstance(item, dict):
            self.window._error("Refresh profiles and choose a browser profile first.")
            return
        selected = [key for key, checkbox in self.categories.items() if checkbox.isEnabled() and checkbox.isChecked()]
        if not selected:
            self.window._error("Choose at least one privacy category to preview.")
            return
        domains = [domain.strip() for domain in self.keep_domains.text().replace("\n", ",").split(",") if domain.strip()]
        self._invalidate()
        self.window._run("privacy_scan", item["id"], categories=selected, keep_domains=domains, done=self._show_plan)

    def _show_plan(self, plan):
        self._plan = plan
        self.clear_button.setEnabled(bool(plan.get("canApply")))
        profile = plan.get("profile", {})
        lines = [f"{profile.get('browser', '')} - {profile.get('name', '')}", ""]
        labels = {"history": "History visits", "cookies": "Cookies selected for removal", "cookiesKept": "Cookies kept for your domains", "downloads": "Download list entries"}
        for key, count in plan.get("counts", {}).items():
            lines.append(f"{labels.get(key, key)}: {count:,}")
        lines.extend(["", f"Private backup estimate: {plan.get('estimatedBackupBytes', 0)/1024/1024:.1f} MB", "Preview expires after 15 minutes."])
        for unsupported in plan.get("unsupported", []):
            lines.append(unsupported.get("reason", "This category needs browser-native controls."))
        lines.extend(["", plan.get("note", "")])
        self.details.setPlainText("\n".join(lines))

    def _apply(self):
        if not self._plan or not self._plan.get("canApply"):
            return
        plan = self._plan
        if not self.window._confirm("Clear selected browser data", "Clear the categories in this preview? Cookies can sign you out. A private local backup is required before records are deleted.", details=self.details.toPlainText(), phrase="CLEAR"):
            return
        request = {"planId": plan["planId"], "confirmed": True, "selectedCategories": list(plan["categories"])}
        self._invalidate()
        self.window._run("privacy_apply", request, done=self._applied)

    def _applied(self, result):
        self.details.setPlainText("Privacy cleanup completed.\n\n" + json.dumps(result, indent=2, ensure_ascii=False))
        self.refresh_backups()

    def refresh_backups(self):
        self.window._run("privacy_backups", done=self._show_backups)

    def _show_backups(self, backups):
        self._backups = backups or []
        self.backup.blockSignals(True)
        self.backup.clear()
        for item in self._backups:
            label = f"{item.get('createdUtc','')} - {item.get('browser','')} / {item.get('profileName','')} - {item.get('state','')}"
            self.backup.addItem(label, item)
        self.backup.blockSignals(False)
        self._backup_changed()

    def _backup_changed(self, *args):
        item = self.backup.currentData()
        self.restore_button.setEnabled(isinstance(item, dict) and item.get("state") in ("applied", "partial"))

    def _restore(self):
        item = self.backup.currentData()
        if not isinstance(item, dict) or item.get("state") not in ("applied", "partial"):
            return
        if not self.window._confirm("Restore private browser backup", "Restore the original data to this same browser profile? Close the browser first. Restore is refused if newer browser data would be overwritten.", details=item, phrase="RESTORE"):
            return
        self.window._run("privacy_restore", item["backupId"], confirmed=True, done=self._restored)

    def _restored(self, result):
        self._invalidate()
        self.details.setPlainText("Private backup restored.\n\n" + json.dumps(result, indent=2, ensure_ascii=False))
        self.refresh_backups()
