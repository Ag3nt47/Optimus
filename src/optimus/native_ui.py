"""Native choices and tools with the shared Optimus presentation and workflow UI."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget, QWizard, QWizardPage)

from .automation import CURRENT_SETUP_VERSION, save_setup
from .native_settings import NativePreferences, full_native_settings, startup_descriptor
from .ui import BackendWorker, DataTable, DetailedSettingsWindow, OptimusWindow
from .widgets.theme import THEMES, build_qss
from .widgets.questions import make_yes_no_dialog


def note(layout, text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.PlainText)
    layout.addWidget(label)
    return label


def selection_buttons(layout, table):
    row = QHBoxLayout()
    status = QLabel()
    for label, state in (('Select all', Qt.Checked), ('Clear all', Qt.Unchecked)):
        button = QPushButton(label)
        def change(checked=False, value=state):
            selected = skipped = 0
            for index in range(table.count()):
                item = table.item(index)
                if item.flags() & Qt.ItemIsEnabled:
                    include = value == Qt.Checked and selected < 100
                    item.setCheckState(Qt.Checked if include else Qt.Unchecked)
                    selected += int(include)
                    skipped += int(value == Qt.Checked and not include)
            status.setText(f'{selected} selected; {skipped} remain unselected due to the 100-entry approval limit.'
                           if skipped else f'{selected} choices selected.')
        button.clicked.connect(change)
        row.addWidget(button)
    row.addStretch()
    layout.addLayout(row)
    layout.addWidget(status)


def check_item(table, label, data, checked=False, eligible=True):
    item = QListWidgetItem(label)
    item.setData(Qt.UserRole, data)
    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
    item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
    if not eligible:
        item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
    table.addItem(item)
    return item


def checked_data(table):
    return [table.item(i).data(Qt.UserRole) for i in range(table.count())
            if table.item(i).flags() & Qt.ItemIsEnabled and table.item(i).checkState() == Qt.Checked]


class NativeSetupWizard(QWizard):
    """First-start questions authorize exact native scopes, never inferred targets."""
    def __init__(self, backend, parent=None, *, preferences=None, first_run=False):
        super().__init__(parent)
        self.backend = backend
        self.initial = dict(preferences or backend.preferences())
        self.controls = {}
        self.saved_preferences = None
        self.run_now = False
        self.busy = False
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._workers = set()
        self._software_loaded = False
        self._startup_loaded = False
        self.setWindowTitle('Optimus — native setup questions')
        self.setWizardStyle(QWizard.ModernStyle)
        self.setMinimumSize(640, 480)
        self.setStyleSheet(build_qss('cyber'))
        self.setButtonText(QWizard.FinishButton, 'Save choices')
        self._build_general()
        self._build_cleanup()
        self._build_duplicates()
        self._build_startup()
        self._build_updates()
        self._build_summary(first_run)

    def page_body(self, title, description):
        page = QWizardPage()
        page.setTitle(title)
        page.setSubTitle(description)
        outer = QVBoxLayout(page)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self.addPage(page)
        return layout

    def check(self, body, key, text):
        from .widgets.wrapping_checkbox import WrappingCheckBox
        control = WrappingCheckBox(text)
        control.setChecked(bool(self.initial.get(key)))
        self.controls[key] = control
        body.addWidget(control)
        return control

    def _build_general(self):
        body = self.page_body('When should Optimus run?',
            'Choose once. Saved choices determine later one-click runs on this computer.')
        note(body, 'Native tools run as your desktop account. Package operations that need additional '
             'permission use the OS authorization dialog for that action. The whole GUI is not launched as root.')
        self.mode = QComboBox()
        self.mode.addItem('Full Optimization — supported native tools', 'full')
        self.mode.addItem('Custom — my individual choices', 'custom')
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.initial.get('optimization_mode'))))
        body.addWidget(self.mode)
        self.cadence = QComboBox()
        for value, label in (('manual', 'Only when I click Optimize'), ('startup', 'Each Optimus startup'),
                             ('daily', 'Daily while Optimus is open'), ('weekly', 'Weekly while Optimus is open'),
                             ('monthly', 'Monthly by the local calendar while Optimus is open')):
            self.cadence.addItem(label, value)
        self.cadence.setCurrentIndex(max(0, self.cadence.findData(self.initial.get('automation_mode'))))
        body.addWidget(self.cadence)
        self.check(body, 'launch_at_sign_in', 'Launch Optimus when I sign in to this desktop account')
        self.check(body, 'background_analysis_enabled', 'Keep background CPU, RAM, disk and network analysis running')
        self.check(body, 'speed_enhancements_enabled', 'Automatically apply supported reversible speed enhancements during Optimize')
        self.check(body, 'parallel_app_updates_enabled', 'Update approved apps in background while independent steps continue')
        self.check(body, 'auto_continue_fixes_enabled', 'Automatically continue with supported post-scan fixes and approved updates')
        note(body, 'Automatic continuation uses your saved package scopes and agreements. Native OS/security choices, process actions and restarts remain manual. Background app updates keep one installer lane while independent steps continue.')
        note(body, 'The speed package changes only a supported power policy after fresh mains-power, permission and policy checks. Battery, custom and unsupported configurations are deferred. Settings provides current review and guarded restore; no speed gain is assumed.')
        self.check(body, 'reduce_motion', 'Reduce animation and starfield motion')
        note(body, 'Raising process priority may need OS permission. It does not create additional CPU or RAM. '
             'Daily, weekly and monthly maintenance stop when Optimus exits. Monthly uses the same local calendar time next month, clamped to its final day. Sign-in registration takes effect at the next login.')
        self.interval = QSpinBox()
        self.interval.setRange(2, 60)
        self.interval.setSuffix(' seconds between background samples')
        self.interval.setValue(self.initial.get('analysis_interval', 5))
        body.addWidget(self.interval)
        self.priority = QComboBox()
        self.priority.addItems(['Normal', 'AboveNormal', 'High'])
        self.priority.setCurrentText(self.initial.get('codex_priority', 'High'))
        body.addWidget(self.priority)
        self.mode.currentIndexChanged.connect(self._preset)
        from .widgets.unattended_setup_choices import UnattendedSetupChoices
        self.unattended_setup = UnattendedSetupChoices(body, self.initial,
            add_check=lambda key, text: self.check(body, key, text),
            add_note=lambda text: note(body, text))
        def unattended_controls(enabled):
            if enabled:
                self.controls['launch_at_sign_in'].setChecked(True)
                self.cadence.setCurrentIndex(self.cadence.findData('startup'))
            self.controls['launch_at_sign_in'].setEnabled(not enabled)
            self.cadence.setEnabled(not enabled)
        self.unattended_setup.enabled.toggled.connect(unattended_controls)
        unattended_controls(self.unattended_setup.enabled.isChecked())
        body.addStretch()

    def _build_cleanup(self):
        body = self.page_body('Which temporary and browser data?',
            'Browser cleanup asks Close and clean / Skip separately for each open browser.')
        self.check(body, 'cleanup_enabled', 'Clean selected rebuildable temporary/cache files')
        self.intensity = QComboBox()
        self.intensity.addItems(['Low', 'Standard', 'High'])
        self.intensity.setCurrentText(self.initial.get('intensity', 'Standard'))
        body.addWidget(self.intensity)
        self.age = QSpinBox()
        self.age.setRange(0, 3650)
        self.age.setSuffix(' days minimum age (0 includes all eligible ages)')
        self.age.setValue(self.initial.get('cleanup_age_days', 7))
        self.intensity.currentTextChanged.connect(lambda value: self.age.setValue({'Low': 30, 'Standard': 7, 'High': 1}[value]))
        body.addWidget(self.age)
        self.categories = QListWidget()
        saved = set(self.initial.get('cleanup_category_ids', []))
        for category in backend_categories(self.backend):
            key = category.get('id')
            if not isinstance(key, str) or ':' in key:
                continue  # Browser cache scopes use their own close-or-skip stage.
            eligible = bool(category.get('available', True))
            first_full = self.initial.get('setup_version', 0) == 0 and self.initial.get('optimization_mode') == 'full'
            item = check_item(self.categories, str(category.get('name') or category.get('label') or key), key,
                              checked=key in saved or first_full and eligible, eligible=eligible)
            item.setToolTip(str(category.get('description', '')) + '\n' + str(category.get('path', '')))
        body.addWidget(self.categories)
        selection_buttons(body, self.categories)
        for key, label in (('browser_cache_enabled', 'Browser caches'), ('browser_history_enabled', 'Browsing history'),
                           ('browser_cookies_enabled', 'Cookies and site sign-ins (can sign me out)'),
                           ('browser_download_history_enabled', 'Download-list records (downloaded files stay)')):
            self.check(body, key, label)
        self.browsers = QListWidget()
        self.browsers.setMaximumHeight(140)
        for key, label in (('chrome', 'Google Chrome'), ('chromium', 'Chromium'), ('firefox', 'Firefox'), ('brave', 'Brave'),
                           ('vivaldi', 'Vivaldi'), ('edge', 'Microsoft Edge'), ('opera', 'Opera (optional)')):
            check_item(self.browsers, label, key, key in self.initial['browser_cleanup_browsers'])
        body.addWidget(self.browsers)
        selection_buttons(body, self.browsers)
        self.domains = QLineEdit(', '.join(self.initial.get('browser_keep_domains', [])))
        self.domains.setPlaceholderText('Keep cookies for: example.com, another.example')
        body.addWidget(self.domains)
        note(body, 'Passwords and bookmarks are preserved. Browser database backups remain private on this machine. '
             'Safari databases and macOS protected app containers are excluded. Broad /tmp and system folder purges are not used. '
             'Native user temporary files retain a seven-day minimum even in Full mode; recent or observed open files '
             'and coordination files stay protected. Missing process access can defer that temporary-file scope.')
        from .widgets.smart_setup_choices import SmartSetupChoices
        self.smart_setup = SmartSetupChoices(body, self.initial,
            add_check=lambda key, text: self.check(body, key, text),
            add_note=lambda text: note(body, text))

    def _build_duplicates(self):
        body = self.page_body('Duplicate files', 'Choose concrete personal folders and a retention rule.')
        self.check(body, 'duplicates_enabled', 'Scan my selected duplicate folders during optimization')
        self.roots = QPlainTextEdit('\n'.join(self.initial.get('duplicate_roots', [])))
        self.roots.setPlaceholderText('One absolute personal folder per line; filesystem and system roots are refused.')
        body.addWidget(self.roots)
        browse = QPushButton('Add personal folder')
        browse.clicked.connect(self._add_root)
        body.addWidget(browse)
        self.duplicate_policy = QComboBox()
        for key, text in (('review', 'Review only'), ('keep_oldest', 'Keep oldest matching copy; Trash eligible newer copies'),
                          ('keep_newest', 'Keep newest matching copy; Trash eligible older copies')):
            self.duplicate_policy.addItem(text, key)
        self.duplicate_policy.setCurrentIndex(max(0, self.duplicate_policy.findData(self.initial.get('duplicate_policy'))))
        body.addWidget(self.duplicate_policy)
        self.check(body, 'duplicate_changes_approved', 'Approve this folder/rule scope for later automatic duplicate recycling')
        self.duplicate_age = QSpinBox()
        self.duplicate_age.setRange(1, 3650)
        self.duplicate_age.setValue(self.initial.get('duplicate_min_age_days', 7))
        self.duplicate_age.setSuffix(' days minimum duplicate age')
        body.addWidget(self.duplicate_age)
        self.duplicate_limit = QSpinBox()
        self.duplicate_limit.setRange(1, 500)
        self.duplicate_limit.setValue(self.initial.get('duplicate_max_recycle', 25))
        self.duplicate_limit.setSuffix(' maximum files to Trash per run')
        body.addWidget(self.duplicate_limit)
        note(body, 'Matching content is hashed and a retained copy is rechecked immediately before recycling. '
             'The OS Trash is not emptied. Changed files, symlinks and unsupported locations remain untouched.')
        self.roots.textChanged.connect(lambda: self.controls['duplicate_changes_approved'].setChecked(False))
        self.duplicate_policy.currentIndexChanged.connect(lambda: self.controls['duplicate_changes_approved'].setChecked(False))

    def _add_root(self):
        root = QFileDialog.getExistingDirectory(self, 'Choose a personal duplicate folder', str(Path.home()))
        if root:
            self.roots.appendPlainText(root)

    def _build_startup(self):
        body = self.page_body('Background and sign-in programs',
            'Only exact user-owned native registrations can be approved for disabling.')
        self.check(body, 'startup_review_enabled', 'Inspect native startup entries on each run')
        self.startup = QListWidget()
        body.addWidget(self.startup)
        selection_buttons(body, self.startup)
        load = QPushButton('Load current user startup entries')
        load.clicked.connect(lambda: self._probe('startup_list', self._startup_ready))
        body.addWidget(load)
        self.startup_policy = QComboBox()
        self.startup_policy.addItem('Review only', 'review')
        self.startup_policy.addItem('Disable only my exact approved entries', 'disable_selected')
        self.startup_policy.setCurrentIndex(max(0, self.startup_policy.findData(self.initial.get('startup_policy'))))
        body.addWidget(self.startup_policy)
        self.check(body, 'startup_changes_approved', 'Approve disabling these exact user startup files on later runs')
        self.startup.itemChanged.connect(lambda _: self.controls['startup_changes_approved'].setChecked(False))
        self.check(body, 'performance_review_enabled', 'Analyze measured CPU, RAM and I/O contributors')
        self.check(body, 'post_process_review_enabled', 'Offer reviewed process/startup actions after the final verification')
        self.check(body, 'applications_review_enabled', 'Inventory installed programs during optimization')
        note(body, 'System startup services and protected desktop processes stay protected. '
             'A process being old or using RAM does not establish that it is unnecessary. Nothing is paused automatically.')

    def _startup_ready(self, result):
        self.startup.clear()
        saved = {item['Path'] for item in self.initial.get('startup_approved_entries', [])}
        for row in result if isinstance(result, list) else []:
            eligible = row.get('CanManage') is True and not row.get('Protected', True) and row.get('Enabled') is True
            check_item(self.startup, str(row.get('Name') or row.get('Path')), row,
                       checked=row.get('Path') in saved, eligible=eligible)
        self._startup_loaded = True

    def _build_updates(self):
        body = self.page_body('Native software and OS package updates',
            'Only updates currently offered by a recognized installed package manager are eligible.')
        self.check(body, 'software_updates_enabled', 'Check supported native package updates')
        self.software_policy = QComboBox()
        for key, text in (('review', 'Review only'), ('selected', 'Update only my selected exact package IDs'),
                          ('all_supported', 'All supported installed packages, excluding my exceptions')):
            self.software_policy.addItem(text, key)
        self.software_policy.setCurrentIndex(max(0, self.software_policy.findData(self.initial.get('software_update_policy'))))
        body.addWidget(self.software_policy)
        self.software = QListWidget()
        body.addWidget(self.software)
        selection_buttons(body, self.software)
        load = QPushButton('Load currently available newer packages')
        load.clicked.connect(lambda: self._probe('updates_scan', self._software_ready))
        body.addWidget(load)
        self.exclusions = QLineEdit(', '.join(self.initial.get('software_update_exclude_ids', [])))
        self.exclusions.setPlaceholderText('Exclude exact manager-qualified package IDs, separated by commas')
        body.addWidget(self.exclusions)
        self.check(body, 'software_update_all_approved', 'Approve all-supported native package scope, including future matching packages')
        self.check(body, 'software_update_agreements_accepted', 'Accept selected package-manager agreements and approve installing available newer versions')
        note(body, 'Linux OS repositories may include kernel, firmware and driver packages. macOS driver/security '
             'updates remain part of Apple Software Update; app updates use installed Homebrew where supported. '
             'No OS major upgrade, arbitrary unofficial driver, forced restart or invented update offer is used. '
             'OS authorization may still require your password/approval. Missing package managers are reported.')
        self.software_policy.currentIndexChanged.connect(self._revoke_software)
        self.software.itemChanged.connect(lambda _: self._revoke_software())
        self.exclusions.textChanged.connect(lambda _: self._revoke_software())

    def _revoke_software(self, *_):
        self.controls['software_update_agreements_accepted'].setChecked(False)
        self.controls['software_update_all_approved'].setChecked(False)

    def _software_ready(self, result):
        self.software.clear()
        saved = set(self.initial.get('software_update_ids', []))
        offers = result.get('updates', []) if isinstance(result, dict) else []
        for row in offers:
            key = row.get('id') or row.get('Id')
            if not key:
                continue
            label = str(row.get('name') or row.get('Name') or key) + ' | ' + str(row.get('installedVersion') or row.get('version') or '') + ' → ' + str(row.get('availableVersion') or row.get('available') or '')
            check_item(self.software, label, key, key in saved, row.get('eligible', True) is not False)
        self._software_loaded = True
        self.probe_status.setText(str(result.get('note') or result.get('output') or 'Native update scan completed. Review available packages.')[:2000])

    def _build_summary(self, first_run):
        body = self.page_body('Review and save', 'Approvals persist until you change them in Settings.')
        self.check(body, 'security_review_enabled', 'Inspect native security/update status and show recommendations')
        self.speed_summary = note(body, '')
        def speed_summary(enabled):
            self.speed_summary.setText('Automatic reversible speed enhancements: ' + ('enabled. Supported power-policy changes run during Optimize after fresh checks; review and restore are available in Settings.'
                if enabled else 'disabled. Optimize keeps the existing power policy.'))
        self.controls['speed_enhancements_enabled'].toggled.connect(speed_summary)
        speed_summary(self.controls['speed_enhancements_enabled'].isChecked())
        self.continuation_summary = note(body, '')
        def continuation_summary(*_):
            self.continuation_summary.setText('Automatic post-scan fixes: '
                + ('enabled within saved package scopes and agreements' if self.controls['auto_continue_fixes_enabled'].isChecked() else 'disabled; results wait for your click')
                + '. Background app updates: ' + ('enabled for independent steps' if self.controls['parallel_app_updates_enabled'].isChecked() else 'disabled')
                + '. Native OS/security, process actions and restarts remain manual.')
        for key in ('auto_continue_fixes_enabled', 'parallel_app_updates_enabled'):
            self.controls[key].toggled.connect(continuation_summary)
        continuation_summary()
        self.smart_summary = note(body, '')
        def smart_summary(*_):
            choices = self.smart_setup.collect()
            self.smart_summary.setText('Smart Cleaning: ' + ('enabled at ' + str(choices['smart_cleaning_threshold_bytes']) + ' bytes'
                if choices['smart_cleaning_enabled'] else 'disabled') + '. Browser-exit cleanup: '
                + ('enabled for saved browser categories; history/cookies can be removed and sign you out, with kept domains and rollback backups'
                   if choices['browser_exit_cleaning_enabled'] else 'disabled')
                + '. Monitoring requires Optimus to be open and the current session approved.')
        for control in self.smart_setup.checks.values():
            control.toggled.connect(smart_summary)
        for control in self.smart_setup.numbers.values():
            control.valueChanged.connect(smart_summary)
        smart_summary()
        self.unattended_summary = note(body, '')
        def unattended_summary(*_):
            self.unattended_summary.setText('Unattended Auto Clean: ' + (
                'saving approves the exact current scope, enables sign-in launch and Startup cadence, and skips future launch/post-run prompts while unchanged. '
                'Temporary/browser records and selected apps can be permanently removed. Open browsers: '
                + ('skip' if self.unattended_setup.browser_policy.currentData() == 'skip_open' else 'request graceful closure; skip if still open or unknown')
                + '. Unknown installer outcomes, permission prompts and restarts are deferred.'
                if self.unattended_setup.enabled.isChecked() else 'disabled; each launch requires review before automatic actions.'))
        self.unattended_setup.enabled.toggled.connect(unattended_summary)
        self.unattended_setup.browser_policy.currentIndexChanged.connect(unattended_summary)
        unattended_summary()
        note(body, 'Native capability information explains what was measured, what is supported and what requires '
             'manual OS controls. Registry cleanup, Windows consumer-app removals, Defender scans and DPAPI-backed '
             'cloud sign-in are Windows-specific. No substitute is presented as an equivalent security fix.')
        self.run_after = QCheckBox('Run my selected one-click optimization after saving these choices')
        self.run_after.setChecked(bool(first_run))
        body.addWidget(self.run_after)
        self.probe_status = note(body, 'Saving a scope grants only its described actions. Browser close-or-skip prompts remain interactive.')
        body.addStretch()

    def _preset(self, *_):
        if self.mode.currentData() != 'full' or not hasattr(self, 'age'):
            return
        value = full_native_settings(self.initial)
        for key, control in self.controls.items():
            if key in value and key not in ('launch_at_sign_in', 'software_update_all_approved',
                    'software_update_agreements_accepted', 'startup_changes_approved', 'duplicate_changes_approved',
                    'smart_cleaning_enabled', 'browser_exit_cleaning_enabled', 'unattended_auto_clean_enabled'):
                control.setChecked(bool(value[key]))
        self.intensity.setCurrentText('High')
        self.age.setValue(0)
        self.cadence.setCurrentIndex(self.cadence.findData('startup'))
        for index in range(self.categories.count()):
            item = self.categories.item(index)
            if item.flags() & Qt.ItemIsEnabled:
                item.setCheckState(Qt.Checked)

    def _probe(self, method, callback):
        if self.busy:
            return
        self.busy = True
        self.button(QWizard.FinishButton).setEnabled(False)
        worker = BackendWorker(getattr(self.backend, method), (), {})
        self._workers.add(worker)
        worker.signals.result.connect(callback)
        worker.signals.error.connect(lambda error: QMessageBox.warning(self, 'Native inventory', error))
        def finished():
            self.busy = False
            self._workers.discard(worker)
            self.button(QWizard.FinishButton).setEnabled(True)
        worker.signals.finished.connect(finished)
        self.pool.start(worker)

    def collect(self):
        value = dict(self.initial)
        value.update({key: control.isChecked() for key, control in self.controls.items()})
        value.update(self.smart_setup.collect())
        value.update(self.unattended_setup.collect())
        value.update(optimization_mode=self.mode.currentData(), automation_mode=self.cadence.currentData(),
                     setup_version=CURRENT_SETUP_VERSION, cleanup_age_days=self.age.value(),
                     intensity=self.intensity.currentText(),
                     cleanup_category_ids=checked_data(self.categories), analysis_interval=self.interval.value(),
                     codex_priority=self.priority.currentText(), browser_cleanup_browsers=checked_data(self.browsers),
                     browser_keep_domains=[item.strip() for item in self.domains.text().split(',') if item.strip()],
                     duplicate_roots=[item.strip() for item in self.roots.toPlainText().splitlines() if item.strip()],
                     duplicate_policy=self.duplicate_policy.currentData(), duplicate_min_age_days=self.duplicate_age.value(),
                     duplicate_max_recycle=self.duplicate_limit.value(), startup_policy=self.startup_policy.currentData(),
                     software_update_policy=self.software_policy.currentData(),
                     software_update_exclude_ids=[item.strip() for item in self.exclusions.text().split(',') if item.strip()])
        if self._startup_loaded:
            value['startup_approved_entries'] = [startup_descriptor(row) for row in checked_data(self.startup)]
        if self._software_loaded:
            value['software_update_ids'] = checked_data(self.software)
        return NativePreferences.validate(value)

    def accept(self):
        if self.busy:
            return
        try:
            self.run_now = self.run_after.isChecked()
            value = self.collect()
            if value.get('unattended_auto_clean_enabled') is True:
                from .unattended_configuration import approve_unattended_settings
                value = approve_unattended_settings(value)
            self.saved_preferences = save_setup(self.backend.data_dir, value, self.backend.save_preferences,
                                               run_now=self.run_now, validator=NativePreferences.validate)
        except (OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, 'Choices could not be saved', str(error))
            return
        super().accept()

    def reject(self):
        if self.busy:
            return
        super().reject()


def backend_categories(backend):
    try:
        return backend.cleanup_categories()
    except (OSError, ValueError, RuntimeError):
        return []


class NativeSettingsWindow(DetailedSettingsWindow):
    """Native operations use the existing worker/confirmation/table contracts."""
    def __init__(self, backend, owner, *, smoke_test=False):
        QMainWindow.__init__(self)
        self.backend, self.owner, self.smoke_test = backend, owner, smoke_test
        self.busy = False
        self._workers = set()
        self._callback = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.loaded_pages = set()
        self.setWindowTitle('Optimus — native settings and tools')
        self.resize(1160, 800)
        self.setStyleSheet(build_qss('cyber'))
        central = QWidget()
        body = QVBoxLayout(central)
        self.tabs = QTabWidget()
        self.pages = {}
        self.layouts = {}
        body.addWidget(self.tabs, 1)
        self.activity = QProgressBar()
        self.activity.setRange(0, 0)
        self.activity.hide()
        body.addWidget(self.activity)
        self.status = note(body, 'Native tools use this desktop account. Choose an action to inspect its scope.')
        self.setCentralWidget(central)
        self._build_native_maintenance()
        self._build_native_processes()
        self._build_native_browser()
        self._build_native_files()
        self._build_native_startup()
        self._build_native_updates()
        self._build_native_history()
        self._build_native_capabilities()
        for name in ('Smart cleaning', 'Workers', 'File utilities', 'Recovery'):
            self.page(name)
        self._build_extended_tools()

    def page(self, name, *, scroll=True):
        content = QWidget()
        layout = QVBoxLayout(content)
        self.pages[name] = self.tabs.count()
        self.layouts[name] = layout
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(content)
            self.tabs.addTab(area, name)
        else:
            self.tabs.addTab(content, name)
        return layout

    def action(self, body, text, method, *args, callback=None):
        button = QPushButton(text)
        button.clicked.connect(lambda: self._run(method, *args, done=callback or self._show_result))
        body.addWidget(button)
        return button

    def _build_native_maintenance(self):
        body = self.page('Maintenance', scroll=True)
        note(body, 'Change the one-click configuration, saved approvals, sign-in launch and background analysis '
             'through the setup questions. Select all / Clear all are available for each multi-selection.')
        configure = QPushButton('Configure one-click optimization and startup')
        configure.clicked.connect(self.owner.open_initial_setup)
        body.addWidget(configure)
        from .widgets.speed_enhancements_panel import SpeedEnhancementsPanel
        self.speed_enhancements_panel = SpeedEnhancementsPanel(self, self.owner._settings())
        body.addWidget(self.speed_enhancements_panel)
        self.action(body, 'Inspect current system health', 'audit')
        self.action(body, 'Check Optimus sign-in launch', 'startup_launch_status')
        self.action(body, 'Show saved configuration', 'preferences')
        self.action(body, 'Inspect native security and OS update recommendations', 'security_status')
        self.action(body, 'Measure slowdown contributors', 'slowdown_report')
        note(body, 'Appearance and window effects')
        self.interface_priority_choice = QCheckBox('Prioritize Optimus interface (Windows Above Normal CPU only)')
        self.interface_priority_choice.setChecked(False)
        self.interface_priority_choice.setEnabled(False)
        self.interface_priority_choice.setToolTip('Windows process priority classes are unavailable on this native platform.')
        body.addWidget(self.interface_priority_choice)
        self.theme_choice = QComboBox()
        self.theme_choice.addItems(list(THEMES))
        self.theme_choice.setCurrentText(self.owner.appearance.get('theme', 'cyber'))
        body.addWidget(self.theme_choice)
        self.motion_choice = QCheckBox('Enable starfield and window animations')
        self.motion_choice.setChecked(self.owner.appearance.get('motion', True))
        body.addWidget(self.motion_choice)
        note(body, 'Minimize uses the taskbar or Dock. Closing the main window exits after active work finishes; unsaved Sandbox edits prompt before exit.')
        appearance = QPushButton('Save appearance')
        appearance.clicked.connect(self._save_appearance)
        body.addWidget(appearance)
        body.addStretch()

    def _save_appearance(self):
        if self.smoke_test or self.busy:
            self.status.setText('Appearance changes are disabled during a smoke test or active operation.')
            return
        from .workflow import atomic_json
        value = {'theme': self.theme_choice.currentText(), 'motion': self.motion_choice.isChecked()}
        try:
            atomic_json(self.owner.preferences_path, value)
            self.owner.apply_appearance(value)
            self.setStyleSheet(build_qss(value['theme']))
            self.status.setText('Appearance saved. The reduced-motion setup choice still takes precedence.')
        except (OSError, ValueError, RuntimeError) as error:
            self._error(str(error))

    def _build_native_processes(self):
        body = self.page('Performance')
        from .widgets.process_harness_panel import ProcessHarnessPanel
        self.process_harness_panel = ProcessHarnessPanel(self)
        body.addWidget(self.process_harness_panel)
        note(body, 'Lowering native process priority usually needs no administrator permission. Raising it again, '
             'including restoring Normal after a lower priority, can require OS authorization or restarting the app.')
        bar = QHBoxLayout()
        self.process_priority = QComboBox()
        self.process_priority.addItems(['Normal', 'AboveNormal', 'High', 'BelowNormal', 'Idle'])
        bar.addWidget(self.process_priority)
        priority = QPushButton('Set selected process priority')
        priority.clicked.connect(self._priority)
        bar.addWidget(priority)
        self.affinity = QLineEdit()
        self.affinity.setPlaceholderText('Linux CPU cores: 0,1')
        bar.addWidget(self.affinity)
        affinity = QPushButton('Set CPU affinity')
        affinity.setEnabled(sys.platform == 'linux')
        affinity.clicked.connect(self._affinity)
        bar.addWidget(affinity)
        body.addLayout(bar)

    def _priority(self):
        record = self.process_harness_panel.table.selected()
        if record and self._confirm('Change process priority', 'Apply this priority to the exact selected application process?'):
            self._run('set_priority', record, self.process_priority.currentText(), done=self._show_result)

    def _affinity(self):
        try:
            cores = [int(item.strip()) for item in self.affinity.text().split(',') if item.strip()]
            if not cores:
                raise ValueError('Choose at least one CPU core.')
        except ValueError as error:
            self._error(str(error))
            return
        record = self.process_harness_panel.table.selected()
        if record and self._confirm('Change CPU affinity', 'Apply the listed cores to the exact selected application process?'):
            self._run('set_affinity', record, cores, done=self._show_result)

    def resume_processes(self):
        if self._confirm('Resume paused applications', 'Resume the unchanged application processes Optimus previously paused?'):
            self._run('wake_all', done=self._show_result)

    def _build_native_browser(self):
        body = self.page('Browser privacy')
        from .widgets.privacy_panel import PrivacyPanel
        self.privacy_panel = PrivacyPanel(self)
        body.addWidget(self.privacy_panel)

    def _build_native_files(self):
        body = self.page('Files')
        note(body, 'Inspect selected ordinary personal folders. Duplicate recycling keeps a revalidated identical copy '
             'and sends only the selected file to the OS Trash. System roots and indirect paths are excluded.')
        self.file_root = QLineEdit(str(Path.home() / 'Downloads'))
        body.addWidget(self.file_root)
        browse = QPushButton('Choose folder')
        def choose():
            root = QFileDialog.getExistingDirectory(self, 'Choose personal folder', self.file_root.text())
            if root:
                self.file_root.setText(root)
        browse.clicked.connect(choose)
        body.addWidget(browse)
        scan = QPushButton('Analyze folder storage')
        scan.clicked.connect(lambda: self._run('storage_scan', self.file_root.text(), done=self._show_result))
        body.addWidget(scan)
        duplicates = QPushButton('Find exact duplicate contents')
        duplicates.clicked.connect(lambda: self._run('duplicate_scan', self.file_root.text(), done=self._duplicates_ready))
        body.addWidget(duplicates)
        self.duplicates_table = DataTable([('File', 'FilePath|path'), ('Bytes', 'Length|size'), ('SHA256', 'SHA256|hash')])
        body.addWidget(self.duplicates_table)
        recycle = QPushButton('Trash selected duplicate after review')
        recycle.clicked.connect(self._recycle_duplicate)
        body.addWidget(recycle)

    def _duplicates_ready(self, result):
        self.duplicates_table.populate(result.get('Candidates', result.get('candidates', result.get('duplicates', []))))
        self._show_result(result)
        self.select_page('Files')

    def _recycle_duplicate(self):
        record = self.duplicates_table.selected()
        if record and self._confirm('Recycle duplicate', 'Send this exact duplicate to Trash after verifying a retained matching copy?', details=record, phrase='TRASH DUPLICATE'):
            self._run('duplicate_recycle', record, done=self._show_result)

    def _build_native_startup(self):
        body = self.page('Startup and apps')
        note(body, 'User-owned XDG autostart files or LaunchAgents can be disabled with recovery records. '
             'System services are inspected separately and remain protected.')
        self.startup_table = DataTable([('Name', 'Name'), ('Enabled', 'Enabled'), ('Path', 'Path'), ('Protected', 'Protected')])
        self.action(body, 'Refresh native startup entries', 'startup_list', callback=self.startup_table.populate)
        body.addWidget(self.startup_table)
        disable = QPushButton('Disable selected user startup entry')
        disable.clicked.connect(self._disable_startup)
        body.addWidget(disable)
        self.apps_table = DataTable([('Program', 'Name|name'), ('Version', 'Version|version'), ('Source', 'Source|source'), ('Identity', 'Identity|id')])
        self.action(body, 'Inventory installed native applications', 'installed_apps', callback=self.apps_table.populate)
        body.addWidget(self.apps_table)
        uninstall = QPushButton('Review selected application uninstall route')
        uninstall.clicked.connect(self._uninstall)
        body.addWidget(uninstall)
        self.action(body, 'Inspect native services', 'services')

    def _disable_startup(self):
        record = self.startup_table.selected()
        if record and self._confirm('Disable startup entry', 'Disable only this unchanged user-owned sign-in file? Review History to restore.', details=record):
            self._run('startup_set', record, False, done=self._show_result)

    def _uninstall(self):
        record = self.apps_table.selected()
        if record and self._confirm('Review uninstall', 'Open or describe the supported uninstall route for this exact installed application?', details=record):
            self._run('uninstall', record, done=self._show_result)

    def _build_native_updates(self):
        body = self.page('Updates')
        note(body, 'Use setup questions to approve exact or all-supported package updates for one-click runs. '
             'Native package managers provide supported application, kernel and driver packages. Manufacturer-only '
             'and Apple updates are reported separately. No missing offer is described as fully up to date.')
        self.action(body, 'Scan offered newer package versions', 'updates_scan')
        self.action(body, 'Inspect native kernel / driver / OS update coverage', 'driver_devices')
        self.action(body, 'Inspect OS security recommendations', 'security_status')
        if sys.platform == 'darwin':
            note(body, 'Apple OS updates need a separate exact selection, license acceptance and native authorization. '
                 'They are not part of Homebrew approvals. Review Apple restart requirements before installing.')
            self.apple_updates = DataTable([('Apple update', 'name'), ('Offered version', 'available'), ('Restart required', 'restart_required')])
            def apple_ready(result):
                self.apple_updates.populate(result.get('updates', []))
                self._show_result(result)
                self.select_page('Updates')
            self.action(body, 'Check Apple Software Update offers', 'os_updates_scan', callback=apple_ready)
            body.addWidget(self.apple_updates)
            apple_install = QPushButton('Review and install selected Apple update')
            apple_install.clicked.connect(self._install_apple_update)
            body.addWidget(apple_install)
        body.addStretch()

    def _install_apple_update(self):
        record = self.apple_updates.selected()
        if record and self._confirm('Install one offered Apple update',
                'Install this exact currently offered Apple update and accept its license? '
                'The OS may request administrator/volume-owner authorization. Save your work. '
                'Optimus does not request an automatic restart.', details=record, phrase='INSTALL APPLE UPDATE'):
            self._run('install_os_update', record['id'], confirmed=True, agreements_accepted=True, done=self._show_result)

    def _build_native_history(self):
        body = self.page('Restore/history')
        self.action(body, 'Refresh supported changes', 'changes', callback=self._changes_ready)
        self.history_table = DataTable([('Action', 'Action|action'), ('Time', 'CreatedAt|created_at'), ('Identity', 'Id|id'), ('Status', 'Status|status')])
        body.addWidget(self.history_table)
        restore = QPushButton('Restore selected supported change')
        restore.clicked.connect(self._restore)
        body.addWidget(restore)
        self.result_text = QPlainTextEdit()
        self.result_text.setReadOnly(True)
        body.addWidget(self.result_text, 1)

    def _changes_ready(self, rows):
        self.history_table.populate(rows)

    def _restore(self):
        record = self.history_table.selected()
        if record and self._confirm('Restore selected change', 'Restore only this saved change if its current state still matches?', details=record):
            self._run('restore', record, done=self._show_result)

    def _build_native_capabilities(self):
        body = self.page('Capabilities')
        note(body, 'Native coverage depends on this OS, permissions and installed package managers. '
             'Windows Registry, Defender, Windows consumer debloat and DPAPI-backed cloud account features are '
             'not applicable here. macOS affinity and unsupported system-service changes are unavailable.')
        self.action(body, 'Inspect supported native capabilities and limits', 'capabilities')
        body.addStretch()

    def select_page(self, name, *, load=True):
        aliases = {'Dashboard': 'Maintenance', 'Settings': 'Maintenance', 'Applications': 'Startup and apps',
                   'Startup': 'Startup and apps', 'Disk analyzer': 'Files', 'Duplicate finder': 'Files', 'Drivers': 'Updates'}
        self.tabs.setCurrentIndex(self.pages.get(aliases.get(name, name), 0))

    def _run(self, method, *args, done=None, **kwargs):
        if self.smoke_test and method in ('set_priority', 'set_affinity', 'wake_all', 'startup_set', 'restore',
                                         'privacy_apply', 'privacy_restore', 'duplicate_recycle', 'uninstall', 'process_harness_apply', 'install_os_update'):
            self._error('Changes are disabled during the packaged interface smoke test.')
            return
        super()._run(method, *args, done=done, **kwargs)

    def closeEvent(self, event):
        if self.busy:
            self.status.setText('Wait for the active operation to finish before closing Settings.')
            event.ignore()
        else:
            event.accept()

    def shutdown(self):
        if getattr(self, '_native_shutdown', False):
            return
        self._native_shutdown = True
        for name in ('smart_maintenance_panel', 'worker_board', 'file_utilities_panel', 'recovery_panel'):
            panel = getattr(self, name, None)
            if panel is not None:
                panel.shutdown()
        self.process_harness_panel.shutdown()
        self.privacy_panel.shutdown()
        self.speed_enhancements_panel.shutdown()
        self.pool.waitForDone()


class NativeOptimusWindow(OptimusWindow):
    def open_initial_setup(self, checked=False, *, first_run=False):
        if self.busy or self._setup_wizard is not None or self._settings_window is not None and self._settings_window.busy:
            return False
        wizard = NativeSetupWizard(self.backend, self, preferences=self._settings(), first_run=first_run)
        self._setup_wizard = wizard
        try:
            accepted = wizard.exec() == QDialog.Accepted
            if accepted:
                self.workflow_settings = wizard.saved_preferences
                if self._launch_consent.required:
                    self.invalidate_session_review('Setup choices saved. Review their actions before Auto Clean.')
                if self._settings_window is not None:
                    self._settings_window.speed_enhancements_panel.sync_settings(self.workflow_settings)
                if self.session_approved(self.workflow_settings):
                    method = self.backend.analysis_start if self.workflow_settings['background_analysis_enabled'] else self.backend.analysis_stop
                    method(self.workflow_settings['analysis_interval']) if self.workflow_settings['background_analysis_enabled'] else method()
                self.config.ui_motion_enabled = not self.workflow_settings['reduce_motion'] and bool(self.appearance.get('motion', True))
                self.canvas.motion = self.config.ui_motion_enabled
                self.canvas.sync_motion()
                self.status.setText('Native choices saved. Future runs use these exact scopes and approvals.')
            run_now = accepted and wizard.run_now
        finally:
            self._setup_wizard = None
            wizard.deleteLater()
        if run_now:
            self.start_optimization()
        return accepted

    def open_settings(self, checked=False, page=None):
        if self.busy or self._setup_wizard is not None:
            self.status.setText('Finish the current operation or setup questions first.')
            return
        if self._launch_consent.required:
            self.invalidate_session_review('Settings opened. Automatic maintenance is paused until you review Auto Clean again.')
        if self._settings_window is None:
            self._settings_window = NativeSettingsWindow(self.backend, self, smoke_test=self.smoke_test)
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()
        if page:
            self._settings_window.select_page(page)

    def _offer_post_optimization_reviews(self, result, settings):
        from .unattended_configuration import matches_unattended
        if matches_unattended(settings):
            self._post_review_active = False
            return
        try:
            if self._progress_window is not None:
                self._progress_window.hide()
            security = result.get('security_review')
            if isinstance(security, dict) and security.get('recommendations'):
                dialog = make_yes_no_dialog(self, 'Native security recommendations',
                    'Review native security and update findings. Open the native security/update tools now?',
                    yes_label='Yes — review', no_label='No — skip')
                dialog.setDetailedText(json.dumps(security, indent=2, default=str))
                try:
                    if dialog.exec() == QMessageBox.Yes:
                        self.open_settings(page='Updates')
                        self._settings_window._show_result(security)
                finally:
                    dialog.deleteLater()
            processes = result.get('process_review')
            if settings.get('post_process_review_enabled') and isinstance(processes, dict) and self._ask_process_review(processes):
                self.open_settings(page='Performance')
                self._settings_window.process_harness_panel._scan_ready(processes)
        finally:
            self._post_review_active = False
            if self._progress_window is not None and not self._quitting:
                self._progress_window.show()
