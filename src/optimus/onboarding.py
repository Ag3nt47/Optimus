"""First-run questions. Preferences are written only after Save choices."""
from __future__ import annotations
import copy
import ntpath
import os
import re
import json
from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QButtonGroup, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget, QWizard, QWizardPage, QListWidget, QListWidgetItem,
)

SETUP_VERSION = 2
DEVICE_RULE_LIMIT = 2048

CADENCES = {
    'manual': 'Manual - only when I click Optimize',
    'startup': 'Each app startup',
    'daily': 'Daily while the app is open',
    'weekly': 'Weekly while the app is open',
}
REMOVALS = (
    ('consumer_debloat_enabled', 'Remove the curated consumer-app group'),
    ('m365_debloat_enabled', 'Remove Microsoft 365 consumer packages'),
    ('outlook_debloat_enabled', 'Remove the new Outlook package'),
    ('edge_debloat_enabled', 'Run the supported Microsoft Edge uninstall action'),
)


def _label(text):
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    return label


class QuestionPage(QWizardPage):
    def __init__(self, title, description, parent=None, on_enter=None):
        super().__init__(parent)
        self.setTitle(title)
        self.on_enter = on_enter
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(14, 10, 20, 10)
        self.body.setSpacing(16)
        self.body.addWidget(_label(description))
        scroll.setWidget(content)
        outer.addWidget(scroll)

    def initializePage(self):
        if self.on_enter:
            self.on_enter()


class ProbeSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class InventoryProbe(QRunnable):
    def __init__(self, operation, args, kwargs):
        super().__init__()
        self.operation, self.args, self.kwargs = operation, args, kwargs
        self.signals = ProbeSignals()

    def run(self):
        try:
            self.signals.result.emit(self.operation(*self.args, **self.kwargs))
        except Exception as error:
            self.signals.error.emit(str(error))
        finally:
            self.signals.finished.emit()


class InitialSetupWizard(QWizard):
    """Collect scoped approvals; inventories run only from explicit setup buttons."""
    def __init__(self, backend, parent=None, preferences=None, *, first_run=False):
        super().__init__(parent)
        self.backend = backend
        self.initial = copy.deepcopy(preferences if preferences is not None else backend.preferences())
        self.saved_preferences = None
        self.run_now = False
        self.controls = {}
        self.page_ids = {}
        self.category_controls = {}
        self.category_names = {}
        self.browser_controls = {}
        self.selection_actions = {}
        self.busy = False
        self._closing = False
        self._probe_worker = None
        self._probe_callback = None
        self._probe_status = None
        self._cloud_binding = None
        self._startup_loaded = False
        self._drivers_loaded = False
        self._hardware_loaded = False
        self._connection_panel = None
        self.setWindowTitle('Optimus - initial setup' if first_run else 'Optimus - setup questions')
        self.setWizardStyle(QWizard.ClassicStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setButtonText(QWizard.FinishButton, 'Save choices')
        self.resize(1000, 780)
        self.setMinimumSize(850, 650)
        palette = self.palette()
        for role, color in ((QPalette.Window, '#101722'), (QPalette.WindowText, '#e1ebf7'),
                            (QPalette.Base, '#0b111a'), (QPalette.AlternateBase, '#162232'),
                            (QPalette.Text, '#e1ebf7'), (QPalette.Button, '#20344b'),
                            (QPalette.ButtonText, '#e1ebf7'), (QPalette.Highlight, '#1f9cff'),
                            (QPalette.HighlightedText, '#07111f')):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.setStyleSheet('''
            QWizard, QWizardPage, QScrollArea, QWidget {background:#101722;color:#e1ebf7;font-family:Segoe UI;font-size:13px;}
            QLabel {background:transparent;}
            QLineEdit,QPlainTextEdit,QSpinBox,QComboBox {background:#0b111a;color:#e1ebf7;border:1px solid #52647b;border-radius:4px;padding:7px;}
            QComboBox QAbstractItemView {background:#142235;color:#e1ebf7;selection-background-color:#1f9cff;selection-color:#07111f;}
            QPushButton {background:#20344b;color:#e1ebf7;border:1px solid #52647b;border-radius:5px;padding:9px 18px;min-height:24px;}
            QPushButton:hover {background:#304f70;}
            QPushButton:default {background:#1f9cff;color:#07111f;border-color:#91ceff;}
            QPushButton:disabled {color:#94a3b8;background:#172231;}
            QCheckBox,QRadioButton {spacing:10px;min-height:25px;}
            QCheckBox::indicator,QRadioButton::indicator,QListView::indicator {width:18px;height:18px;border:1px solid #94a3b8;background:#0b111a;}
            QCheckBox::indicator,QListView::indicator {border-radius:3px;}
            QListWidget {background:#0b111a;border:1px solid #52647b;border-radius:4px;}
            QListWidget::item {padding:6px;}
            QRadioButton::indicator {border-radius:10px;}
            QCheckBox::indicator:checked,QRadioButton::indicator:checked,QListView::indicator:checked {background:#1f9cff;border:2px solid #e1ebf7;}
            QCheckBox::indicator:disabled {border-color:#52647b;}
            QCheckBox:disabled {color:#a4b1c2;}
            QScrollBar:vertical {background:#101722;width:12px;}
            QScrollBar::handle:vertical {background:#465e7b;min-height:25px;border-radius:5px;}
        ''')
        self._build_cadence()
        self._build_health()
        self._build_cleanup()
        self._build_uninstaller()
        self._build_duplicates()
        self._build_cloud()
        self._build_performance()
        self._build_startup()
        self._build_software()
        self._build_drivers()
        self._build_summary()

    def _page(self, key, title, description, *, on_enter=None):
        page = QuestionPage(title, description, self, on_enter=on_enter)
        self.page_ids[key] = self.addPage(page)
        return page.body

    def _check(self, layout, key, label, *, default=False):
        checkbox = QCheckBox(label)
        checkbox.setChecked(bool(self.initial.get(key, default)))
        self.controls[key] = checkbox
        layout.addWidget(checkbox)
        return checkbox

    def _selection_buttons(self, body, key, select, clear):
        group = QWidget()
        row = QHBoxLayout(group)
        row.setContentsMargins(0, 0, 0, 0)
        select_button, clear_button = QPushButton('Select all'), QPushButton('Clear all')
        select_button.setAccessibleName('Select all ' + key + ' choices')
        clear_button.setAccessibleName('Clear all ' + key + ' choices')
        select_button.clicked.connect(lambda: select() if not self.busy else None)
        clear_button.clicked.connect(lambda: clear() if not self.busy else None)
        row.addWidget(select_button)
        row.addWidget(clear_button)
        row.addStretch()
        body.addWidget(group)
        self.selection_actions[key] = {'select': select_button, 'clear': clear_button, 'group': group}
        return group

    def _checkbox_actions(self, body, key, controls):
        def change(checked):
            for control in controls():
                if control.isEnabled():
                    control.setChecked(checked)
        self._selection_buttons(body, key, lambda: change(True), lambda: change(False))

    @staticmethod
    def _selectable(item):
        return bool(item.flags() & Qt.ItemIsEnabled and item.flags() & Qt.ItemIsUserCheckable)

    def _set_list_checks(self, key, table, checked, status, *, reserved_ids=()):
        if not table.isEnabled() or self.busy:
            return
        keys = {value.casefold() for value in reserved_ids}
        selected = skipped = 0
        for index in range(table.count()):
            item = table.item(index)
            if not self._selectable(item):
                continue
            identity = item.data(Qt.UserRole).get('id', str(index)) if key == 'software' else str(index)
            identity = identity.casefold()
            include = checked and len(keys | {identity}) <= 100
            item.setCheckState(Qt.Checked if include else Qt.Unchecked)
            if include:
                keys.add(identity)
                selected += 1
            elif checked:
                skipped += 1
        message = str(selected) + ' eligible choices selected.' if checked else 'Eligible choices cleared.'
        if skipped:
            message += ' ' + str(skipped) + ' additional choices were left unchecked because the 100-entry approval limit would be exceeded.'
        status.setText(message + ' Review the selection and any separate authorization below, then Save choices.')

    def _select_all_software(self):
        try:
            reserved = self._exact_ids(self.software_ids.toPlainText())
        except ValueError as error:
            self.software_status.setText(str(error))
            return
        self._set_list_checks('software', self.software_targets, True, self.software_status, reserved_ids=reserved)

    def _sync_selection_actions(self):
        tables = {'startup': self.startup_targets, 'software': self.software_targets,
                  'drivers': self.driver_targets, 'hardware': self.hardware_targets}
        for key, actions in self.selection_actions.items():
            table = tables.get(key)
            enabled = not self.busy and (table is None or table.isEnabled() and any(self._selectable(table.item(index)) for index in range(table.count())))
            if key == 'drivers' and self.driver_policy.currentData() == 'install_matching':
                enabled = False
            for name in ('select', 'clear'):
                actions[name].setEnabled(enabled)

    def _build_cadence(self):
        body = self._page('cadence', '1. When should Optimus run?',
            'These questions configure Optimize and its automatic cadence. Existing choices are retained; new automatic file, startup and driver changes need scoped approval. Maintenance choices are saved only at Save choices; account connection/inventory are separate explicit setup actions.')
        self.cadence_group = QButtonGroup(self)
        self.cadence_buttons = {}
        current = self.initial.get('automation_mode', 'manual')
        if current not in CADENCES:
            current = 'manual'
        for mode, text in CADENCES.items():
            button = QRadioButton(text)
            button.setChecked(mode == current)
            self.cadence_group.addButton(button)
            self.cadence_buttons[mode] = button
            body.addWidget(button)
        body.addWidget(_label('Daily and weekly maintenance require Optimus to stay open. Closing the app stops its timer. Existing Windows scheduled audit tasks are separate and remain read-only.'))
        warning = _label('At the saved cadence, the changes you approve here run without another per-run confirmation. Review policies only scan/report. Choose Manual to start runs yourself; return to setup and switch a tool off or to Review to revoke its automatic changes.')
        warning.setStyleSheet('color:#ffd29c;')
        body.addWidget(warning)
        body.addStretch()

    def _build_health(self):
        body = self._page('health', '2. Health Check',
            'Each maintenance run reads current CPU, memory, free storage, running programs, startup entries and services. It captures a second resource report afterward. This inspection does not stop programs or change Windows settings.')
        checked = QCheckBox('Include the initial and final health/resource reports (always included)')
        checked.setChecked(True)
        checked.setEnabled(False)
        body.addWidget(checked)
        body.addWidget(_label('Results are measured observations. A high memory reading does not establish that every background program is unnecessary, and no speedup is guaranteed.'))
        body.addStretch()

    def _build_cleanup(self):
        body = self._page('cleanup', '3. Custom Clean',
            'Choose temporary files, browser caches and optional browser records to clean. Temporary/cache files use the minimum age below. Selected history, cookies and download-list records are cleared across the selected browsers\' recognized profiles, regardless of age; private database backups are required. Passwords, bookmarks and downloaded files are preserved.')
        self._checkbox_actions(body, 'cleanup', lambda: [*(self.controls[key] for key in ('cleanup_enabled', 'browser_cache_enabled', 'browser_history_enabled', 'browser_cookies_enabled', 'browser_download_history_enabled')), *self.category_controls.values(), *self.browser_controls.values()])
        self._check(body, 'cleanup_enabled', 'Clean old files in my user temporary folder', default=True)
        self._check(body, 'browser_cache_enabled', 'Clean recognized browser caches and cached temporary web files', default=True)
        self._check(body, 'browser_history_enabled', 'Clear browsing history (all ages; private backup retained)')
        self._check(body, 'browser_cookies_enabled', 'Clear cookies and site sign-ins (can sign me out; private backup retained)')
        self._check(body, 'browser_download_history_enabled', 'Clear the download list (Chromium browsers only; downloaded files stay)')
        body.addWidget(_label('Apply those categories to all recognized profiles of the browsers checked below:'))
        selected_browsers = self.initial.get('browser_cleanup_browsers', ['edge', 'chrome', 'brave', 'vivaldi', 'firefox'])
        browser_row = QHBoxLayout()
        for key, name in (('edge', 'Microsoft Edge'), ('chrome', 'Google Chrome'), ('brave', 'Brave'), ('vivaldi', 'Vivaldi'), ('firefox', 'Firefox')):
            control = QCheckBox(name)
            control.setChecked(key in selected_browsers)
            self.browser_controls[key] = control
            browser_row.addWidget(control)
        body.addLayout(browser_row)
        self.browser_keep_domains = QPlainTextEdit()
        self.browser_keep_domains.setPlaceholderText('Keep cookies for these domains, comma-separated: example.com, another.example')
        self.browser_keep_domains.setPlainText(', '.join(self.initial.get('browser_keep_domains', [])))
        self.browser_keep_domains.setFixedHeight(70)
        body.addWidget(self.browser_keep_domains)
        body.addWidget(_label('When a selected browser is open, each run asks Yes: close and clean, or No: skip that browser for now. A refusal, cancellation, restart or failed graceful close skips that browser. Cookie exceptions include subdomains. Backups retain original private records until you remove their backup files; this is not secure erasure.'))
        row = QHBoxLayout()
        row.addWidget(_label('Intensity preset'))
        intensity = QComboBox()
        intensity.addItems(['Low', 'Standard', 'High'])
        intensity.setCurrentText(str(self.initial.get('intensity', 'Standard')))
        self.controls['intensity'] = intensity
        row.addWidget(intensity)
        row.addWidget(_label('Minimum file age'))
        age = QSpinBox()
        age.setRange(1, 3650)
        age.setSuffix(' days')
        age.setValue(int(self.initial.get('cleanup_age_days', 7)))
        self.controls['cleanup_age_days'] = age
        row.addWidget(age)
        body.addLayout(row)
        intensity.currentTextChanged.connect(lambda value: age.setValue({'Low':30,'Standard':7,'High':1}[value]))
        body.addWidget(_label('Low selects 30 days, Standard 7 days and High 1 day. An intensity preset does not enable app removals, updates or a Codex profile. You can set another age.'))
        body.addWidget(_label('Additional cleanup categories - off unless already selected:'))
        chosen = set(self.initial.get('cleanup_category_ids', []))
        categories = []
        getter = getattr(self.backend, 'cleanup_categories', None)
        try:
            if callable(getter):
                categories = getter() or []
        except Exception as error:
            body.addWidget(_label('Category inventory is unavailable. Saved category IDs are retained below; review Cleaner later. ' + str(error)))
        for category in categories:
            identifier = str(category.get('id', ''))
            if not identifier or identifier == 'user-temp' or category.get('process_names'):
                continue
            self._category(body, identifier, str(category.get('name', identifier)), identifier in chosen, not bool(category.get('available', True)))
        for identifier in sorted(chosen - self.category_controls.keys()):
            self._category(body, identifier, identifier + ' (saved category)', True, False)
        if not self.category_controls:
            body.addWidget(_label('No additional categories were reported. The Cleaner page can preview the available categories later.'))
        body.addStretch()

    def _category(self, body, identifier, name, checked, unavailable):
        text = name + (' - currently unavailable; will be skipped' if unavailable else '')
        checkbox = QCheckBox(text)
        checkbox.setChecked(checked)
        self.category_controls[identifier] = checkbox
        self.category_names[identifier] = name
        body.addWidget(checkbox)

    def _build_uninstaller(self):
        body = self._page('uninstaller', '4. Uninstaller and curated debloat',
            'Installed-app review creates an inventory/report. The separate removal choices below authorize curated groups to be removed during maintenance. They can require reinstallation later and do not guarantee recovery of app-local data.')
        self._checkbox_actions(body, 'uninstaller', lambda: [self.controls['applications_review_enabled'], *(self.controls[key] for key, unused in REMOVALS)])
        self._check(body, 'applications_review_enabled', 'Review installed applications and include a report (no uninstall)')
        for key, label in REMOVALS:
            checkbox = self._check(body, key, label)
            checkbox.setStyleSheet('color:#ffd29c;')
        body.addWidget(_label('Bing Wallpaper is always preserved by Optimus debloat. Store support, security components, frameworks and shared WebView2 are also protected. High intensity does not select removal groups for you.'))
        body.addWidget(_label('These choices do not invoke an existing external Desktop removal script. Use the detailed Debloat page to inspect exact targets and that script\'s separate approval flow.'))
        body.addStretch()

    def _build_duplicates(self):
        body = self._page('duplicates', '5. Duplicate Finder',
            'Choose review-only or automatic recycling within the local folders below. Each duplicate group retains a deterministic oldest/newest copy; only eligible old additional copies are recycled, with identity and retained-copy checks.')
        self._check(body, 'duplicates_enabled', 'Include Duplicate Finder in maintenance')
        self.duplicate_roots = QPlainTextEdit()
        self.duplicate_roots.setPlaceholderText('One absolute local folder per line, for example C:\\Users\\YourName\\Downloads')
        self.duplicate_roots.setPlainText('\n'.join(self.initial.get('duplicate_roots', [])))
        self.duplicate_roots.setFixedHeight(115)
        body.addWidget(self.duplicate_roots)
        browse = QPushButton('Add local folder...')
        browse.clicked.connect(self._browse_duplicate_root)
        body.addWidget(browse)
        self.duplicate_policy = self._policy(body, 'duplicate_policy')
        self._limits(body, 'duplicate')
        self.duplicate_consent = self._consent(body, 'duplicate_changes_approved',
            'Allow automatic recycling of eligible extra copies in only these folders, without per-run prompts')
        self.duplicate_roots.textChanged.connect(lambda: self.duplicate_consent.setChecked(False))
        self.duplicate_policy.currentIndexChanged.connect(lambda: self.duplicate_consent.setChecked(False))
        for key in ('duplicate_max_recycle','duplicate_min_age_days'):
            self.controls[key].valueChanged.connect(lambda: self.duplicate_consent.setChecked(False))
        body.addWidget(_label('Oldest/newest compares each file\'s last-modified date; ties use its path. Equal content does not make a file disposable. Reparse points, cloud placeholders, protected paths and hardlinks are excluded. Limits apply to each run; reports list skips and actual results. Restore local files through the Recycle Bin.'))
        body.addStretch()

    def _browse_duplicate_root(self):
        directory = QFileDialog.getExistingDirectory(self, 'Choose a local folder to scan')
        if directory:
            current = self.duplicate_roots.toPlainText().strip()
            self.duplicate_roots.setPlainText(current + ('\n' if current else '') + directory)

    def _build_cloud(self):
        body = self._page('cloud', '6. Cloud drive - personal OneDrive',
            'Choose metadata review or scoped automatic recycling in a connected personal OneDrive. Enter owned folder IDs explicitly and verify them against the current connected drive. Empty scope never falls back to the whole drive.')
        self._cloud_body = body
        self._check(body, 'cloud_review_enabled', 'Include personal OneDrive in maintenance')
        self.cloud_folders = QPlainTextEdit()
        self.cloud_folders.setPlaceholderText('Owned folder item IDs, one per line or comma-separated. Enter root only if you deliberately approve that entire owned drive scope.')
        self.cloud_folders.setPlainText('\n'.join(self.initial.get('cloud_folder_ids', [])))
        self.cloud_folders.setFixedHeight(100)
        body.addWidget(self.cloud_folders)
        verify = QPushButton('Verify these folders and bind the connected drive')
        verify.clicked.connect(self.verify_cloud_scope)
        body.addWidget(verify)
        self.cloud_scope_status = _label('No drive/folder binding verified in this setup session.')
        body.addWidget(self.cloud_scope_status)
        connect = QPushButton('Open OneDrive connection controls here')
        connect.clicked.connect(self.open_cloud_connection)
        body.addWidget(connect)
        self.cloud_policy = self._policy(body, 'cloud_policy')
        self._limits(body, 'cloud')
        self.cloud_consent = self._consent(body, 'cloud_changes_approved',
            'Allow automatic recycling of eligible extra copies in only this verified drive/folder scope, without per-run prompts')
        self.cloud_folders.textChanged.connect(self._invalidate_cloud_scope)
        self.cloud_policy.currentIndexChanged.connect(lambda: self.cloud_consent.setChecked(False))
        for key in ('cloud_max_recycle','cloud_min_age_days'):
            self.controls[key].valueChanged.connect(lambda: self.cloud_consent.setChecked(False))
        body.addWidget(_label('Provider hash/size matches are candidates, not downloaded byte comparisons. The engine checks the retained copy and remote identity before recycling. Only owned personal OneDrive folders are supported; shared/business drives, Google Drive and Dropbox are excluded. Restore through the OneDrive website recycle bin. Changing the account or scope requires new approval.'))
        body.addStretch()

    def _build_performance(self):
        body = self._page('performance', '7. Performance Optimizer',
            'Review process CPU/memory and priority/affinity information without closing applications. Optional Codex priority and background sampling are separate choices. Existing external priority watchers are preserved.')
        self._checkbox_actions(body, 'performance', lambda: [self.controls[key] for key in ('performance_review_enabled', 'codex_priority_enabled', 'background_analysis_enabled')])
        self._check(body, 'performance_review_enabled', 'Include a detailed process/resource review report')
        self._check(body, 'codex_priority_enabled', 'Apply the selected Codex priority during maintenance')
        priority = QComboBox()
        priority.addItems(['Normal', 'AboveNormal', 'High'])
        priority.setCurrentText(str(self.initial.get('codex_priority', 'High')))
        self.controls['codex_priority'] = priority
        body.addWidget(priority)
        self._check(body, 'background_analysis_enabled', 'Keep background resource analysis on while Optimus is running')
        interval = QSpinBox()
        interval.setRange(2, 60)
        interval.setSuffix(' seconds between samples')
        interval.setValue(int(self.initial.get('analysis_interval', 5)))
        self.controls['analysis_interval'] = interval
        body.addWidget(interval)
        body.addWidget(_label('Background analysis measures CPU, memory, disk I/O and network counters; it does not automatically stop or suspend programs. Saving applies the background analysis On/Off choice immediately; it also resumes an opt-in when the normal interface opens. Higher priority is not a promise of better performance.'))
        body.addStretch()

    def _build_startup(self):
        body = self._page('startup', '8. Startup Manager',
            'Review startup inventory, or automatically disable only the exact sign-in entries selected from a fresh inventory. Services are reported; this policy does not automatically stop services or change their startup modes.')
        self._check(body, 'startup_review_enabled', 'Include Startup Manager in maintenance')
        self.startup_policy = self._policy(body, 'startup_policy', [('Review inventory only','review'),('Disable only my approved entries','disable_selected')])
        load = QPushButton('Load current eligible startup entries')
        load.clicked.connect(self.load_startup_approvals)
        body.addWidget(load)
        self.startup_status = _label('No startup inventory loaded in this setup session.')
        body.addWidget(self.startup_status)
        self.startup_targets = QListWidget()
        self.startup_targets.setMinimumHeight(170)
        self._selection_buttons(body, 'startup',
            lambda: self._set_list_checks('startup', self.startup_targets, True, self.startup_status),
            lambda: self._set_list_checks('startup', self.startup_targets, False, self.startup_status))
        body.addWidget(self.startup_targets)
        self.startup_consent = self._consent(body, 'startup_changes_approved',
            'Allow these exact checked entries to be disabled automatically, without per-run prompts')
        self.startup_targets.itemChanged.connect(lambda: self.startup_consent.setChecked(False))
        self.startup_policy.currentIndexChanged.connect(lambda: self.startup_consent.setChecked(False))
        body.addWidget(_label('Command, registry/shortcut identity and fingerprint must still match the saved snapshot. Changed/new entries need a new setup approval; protected/security/system entries and Bing Wallpaper are excluded. Backups and supported Restore/history remain available.'))
        body.addStretch()

    def _build_software(self):
        body = self._page('software', '9. Software Updater',
            'Load current WinGet application updates and select exact packages, or authorize all supported installed WinGet applications with explicit exclusions. Review only never installs. All-supported approval also covers future matching installed applications.')
        self._check(body, 'software_updates_enabled', 'Include the Software Updater stage')
        self.software_policy = QComboBox()
        self.software_policy.addItem('Scan and report only', 'review')
        self.software_policy.addItem('Install only my selected/listed exact package IDs', 'selected')
        self.software_policy.addItem('All supported installed WinGet applications (including future matching applications)', 'all_supported')
        self.software_policy.setCurrentIndex(max(0, self.software_policy.findData(self.initial.get('software_update_policy', 'review'))))
        body.addWidget(self.software_policy)
        load = QPushButton('Load current WinGet updates to choose packages')
        load.clicked.connect(self.load_software_candidates)
        body.addWidget(load)
        self.software_status = _label('No software scan loaded. Loading the list does not install anything.')
        body.addWidget(self.software_status)
        self.software_targets = QListWidget()
        self.software_targets.setMinimumHeight(135)
        self._selection_buttons(body, 'software', self._select_all_software,
            lambda: self._set_list_checks('software', self.software_targets, False, self.software_status))
        body.addWidget(self.software_targets)
        body.addWidget(_label('Advanced: other exact WinGet package IDs, one per line or comma-separated. Checked packages above are included automatically.'))
        self.software_ids = QPlainTextEdit()
        self.software_ids.setPlaceholderText('Exact IDs, for example Vendor.Application. Known listed IDs move into the checklist when loaded.')
        self.software_ids.setPlainText('\n'.join(self.initial.get('software_update_ids', [])))
        self.software_ids.setFixedHeight(80)
        body.addWidget(self.software_ids)
        body.addWidget(_label('All-supported exclusions: these exact package IDs must never be included by that policy.'))
        self.software_exclusions = QPlainTextEdit()
        self.software_exclusions.setPlainText('\n'.join(self.initial.get('software_update_exclude_ids', [])))
        self.software_exclusions.setFixedHeight(75)
        body.addWidget(self.software_exclusions)
        self.software_all_consent = self._check(body, 'software_update_all_approved',
            'Approve all supported installed WinGet applications, including future matching applications, except my exclusions')
        self.software_all_consent.setStyleSheet('color:#ffd29c;')
        self.software_agreements = self._check(body, 'software_update_agreements_accepted',
            'Allow silent installation of my selected packages and accept their package/source agreements')
        body.addWidget(_label('All-supported mode includes only recognized complete WinGet-source identities. Pins, excluded IDs, unknown/truncated/ambiguous entries and unsupported sources are skipped. Vendors may need administrator access or closed apps. No automatic reboot is requested.'))
        self.software_policy.currentIndexChanged.connect(self._software_policy_changed)
        self.controls['software_updates_enabled'].toggled.connect(self._software_controls)
        self.software_targets.itemChanged.connect(lambda: self.software_agreements.setChecked(False))
        self.software_ids.textChanged.connect(lambda: self.software_agreements.setChecked(False))
        self.software_exclusions.textChanged.connect(self._software_scope_changed)
        self._software_controls()
        body.addStretch()

    def _software_controls(self, *unused):
        enabled = self.controls['software_updates_enabled'].isChecked()
        selected = enabled and self.software_policy.currentData() == 'selected'
        all_supported = enabled and self.software_policy.currentData() == 'all_supported'
        self.software_ids.setEnabled(selected)
        self.software_targets.setEnabled(selected)
        self.software_exclusions.setEnabled(all_supported)
        self.software_all_consent.setEnabled(all_supported)
        self.software_agreements.setEnabled(selected or all_supported)
        self.software_agreements.setText('Allow silent installation of supported WinGet applications and accept their package/source agreements' if all_supported else 'Allow silent installation of my selected/listed packages and accept their package/source agreements')
        if hasattr(self, 'hardware_targets'):
            self._sync_selection_actions()

    def _software_policy_changed(self, *unused):
        self.software_all_consent.setChecked(False)
        self.software_agreements.setChecked(False)
        self._software_controls()

    def _software_scope_changed(self):
        self.software_all_consent.setChecked(False)
        self.software_agreements.setChecked(False)

    def load_software_candidates(self):
        self._start_probe(self.backend.updates_scan, (), {}, self._software_candidates_ready, self.software_status)

    def _software_candidates_ready(self, result):
        checked_ids = [item['id'] for item in self._checked(self.software_targets)]
        previous = {value.casefold() for value in checked_ids}
        tokens = [value.strip() for value in re.split(r'[\r\n,]+', self.software_ids.toPlainText()) if value.strip()]
        previous.update(value.casefold() for value in tokens)
        preserved_tokens = list(dict.fromkeys(tokens+checked_ids))
        self.software_targets.blockSignals(True)
        self.software_targets.clear()
        candidates = result.get('packages', result.get('updates', []))
        complete = result.get('structured') is True and result.get('exitCode') == 0 and isinstance(candidates, list)
        if not complete:
            self.software_targets.blockSignals(False)
            self.software_ids.setPlainText('\n'.join(preserved_tokens))
            self.software_status.setText('Selected IDs were retained in the advanced field. WinGet did not return a complete recognized update list. No rows can be selected. Review Updates for its output; advanced exact IDs remain available.')
            self._sync_selection_actions()
            return
        from .automated_software import fresh_candidates
        accepted = fresh_candidates(result)
        selectable = {identity.casefold() for identity in accepted}
        for identifier, record in accepted.items():
            item = QListWidgetItem(str(record.get('name') or identifier)+' | '+identifier+' | '+str(record.get('version','?'))+' -> '+str(record.get('available','?')))
            item.setData(Qt.UserRole, dict(record,id=identifier))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if identifier.casefold() in previous else Qt.Unchecked)
            self.software_targets.addItem(item)
        self.software_targets.blockSignals(False)
        self.software_ids.setPlainText('\n'.join(value for value in preserved_tokens if value.casefold() not in selectable))
        self.software_agreements.setChecked(False)
        self.software_status.setText(str(self.software_targets.count())+' recognized update candidates. Check exact packages for Selected mode. All-supported mode uses its separately approved scope and exclusions at each run.')
        self._software_controls()

    def _build_drivers(self):
        body = self._page('drivers', '10. Driver Updater',
            'Load fresh Windows Update driver candidates. Review only, approve exact update IDs/revisions, or approve future updates for selected exact hardware/provider/class combinations. No unlisted device or provider is authorized.')
        self._check(body, 'driver_updates_enabled', 'Include Driver Updater in maintenance')
        self.driver_policy = self._policy(body, 'driver_policy', [('Scan and report only','review'),('Install my exact approved update revisions','install_approved'),('Install matching future updates for my approved devices','install_matching')])
        load = QPushButton('Load available Windows Update drivers')
        load.clicked.connect(self.load_driver_approvals)
        self.driver_offer_load_button=load
        body.addWidget(load)
        admin = QPushButton('Restart Optimus as administrator (Windows UAC)')
        admin.clicked.connect(self.request_administrator)
        body.addWidget(admin)
        self.driver_status = _label('No Windows Update driver candidates loaded in this setup session.')
        body.addWidget(self.driver_status)
        self.driver_targets = QListWidget()
        self.driver_targets.setMinimumHeight(180)
        self._selection_buttons(body, 'drivers',
            lambda: self._set_list_checks('drivers', self.driver_targets, True, self.driver_status),
            lambda: self._set_list_checks('drivers', self.driver_targets, False, self.driver_status))
        body.addWidget(self.driver_targets)
        self.driver_details = QPlainTextEdit()
        self.driver_details.setReadOnly(True)
        self.driver_details.setMaximumHeight(110)
        body.addWidget(self.driver_details)
        self.hardware_load_button=QPushButton('Load installed hardware for future matching updates')
        self.hardware_load_button.clicked.connect(self.load_hardware_approvals)
        body.addWidget(self.hardware_load_button)
        self.hardware_status=_label('Present-device approvals allow future matching updates even when Windows Update has no offer now. Only known installed provider/class/date metadata is eligible.')
        body.addWidget(self.hardware_status)
        self.hardware_targets=QListWidget()
        self.hardware_targets.setMinimumHeight(145)
        self._selection_buttons(body, 'hardware', self.select_all_hardware, self.clear_all_hardware)
        self.hardware_select_button = self.selection_actions['hardware']['select']
        self.hardware_select_button.setToolTip('Select all eligible present devices, up to 2048 unique approval rules.')
        body.addWidget(self.hardware_targets)

        self.driver_consent = self._consent(body, 'driver_changes_approved',
            'Allow installation of only my checked exact updates or approved matching devices, without per-run prompts')
        self.driver_agreements = self._consent(body, 'driver_update_agreements_accepted',
            'Accept the license agreements for these approved driver updates, including future matching updates only in matching-device mode')
        self.driver_policy.currentIndexChanged.connect(self._driver_policy_changed)
        self.driver_targets.itemChanged.connect(self._driver_selection_changed)
        self.hardware_targets.itemChanged.connect(self._driver_selection_changed)
        self.driver_targets.currentItemChanged.connect(self._driver_item_details)
        matching = self.driver_policy.currentData()=='install_matching'
        self.hardware_load_button.setEnabled(matching)
        self.hardware_select_button.setEnabled(matching)
        self.hardware_targets.setEnabled(matching)
        self._driver_visibility()
        self._sync_selection_actions()
        body.addWidget(_label('Exact mode does not approve a later revision. Matching mode requires complete hardware ID, provider, driver class and a driver-date floor to exclude older offers. A hardware/compatible ID may cover multiple identical devices. New devices/providers need new approval. Interactive, ambiguous or mandatory-immediate-restart offers are skipped. Installation needs an already elevated Optimus process; automatic runs report administrator access needed rather than silently requesting UAC. No blanket driver removal or automatic reboot is requested.'))
        body.addStretch()

    def _policy(self, body, key, choices=None):
        combo = QComboBox()
        for text, value in choices or [('Scan and report only','review'),('Recycle extra copies; keep oldest','keep_oldest'),('Recycle extra copies; keep newest','keep_newest')]:
            combo.addItem(text, value)
        selected = self.initial.get(key, 'review') if int(self.initial.get('setup_version', 0)) >= SETUP_VERSION else 'review'
        combo.setCurrentIndex(max(0, combo.findData(selected)))
        body.addWidget(combo)
        return combo

    def _consent(self, body, key, text):
        control = self._check(body, key, text)
        if int(self.initial.get('setup_version', 0)) < SETUP_VERSION:
            control.setChecked(False)
        control.setStyleSheet('color:#ffd29c;')
        return control

    def _limits(self, body, prefix):
        row = QHBoxLayout()
        row.addWidget(_label('Maximum recycled files per run'))
        maximum = QSpinBox()
        maximum.setRange(1, 500)
        maximum.setValue(int(self.initial.get(prefix+'_max_recycle', 25)))
        self.controls[prefix+'_max_recycle'] = maximum
        row.addWidget(maximum)
        row.addWidget(_label('Minimum age'))
        age = QSpinBox()
        age.setRange(1, 3650)
        age.setSuffix(' days')
        age.setValue(int(self.initial.get(prefix+'_min_age_days', 7)))
        self.controls[prefix+'_min_age_days'] = age
        row.addWidget(age)
        body.addLayout(row)

    def _run(self, operation, *args, done=None, **kwargs):
        # Existing CloudPanel is embedded in connection-only mode; no file actions.
        allowed = {'cloud_status','cloud_configure','cloud_connect','cloud_poll','cloud_logout'}
        if operation not in allowed:
            self._error('File actions are unavailable in setup connection controls.')
            return
        if operation in {'cloud_configure','cloud_connect','cloud_logout'}:
            self._invalidate_cloud_scope()
        self._start_probe(getattr(self.backend, operation), args, kwargs, done, self.cloud_scope_status)

    def _error(self, message):
        self.cloud_scope_status.setText(str(message))

    def _start_probe(self, operation, args, kwargs, callback, status):
        if self.busy or self._closing:
            status.setText('Wait for the current inventory/account request to finish.')
            return
        self.busy = True
        self._sync_selection_actions()
        self._probe_callback, self._probe_status = callback, status
        status.setText('Loading current information...')
        for button in (QWizard.NextButton,QWizard.BackButton,QWizard.FinishButton):
            self.button(button).setEnabled(False)
        worker = InventoryProbe(operation, args, kwargs)
        self._probe_worker = worker
        worker.signals.result.connect(self._probe_result)
        worker.signals.error.connect(self._probe_error)
        worker.signals.finished.connect(self._probe_finished)
        QThreadPool.globalInstance().start(worker)

    @Slot(object)
    def _probe_result(self, result):
        if not self._closing and self._probe_callback:
            try:
                self._probe_callback(result)
            except Exception as error:
                self._probe_error(str(error))

    @Slot(str)
    def _probe_error(self, error):
        if not self._closing and self._probe_status is not None:
            self._probe_status.setText('Could not verify inventory: ' + error)

    @Slot()
    def _probe_finished(self):
        self.busy = False
        self._sync_selection_actions()
        self._probe_worker = None
        self._probe_callback = None
        if not self._closing:
            for button in (QWizard.NextButton,QWizard.BackButton,QWizard.FinishButton):
                self.button(button).setEnabled(True)

    def _cloud_inputs(self):
        return list(dict.fromkeys(value.strip() for value in re.split(r'[\r\n,]+', self.cloud_folders.toPlainText()) if value.strip()))

    def _invalidate_cloud_scope(self):
        self._cloud_binding = None
        self.cloud_consent.setChecked(False)
        self.cloud_scope_status.setText('Scope changed. Verify these exact folders again before approving automatic recycling.')

    def verify_cloud_scope(self):
        folder_ids = self._cloud_inputs()
        self._invalidate_cloud_scope()
        if len(folder_ids) > 20:
            self.cloud_scope_status.setText('Choose at most 20 explicit folder IDs per approved scope.')
            return
        if not folder_ids:
            self.cloud_scope_status.setText('Enter at least one explicit owned folder ID. An empty scope never selects root.')
            return
        def verify():
            return {'inputs':folder_ids, 'scopes':[self.backend.cloud_approval_scope(value) for value in folder_ids]}
        self._start_probe(verify, (), {}, self._cloud_scope_ready, self.cloud_scope_status)

    def _cloud_scope_ready(self, response):
        if response.get('inputs') != self._cloud_inputs():
            self.cloud_scope_status.setText('Folder IDs changed while loading; verify the edited scope again.')
            return
        scopes = response.get('scopes', [])
        drives = {value.get('drive_id') for value in scopes}
        if not scopes or len(drives) != 1 or not next(iter(drives)) or any(not value.get('folder_id') or str(value['folder_id']).casefold() == 'root' or value.get('provider') != 'Personal OneDrive' for value in scopes):
            raise ValueError('All folders must resolve to concrete owned folders in one verified personal OneDrive.')
        self._cloud_binding = {'drive_id':next(iter(drives)), 'folder_ids':list(dict.fromkeys(value['folder_id'] for value in scopes)), 'inputs':list(response['inputs']), 'drive_name':str(scopes[0].get('drive_name') or ''), 'scope_labels':[str(value.get('scope_label') or value.get('folder_name') or value['folder_id']) for value in scopes]}
        self.cloud_consent.setChecked(False)
        self.cloud_scope_status.setText('Verified drive: '+str(scopes[0].get('drive_name') or self._cloud_binding['drive_id'])+' ['+self._cloud_binding['drive_id']+']\nFolders: '+'; '.join(str(value.get('folder_name') or value['folder_id'])+' ['+value['folder_id']+']' for value in scopes))

    def open_cloud_connection(self):
        self._invalidate_cloud_scope()
        if self._connection_panel is None:
            from .widgets.cloud_panel import CloudPanel
            panel = CloudPanel(self)
            panel.layout().itemAt(0).widget().setText('Connect personal OneDrive here. Explicit connection settings/sign-in actions are saved separately; Cancel setup leaves maintenance choices unchanged.')
            def hide_item(item):
                if item.widget():
                    item.widget().hide()
                if item.layout():
                    for index in range(item.layout().count()):
                        hide_item(item.layout().itemAt(index))
            for index in range(6, panel.layout().count()):
                hide_item(panel.layout().itemAt(index))
            self._connection_panel = panel
            self._cloud_body.insertWidget(self._cloud_body.count()-1, panel)
        self._connection_panel.show()
        self._connection_panel.refresh()

    def load_startup_approvals(self):
        self._startup_loaded = False
        self.startup_consent.setChecked(False)
        self._start_probe(self.backend.startup_list, (), {}, self._startup_ready, self.startup_status)

    def _startup_ready(self, records):
        from .automated_system import startup_descriptor
        self.startup_targets.blockSignals(True)
        self.startup_targets.clear()
        approved = self.initial.get('startup_approved_entries', []) if int(self.initial.get('setup_version',0)) >= SETUP_VERSION else []
        for record in records or []:
            try:
                descriptor = startup_descriptor(record)
            except ValueError:
                continue
            name = str(record.get('Name') or record.get('name') or descriptor.get('Name') or descriptor.get('stableId') or 'Startup entry')
            item = QListWidgetItem(name+' | '+str(record.get('Command') or record.get('command') or ''))
            item.setData(Qt.UserRole, descriptor)
            item.setToolTip(json.dumps(descriptor, indent=2))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if descriptor in approved else Qt.Unchecked)
            self.startup_targets.addItem(item)
        self.startup_targets.blockSignals(False)
        self._startup_loaded = True
        self.startup_consent.setChecked(False)
        self.startup_status.setText(str(self.startup_targets.count())+' eligible current entries. Check exact entries, then approve below. Protected/ineligible entries were excluded.')
        self._sync_selection_actions()

    def load_driver_approvals(self):
        self._drivers_loaded = False
        self._driver_selection_changed()
        self._start_probe(self.backend.driver_updates_scan, (), {}, self._drivers_ready, self.driver_status)

    def _drivers_ready(self, response):
        from .automated_system import driver_descriptor, driver_device_rule
        if response.get('resultCode') != 2 or not isinstance(response.get('updates'), list):
            raise ValueError('Windows Update did not return a complete successful candidate scan; no driver approvals were loaded.')
        self.driver_targets.blockSignals(True)
        self.driver_targets.clear()
        for record in response.get('updates', []):
            try:
                descriptor = driver_descriptor(record)
            except ValueError:
                continue
            try:
                device = driver_device_rule(record)
            except ValueError:
                device = None
            item = QListWidgetItem(str(record.get('title') or descriptor.get('title'))+' | revision '+str(descriptor.get('revision')))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(Qt.UserRole, {'descriptor':descriptor,'device':device,'record':record})
            item.setToolTip(json.dumps(record, indent=2))
            selected = int(self.initial.get('setup_version',0)) >= SETUP_VERSION and self.driver_policy.currentData() != 'install_matching' and descriptor in self.initial.get('driver_approved_updates', [])
            item.setCheckState(Qt.Checked if selected else Qt.Unchecked)
            self.driver_targets.addItem(item)
        self.driver_targets.blockSignals(False)
        self._drivers_loaded = True
        self._driver_policy_changed(inventory_refresh=True)
        self.driver_status.setText(str(self.driver_targets.count())+' current driver candidates. Check exact updates/devices, then approve installation and agreements.')

    def _driver_policy_changed(self, *unused, inventory_refresh=False):
        self._driver_selection_changed()
        matching = self.driver_policy.currentData() == 'install_matching'
        self.hardware_load_button.setEnabled(matching)
        self.hardware_select_button.setEnabled(matching)
        self.hardware_targets.setEnabled(matching)
        self._driver_visibility()
        if not inventory_refresh:
            self._hardware_loaded=False
            self.hardware_targets.clear()
            if matching:
                self.driver_targets.blockSignals(True)
                for index in range(self.driver_targets.count()):
                    self.driver_targets.item(index).setCheckState(Qt.Unchecked)
                self.driver_targets.blockSignals(False)
        self.driver_targets.blockSignals(True)
        for index in range(self.driver_targets.count()):
            item = self.driver_targets.item(index)
            eligible = not matching or item.data(Qt.UserRole).get('device') is not None
            flags = item.flags() | Qt.ItemIsEnabled
            if not eligible:
                flags &= ~Qt.ItemIsEnabled
                item.setCheckState(Qt.Unchecked)
                item.setToolTip('Matching mode requires complete hardware ID, provider, driver class and driver date. Use exact revision mode or leave this candidate unapproved.')
            item.setFlags(flags)
        self.driver_targets.blockSignals(False)
        self._sync_selection_actions()

    def _driver_visibility(self):
        matching=self.driver_policy.currentData()=='install_matching'
        for widget in (self.driver_offer_load_button,self.driver_status,self.driver_targets,self.driver_details,self.selection_actions['drivers']['group']):
            widget.setVisible(not matching)
        for widget in (self.hardware_load_button,self.hardware_status,self.hardware_targets,self.selection_actions['hardware']['group']):
            widget.setVisible(matching)

    def _driver_selection_changed(self, *unused):
        self.driver_consent.setChecked(False)
        self.driver_agreements.setChecked(False)

    def _driver_item_details(self, item, previous=None):
        self.driver_details.setPlainText(json.dumps(item.data(Qt.UserRole), indent=2) if item else '')

    def request_administrator(self):
        self._start_probe(self.backend.elevate, (), {}, lambda result:self.driver_status.setText('Administrator restart requested. Windows UAC controls consent. Save setup choices in the elevated session.'), self.driver_status)

    @staticmethod
    def _checked(table):
        return [copy.deepcopy(table.item(index).data(Qt.UserRole)) for index in range(table.count()) if table.item(index).checkState() == Qt.Checked and table.item(index).flags() & Qt.ItemIsEnabled]

    def load_hardware_approvals(self):
        self._hardware_loaded=False
        self._driver_selection_changed()
        self._start_probe(self.backend.driver_devices, (), {}, self._hardware_ready, self.hardware_status)

    def _hardware_ready(self, report):
        from .automated_system import validate_driver_devices
        self.hardware_targets.blockSignals(True)
        self.hardware_targets.clear()
        saved=self.initial.get('driver_approved_devices',[]) if int(self.initial.get('setup_version',0))>=SETUP_VERSION else []
        for device in report.get('devices',[]):
            try:
                rules=validate_driver_devices(device.get('approvalRules',[]))
            except ValueError:
                rules=[]
            name=str(device.get('displayName') or device.get('deviceId') or 'Device')
            item=QListWidgetItem(name+(' | '+str(rules[0]['provider'])+' | '+str(rules[0]['class']) if rules else ' | approval unavailable'))
            item.setData(Qt.UserRole, {'rules':rules,'device':device})
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            if not rules:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setToolTip(str(device.get('approvalUnavailableReason') or 'Known installed hardware/provider/class/date metadata is required.'))
            else:
                item.setToolTip('\n'.join(rule['hardwareId']+' | '+rule['provider']+' | '+rule['class']+' | not older than '+rule['notOlderThanDriverDate'] for rule in rules))
            selected=bool(rules) and all(any(all(rule[key].casefold()==old[key].casefold() for key in ('hardwareId','provider','class')) and rule['notOlderThanDriverDate']>=old['notOlderThanDriverDate'] for old in saved) for rule in rules)
            item.setCheckState(Qt.Checked if selected else Qt.Unchecked)
            self.hardware_targets.addItem(item)
        self.hardware_targets.blockSignals(False)
        self._hardware_loaded=True
        self._driver_selection_changed()
        self.hardware_status.setText(str(self.hardware_targets.count())+' present devices loaded. Unavailable devices remain unchecked. Selecting a device approves only its listed exact hardware/compatible IDs, provider, class and installed date floor.')
        self._sync_selection_actions()

    @staticmethod
    def _rule_key(rule):
        return tuple(rule[name].casefold() for name in ('hardwareId','provider','class'))

    def select_all_hardware(self):
        if self.busy or not self.hardware_targets.isEnabled():
            return
        keys=set()
        selected,skipped=0,0
        self.hardware_targets.blockSignals(True)
        for index in range(self.hardware_targets.count()):
            item=self.hardware_targets.item(index)
            if not self._selectable(item):
                skipped += 1
                continue
            rules=item.data(Qt.UserRole).get('rules',[])
            new={self._rule_key(rule) for rule in rules}
            eligible=bool(rules) and bool(item.flags() & Qt.ItemIsEnabled) and len(keys|new)<=DEVICE_RULE_LIMIT
            item.setCheckState(Qt.Checked if eligible else Qt.Unchecked)
            if eligible:
                keys|=new;selected+=1
            else:
                skipped+=1
        self.hardware_targets.blockSignals(False)
        self._driver_selection_changed()
        self.hardware_status.setText(str(selected)+' devices selected, '+str(len(keys))+' unique rules. '+str(skipped)+' devices remain unapproved because metadata is missing or the 2048-rule limit would be exceeded. Review then approve installation and agreements below.')

    def clear_all_hardware(self):
        if self.busy or not self.hardware_targets.isEnabled():
            return
        for index in range(self.hardware_targets.count()):
            item = self.hardware_targets.item(index)
            if self._selectable(item):
                item.setCheckState(Qt.Unchecked)
        self.hardware_status.setText('Eligible device choices cleared. Review any remaining selections before approving installation and agreements.')

    def _driver_rules(self):
        rules = {}
        selected=[] if self.driver_policy.currentData() == 'install_matching' else [item['device'] for item in self._checked(self.driver_targets) if item.get('device')]
        for item in self._checked(self.hardware_targets):
            selected.extend(item.get('rules',[]))
        for rule in selected:
            key = self._rule_key(rule)
            if key not in rules or rule['notOlderThanDriverDate'] > rules[key]['notOlderThanDriverDate']:
                rules[key] = rule
        return list(rules.values())

    def reject(self):
        self._closing = True
        if self._connection_panel is not None:
            self._connection_panel._timer.stop()
        super().reject()

    def _build_summary(self):
        body = self._page('summary', '11. Review your choices',
            'Save choices writes this configuration. It does not start a maintenance run unless you also select Run selected maintenance now below. Automatic cadence applies to the selected operations while the app is open.', on_enter=self.refresh_summary)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMinimumHeight(300)
        body.addWidget(self.summary, 1)
        self.run_now_checkbox = QCheckBox('Run selected maintenance now')
        self.run_now_checkbox.setChecked(False)
        body.addWidget(self.run_now_checkbox)
        self.save_error = _label('')
        self.save_error.setStyleSheet('color:#ffb9a9;')
        body.addWidget(self.save_error)
        self.refresh_summary()

    def _roots(self):
        roots = []
        for entry in self.duplicate_roots.toPlainText().splitlines():
            value = os.path.expandvars(entry.strip().strip('"'))
            if not value:
                continue
            if not ntpath.isabs(value) or value.startswith(('\\\\', '//')):
                raise ValueError('Duplicate Finder needs absolute local folder paths, one per line.')
            if value.casefold() not in {item.casefold() for item in roots}:
                roots.append(value)
        return roots

    @staticmethod
    def _exact_ids(text):
        values = []
        for entry in re.split(r'[\r\n,]+', text):
            value = entry.strip()
            if not value:
                continue
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]{1,127}', value):
                raise ValueError('Enter exact WinGet package IDs only, separated by new lines or commas. Do not enter commands, switches or URLs.')
            if value.casefold() not in {item.casefold() for item in values}:
                values.append(value)
        if len(values)>100:
            raise ValueError('Choose at most 100 exact WinGet package IDs.')
        return values

    def _package_ids(self):
        values = self._exact_ids(self.software_ids.toPlainText())
        values.extend(item['id'] for item in self._checked(self.software_targets) if item['id'].casefold() not in {value.casefold() for value in values})
        if len(values)>100:
            raise ValueError('Choose at most 100 exact WinGet package IDs across the checklist and advanced list.')
        return values

    def collect_choices(self, *, validate=True, section=None):
        result = copy.deepcopy(self.initial)
        for key, control in self.controls.items():
            if isinstance(control, QCheckBox):
                result[key] = control.isChecked()
            elif isinstance(control, QSpinBox):
                result[key] = control.value()
            elif isinstance(control, QComboBox):
                result[key] = control.currentText()
        result.update({'setup_version':SETUP_VERSION, 'automation_mode':next(mode for mode,button in self.cadence_buttons.items() if button.isChecked()),
            'cleanup_category_ids':[key for key,control in self.category_controls.items() if control.isChecked()],
            'browser_cleanup_browsers':[key for key,control in self.browser_controls.items() if control.isChecked()],
            'software_update_policy':self.software_policy.currentData()})
        raw_domains = [value.strip() for value in re.split(r'[\r\n,]+', self.browser_keep_domains.toPlainText()) if value.strip()]
        if validate:
            from .browser_privacy import _domains
            result['browser_keep_domains'] = _domains(raw_domains)
            if section in (None, 'cleanup') and any(result[key] for key in ('browser_cache_enabled', 'browser_history_enabled', 'browser_cookies_enabled', 'browser_download_history_enabled')) and not result['browser_cleanup_browsers']:
                raise ValueError('Choose at least one browser for the enabled browser cleanup categories.')
        else:
            result['browser_keep_domains'] = raw_domains
        if validate:
            result['duplicate_roots'] = self._roots()
            result['software_update_ids'] = self._package_ids()
            result['software_update_exclude_ids'] = self._exact_ids(self.software_exclusions.toPlainText())
            if section in (None,'duplicates') and result['duplicates_enabled'] and not result['duplicate_roots']:
                raise ValueError('Add at least one local folder for Duplicate Finder, or turn its scan off.')
            selected = result['software_updates_enabled'] and result['software_update_policy'] == 'selected'
            if section in (None,'software') and selected and not result['software_update_ids']:
                raise ValueError('List at least one exact WinGet package ID, or choose scan/report only.')
            if section in (None,'software') and result['software_updates_enabled'] and result['software_update_policy']=='all_supported' and (not result['software_update_all_approved'] or not result['software_update_agreements_accepted']):
                raise ValueError('All-supported updates require explicit approval of future matching applications and package/source agreements.')
            if section in (None,'software') and selected and not result['software_update_agreements_accepted']:
                raise ValueError('Selected-package installation requires your explicit package/source agreement authorization. Choose review-only to avoid accepting agreements.')
        else:
            result['duplicate_roots'] = [entry.strip() for entry in self.duplicate_roots.toPlainText().splitlines() if entry.strip()]
            result['software_update_ids'] = [entry.strip() for entry in re.split(r'[\r\n,]+',self.software_ids.toPlainText()) if entry.strip()]
            result['software_update_ids'] += [item['id'] for item in self._checked(self.software_targets) if item['id'] not in result['software_update_ids']]
            result['software_update_exclude_ids'] = [entry.strip() for entry in re.split(r'[\r\n,]+',self.software_exclusions.toPlainText()) if entry.strip()]
        result.update({'duplicate_policy':self.duplicate_policy.currentData(),
                       'cloud_policy':self.cloud_policy.currentData(),
                       'startup_policy':self.startup_policy.currentData(),
                       'driver_policy':self.driver_policy.currentData(),
                       'cloud_drive_id':self._cloud_binding['drive_id'] if self._cloud_binding else '',
                       'cloud_folder_ids':list(self._cloud_binding['folder_ids']) if self._cloud_binding else [],
                       'startup_approved_entries':self._checked(self.startup_targets),
                       'driver_approved_updates':[item['descriptor'] for item in self._checked(self.driver_targets)],
                       'driver_approved_devices':self._driver_rules()})
        if validate:
            if section in (None,'duplicates') and result['duplicates_enabled'] and result['duplicate_policy'] != 'review' and not result['duplicate_changes_approved']:
                raise ValueError('Approve automatic duplicate recycling for these exact folders, or choose Review.')
            if section in (None,'cloud') and result['cloud_review_enabled']:
                if not self._cloud_binding or self._cloud_binding['inputs'] != self._cloud_inputs():
                    raise ValueError('Verify an explicit owned OneDrive drive/folder scope, or turn the cloud stage off.')
                if result['cloud_policy'] != 'review' and not result['cloud_changes_approved']:
                    raise ValueError('Approve automatic cloud recycling for the verified scope, or choose Review.')
            if section in (None,'startup') and result['startup_review_enabled'] and result['startup_policy'] != 'review':
                if not self._startup_loaded or not result['startup_approved_entries'] or not result['startup_changes_approved']:
                    raise ValueError('Load current startup entries, select exact entries and approve disabling, or choose Review.')
            if section in (None,'drivers') and result['driver_updates_enabled'] and result['driver_policy'] != 'review':
                loaded = self._drivers_loaded if result['driver_policy']=='install_approved' else self._hardware_loaded
                stale_hardware = bool(self._checked(self.hardware_targets)) and not self._hardware_loaded
                if not loaded or stale_hardware or not result['driver_changes_approved'] or not result['driver_update_agreements_accepted']:
                    raise ValueError('Load fresh drivers, select updates/devices and approve installation plus agreements, or choose Review.')
                key = 'driver_approved_devices' if result['driver_policy'] == 'install_matching' else 'driver_approved_updates'
                if not result[key]:
                    raise ValueError('Select at least one eligible exact driver update or complete matching-device rule.')
                if len(result['driver_approved_devices'])>DEVICE_RULE_LIMIT:
                    raise ValueError('Approve at most 2048 unique hardware/provider/class rules; deselect devices or offers to stay within the limit.')
        for enabled, policy, consent in (('duplicates_enabled','duplicate_policy','duplicate_changes_approved'),('cloud_review_enabled','cloud_policy','cloud_changes_approved'),('startup_review_enabled','startup_policy','startup_changes_approved'),('driver_updates_enabled','driver_policy','driver_changes_approved')):
            if not result[enabled] or result[policy] == 'review':
                result[consent] = False
        if not result['driver_changes_approved']:
            result['driver_update_agreements_accepted'] = False

        # Review-only never grants agreement acceptance.
        if result['software_update_policy'] not in ('selected','all_supported') or not result['software_updates_enabled']:
            result['software_update_agreements_accepted'] = False
        if result['software_update_policy'] != 'all_supported' or not result['software_updates_enabled']:
            result['software_update_all_approved'] = False
        return result

    def refresh_summary(self):
        settings = self.collect_choices(validate=False)
        actions = []
        if settings.get('cleanup_enabled'):
            actions.append('Permanently delete eligible old user temporary files')
        if settings.get('browser_cache_enabled'):
            actions.append('Permanently delete eligible browser-cache files')
        for key, label in (('browser_history_enabled', 'browsing history'), ('browser_cookies_enabled', 'cookies and site sign-ins'), ('browser_download_history_enabled', 'download-list records (downloaded files stay)')):
            if settings.get(key):
                actions.append('Clear selected browsers\' ' + label + ' across all ages, with private local database backups')
        if any(settings.get(key) for key in ('browser_cache_enabled', 'browser_history_enabled', 'browser_cookies_enabled', 'browser_download_history_enabled')):
            actions.append('Browsers: ' + (', '.join(settings['browser_cleanup_browsers']) or '[none selected]') + '. Each open browser asks Close and clean or Skip for this run.')
            if settings['browser_keep_domains']:
                actions.append('Keep cookies for domains and their subdomains: ' + ', '.join(settings['browser_keep_domains']))
        for identifier in settings.get('cleanup_category_ids', []):
            actions.append('Permanently clean: ' + self.category_names.get(identifier, identifier))
        for key, text in REMOVALS:
            if settings.get(key):
                actions.append(text)
        if settings.get('codex_priority_enabled'):
            actions.append('Set the optional Codex priority to ' + settings['codex_priority'])
        if settings.get('background_analysis_enabled'):
            actions.append('Keep read-only background analysis on at ' + str(settings['analysis_interval']) + '-second intervals')
        if settings.get('software_updates_enabled') and settings['software_update_policy'] == 'selected':
            actions.append('Install only listed WinGet IDs: ' + (', '.join(settings['software_update_ids']) or '[no IDs entered]'))
            actions.append('Package/source agreements: ' + ('explicitly accepted' if settings['software_update_agreements_accepted'] else 'NOT accepted; Save will be refused'))
        if settings.get('software_updates_enabled') and settings['software_update_policy']=='all_supported':
            actions.append('Update all supported installed WinGet applications, including future matching applications. Exclusions: '+(', '.join(settings['software_update_exclude_ids']) or 'none'))
            actions.append('All-supported scope: '+('approved' if settings['software_update_all_approved'] else 'approval required')+'; package/source agreements: '+('accepted' if settings['software_update_agreements_accepted'] else 'not accepted'))
        policy_labels = {'review':'Review only', 'keep_oldest':'Keep oldest modified copy',
            'keep_newest':'Keep newest modified copy', 'disable_selected':'Disable selected sign-in entries',
            'install_approved':'Install exact approved update revisions',
            'install_matching':'Install matching future updates for approved hardware, provider and class'}
        for enabled, policy, label in (('duplicates_enabled','duplicate_policy','Local duplicate recycling'),('cloud_review_enabled','cloud_policy','OneDrive recycling'),('startup_review_enabled','startup_policy','Startup'),('driver_updates_enabled','driver_policy','Drivers')):
            if settings.get(enabled) and settings[policy] != 'review':
                actions.append(label+': '+policy_labels[settings[policy]])
        if settings.get('duplicates_enabled') and settings['duplicate_policy'] != 'review':
            actions.append('Local folders: '+ '; '.join(settings['duplicate_roots'])+'; minimum age '+str(settings['duplicate_min_age_days'])+' days; maximum '+str(settings['duplicate_max_recycle'])+' files per run. '+('Recycling approved.' if settings['duplicate_changes_approved'] else 'Approval still required.'))
        cloud_scope_text = None
        if settings.get('cloud_review_enabled'):
            binding = self._cloud_binding or {}
            drive = (binding.get('drive_name')+' ' if binding.get('drive_name') else '')+'[drive ID: '+(settings['cloud_drive_id'] or 'not verified')+']'
            names = binding.get('scope_labels', [])
            folders = [((names[index]+' ') if index<len(names) else '')+'[folder ID: '+identity+']' for index,identity in enumerate(settings['cloud_folder_ids'])]
            cloud_scope_text = 'OneDrive: '+drive+'; folders: '+('; '.join(folders) or 'none verified')
            if settings['cloud_policy'] != 'review':
                actions.append(cloud_scope_text)
                actions.append('Cloud minimum age '+str(settings['cloud_min_age_days'])+' days; maximum '+str(settings['cloud_max_recycle'])+' files per run. '+('Recycling approved.' if settings['cloud_changes_approved'] else 'Approval still required.'))
        if settings.get('startup_review_enabled') and settings['startup_policy'] != 'review':
            for entry in settings['startup_approved_entries']:
                actions.append('Disable sign-in entry: '+str(entry.get('Name','Unnamed entry'))+'\n  Command: '+str(entry.get('Command',''))+'\n  Source: '+str(entry.get('Source',''))+'\n  Location: '+str(entry.get('Location','')))
        if settings.get('driver_updates_enabled') and settings['driver_policy'] != 'review':
            if settings['driver_policy'] == 'install_matching':
                for device in settings['driver_approved_devices']:
                    actions.append('Approved driver provider: '+str(device['provider'])+'; class: '+str(device['class'])+'\n  Hardware ID: '+str(device['hardwareId'])+'\n  Driver date must be '+str(device['notOlderThanDriverDate'])+' or later.')
            else:
                for update in settings['driver_approved_updates']:
                    actions.append('Approved driver update: '+str(update['title'])+'; revision '+str(update['revision']))
            actions.append('Driver installation: '+('approved' if settings['driver_changes_approved'] else 'approval still required')+'; update agreements: '+('accepted' if settings['driver_update_agreements_accepted'] else 'not accepted'))
        reviews = ['Initial and final Health Check/resource reports']
        for key, text in (('applications_review_enabled','Installed applications inventory'),('performance_review_enabled','Detailed process/resource review'),('startup_review_enabled','Startup/service inventory'),('duplicates_enabled','Local duplicate scan/report: ' + '; '.join(settings['duplicate_roots'])),('cloud_review_enabled','Connected personal OneDrive metadata scan/report (sign-in required)'),('driver_updates_enabled','Driver inventory/update scan/report')):
            if settings.get(key):
                reviews.append(text)
        if cloud_scope_text and settings['cloud_policy'] == 'review':
            reviews.append(cloud_scope_text)
        if settings.get('software_updates_enabled') and settings['software_update_policy'] == 'review':
            reviews.append('Software update scan/report; no agreements accepted or installation')
        lines = ['CADENCE',CADENCES[settings['automation_mode']], '', 'CLEANUP AGE', str(settings.get('cleanup_age_days',7)) + ' days (' + settings.get('intensity','Standard') + ' preset)', '', 'AUTHORIZED CHANGES / PERSISTENT ANALYSIS']
        lines.extend('- ' + text for text in actions or ['None; this configuration is scan/report only.'])
        lines.extend(['', 'SCAN / REPORT ONLY'])
        lines.extend('- ' + text for text in reviews)
        lines.extend(['', 'PRESERVATION AND REVOKING APPROVAL', 'Bing Wallpaper is protected. Review policies do not apply changes. Only saved approved scopes can be changed without per-run prompts; turn a tool off or return its policy to Review to revoke approval. Recycle bins, supported startup backups and Windows driver recovery have limits; cleanup is permanent.', '', 'Save does not run maintenance unless Run selected maintenance now is checked.'])
        self.summary.setPlainText('\n'.join(lines))

    def validateCurrentPage(self):
        try:
            for key in ('duplicates','cloud','startup','software','drivers'):
                if self.currentId() == self.page_ids[key]:
                    self.collect_choices(section=key)
                    break
        except ValueError as error:
            QMessageBox.warning(self, 'Review setup choices', str(error))
            return False
        return super().validateCurrentPage()

    def accept(self):
        if self.busy:
            self.save_error.setText('Wait for the inventory/account request to finish before saving.')
            return
        try:
            preferences = self.collect_choices()
            from .automation import save_setup
            saved = save_setup(self.backend.data_dir, preferences, self.backend.save_preferences,
                               run_now=self.run_now_checkbox.isChecked())
            if not isinstance(saved, dict) or int(saved.get('setup_version', 0)) < SETUP_VERSION:
                raise RuntimeError('The settings engine did not retain setup choices. No maintenance was started.')
        except Exception as error:
            self.save_error.setText('Choices were not saved: ' + str(error))
            self.refresh_summary()
            return
        self.saved_preferences = copy.deepcopy(saved)
        self.run_now = self.run_now_checkbox.isChecked()
        super().accept()




