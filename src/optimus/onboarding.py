"""First-run questions. Preferences are written only after Save choices."""
from __future__ import annotations
import copy
import ntpath
import os
import re
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QButtonGroup, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget, QWizard, QWizardPage,
)

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


class InitialSetupWizard(QWizard):
    """Collect explicit choices without running a scan, install or cleanup."""
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
            QCheckBox::indicator,QRadioButton::indicator {width:18px;height:18px;border:1px solid #94a3b8;background:#0b111a;}
            QCheckBox::indicator {border-radius:3px;}
            QRadioButton::indicator {border-radius:10px;}
            QCheckBox::indicator:checked,QRadioButton::indicator:checked {background:#1f9cff;border:2px solid #e1ebf7;}
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

    def _build_cadence(self):
        body = self._page('cadence', '1. When should Optimus run?',
            'These questions configure the existing Optimize button. Choices start from your saved settings. Nothing is saved or run until you choose Save choices. Cancel leaves your preferences unchanged.')
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
        warning = _label('At an automatic cadence, the cleanup, removal, priority and selected-package update choices you authorize can run again without reopening this wizard. Choose Manual if you want to start each run yourself.')
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
            'Choose the cleanup stages you authorize. Eligible cache/temporary files are permanently deleted during a configured maintenance run, after age, path, identity and active-process checks. Browser history, cookies, passwords and bookmarks are outside cache cleanup.')
        self._check(body, 'cleanup_enabled', 'Clean old files in my user temporary folder', default=True)
        self._check(body, 'browser_cache_enabled', 'Clean recognized caches of closed browsers', default=True)
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
        self._check(body, 'applications_review_enabled', 'Review installed applications and include a report (no uninstall)')
        for key, label in REMOVALS:
            checkbox = self._check(body, key, label)
            checkbox.setStyleSheet('color:#ffd29c;')
        body.addWidget(_label('Bing Wallpaper is always preserved by Optimus debloat. Store support, security components, frameworks and shared WebView2 are also protected. High intensity does not select removal groups for you.'))
        body.addWidget(_label('These choices do not invoke an existing external Desktop removal script. Use the detailed Debloat page to inspect exact targets and that script\'s separate approval flow.'))
        body.addStretch()

    def _build_duplicates(self):
        body = self._page('duplicates', '5. Duplicate Finder',
            'Compare eligible file contents in the local folders you choose, using a bounded scan. Save/report matching groups for review. This workflow never automatically deletes or recycles duplicate files.')
        self._check(body, 'duplicates_enabled', 'Scan these folders for duplicate files and save a report')
        self.duplicate_roots = QPlainTextEdit()
        self.duplicate_roots.setPlaceholderText('One absolute local folder per line, for example C:\\Users\\YourName\\Downloads')
        self.duplicate_roots.setPlainText('\n'.join(self.initial.get('duplicate_roots', [])))
        self.duplicate_roots.setMinimumHeight(150)
        body.addWidget(self.duplicate_roots)
        browse = QPushButton('Add local folder...')
        browse.clicked.connect(self._browse_duplicate_root)
        body.addWidget(browse)
        body.addWidget(_label('Links, protected locations and cloud placeholders can be excluded. Scans have file/time limits. Identical content does not mean a file is disposable; removal remains a separate reviewed action in Duplicate finder.'))
        body.addStretch()

    def _browse_duplicate_root(self):
        directory = QFileDialog.getExistingDirectory(self, 'Choose a local folder to scan')
        if directory:
            current = self.duplicate_roots.toPlainText().strip()
            self.duplicate_roots.setPlainText(current + ('\n' if current else '') + directory)

    def _build_cloud(self):
        body = self._page('cloud', '6. Cloud drive - personal OneDrive',
            'Optionally include a personal OneDrive metadata duplicate scan/report. An explicit account connection and your own supported Microsoft public-client app registration are required on the Cloud drive page.')
        self._check(body, 'cloud_review_enabled', 'Scan/report my connected personal OneDrive (no cloud deletion)')
        body.addWidget(_label('No sign-in is started by this wizard. Without an existing connection, the report explains why the scan was skipped. Metadata hash/size matches are candidates; file contents are not downloaded for comparison.'))
        body.addWidget(_label('There is no automatic cloud-file deletion. Google Drive, Dropbox, business/shared drives and SharePoint are unsupported. Any later single-file recycle requires a separate review and confirmation.'))
        body.addStretch()

    def _build_performance(self):
        body = self._page('performance', '7. Performance Optimizer',
            'Review process CPU/memory and priority/affinity information without closing applications. Optional Codex priority and background sampling are separate choices. Existing external priority watchers are preserved.')
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
            'Include sign-in entry and service inventory in the maintenance report. Review which application owns each entry before changing it in the detailed Startup tools.')
        self._check(body, 'startup_review_enabled', 'Review/report startup entries and services (no automatic disabling)')
        body.addWidget(_label('This selection does not disable or remove startup entries, stop services or change their startup modes. Those actions keep their own selected-entry confirmation and supported restore history.'))
        body.addStretch()

    def _build_software(self):
        body = self._page('software', '9. Software Updater',
            'Use WinGet to check available application updates. Choose review-only reporting, or explicitly authorize installation of only the exact package IDs you enter. This is not permission to update every app or any future unlisted package.')
        self._check(body, 'software_updates_enabled', 'Include the Software Updater stage')
        self.software_policy = QComboBox()
        self.software_policy.addItem('Scan and report only', 'review')
        self.software_policy.addItem('Install only the exact package IDs listed below', 'selected')
        self.software_policy.setCurrentIndex(max(0, self.software_policy.findData(self.initial.get('software_update_policy', 'review'))))
        body.addWidget(self.software_policy)
        self.software_ids = QPlainTextEdit()
        self.software_ids.setPlaceholderText('Exact WinGet package IDs, one per line or comma-separated. Example: Vendor.Application')
        self.software_ids.setPlainText('\n'.join(self.initial.get('software_update_ids', [])))
        self.software_ids.setMinimumHeight(130)
        body.addWidget(self.software_ids)
        self.software_agreements = self._check(body, 'software_update_agreements_accepted',
            'Allow silent installation of my listed packages and accept their package/source agreements')
        body.addWidget(_label('The installation choice authorizes only these listed IDs. Vendors may require administrator access or closed applications; installers may fail or require a later restart. Optimus does not request an automatic reboot. Review-only does not accept agreements.'))
        self.software_policy.currentIndexChanged.connect(self._software_controls)
        self.controls['software_updates_enabled'].toggled.connect(self._software_controls)
        self._software_controls()
        body.addStretch()

    def _software_controls(self, *unused):
        selected = self.controls['software_updates_enabled'].isChecked() and self.software_policy.currentData() == 'selected'
        self.software_ids.setEnabled(selected)
        self.software_agreements.setEnabled(selected)

    def _build_drivers(self):
        body = self._page('drivers', '10. Driver Updater',
            'Read installed driver information and check Windows Update for available driver candidates. Save results for a later review. Availability and compatibility depend on Windows Update and your hardware.')
        self._check(body, 'driver_updates_enabled', 'Scan/report driver updates (no automatic driver installation)')
        body.addWidget(_label('There are no blanket driver changes, automatic driver removals or automatic reboots. To install a candidate later, use Drivers, select its exact update, review it and confirm explicitly. Administrator access may be required.'))
        body.addStretch()

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

    def _package_ids(self):
        values = []
        for entry in re.split(r'[\r\n,]+', self.software_ids.toPlainText()):
            value = entry.strip()
            if not value:
                continue
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]{1,127}', value):
                raise ValueError('Enter exact WinGet package IDs only, separated by new lines or commas. Do not enter commands, switches or URLs.')
            if value.casefold() not in {item.casefold() for item in values}:
                values.append(value)
        return values

    def collect_choices(self, *, validate=True):
        result = copy.deepcopy(self.initial)
        for key, control in self.controls.items():
            if isinstance(control, QCheckBox):
                result[key] = control.isChecked()
            elif isinstance(control, QSpinBox):
                result[key] = control.value()
            elif isinstance(control, QComboBox):
                result[key] = control.currentText()
        result.update({'setup_version':1, 'automation_mode':next(mode for mode,button in self.cadence_buttons.items() if button.isChecked()),
            'cleanup_category_ids':[key for key,control in self.category_controls.items() if control.isChecked()],
            'software_update_policy':self.software_policy.currentData()})
        if validate:
            result['duplicate_roots'] = self._roots()
            result['software_update_ids'] = self._package_ids()
            if result['duplicates_enabled'] and not result['duplicate_roots']:
                raise ValueError('Add at least one local folder for Duplicate Finder, or turn its scan off.')
            selected = result['software_updates_enabled'] and result['software_update_policy'] == 'selected'
            if selected and not result['software_update_ids']:
                raise ValueError('List at least one exact WinGet package ID, or choose scan/report only.')
            if selected and not result['software_update_agreements_accepted']:
                raise ValueError('Selected-package installation requires your explicit package/source agreement authorization. Choose review-only to avoid accepting agreements.')
        else:
            result['duplicate_roots'] = [entry.strip() for entry in self.duplicate_roots.toPlainText().splitlines() if entry.strip()]
            result['software_update_ids'] = [entry.strip() for entry in re.split(r'[\r\n,]+',self.software_ids.toPlainText()) if entry.strip()]
        # Review-only never grants agreement acceptance.
        if result['software_update_policy'] != 'selected' or not result['software_updates_enabled']:
            result['software_update_agreements_accepted'] = False
        return result

    def refresh_summary(self):
        settings = self.collect_choices(validate=False)
        actions = []
        if settings.get('cleanup_enabled'):
            actions.append('Permanently delete eligible old user temporary files')
        if settings.get('browser_cache_enabled'):
            actions.append('Permanently delete eligible caches of closed browsers')
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
        reviews = ['Initial and final Health Check/resource reports']
        for key, text in (('applications_review_enabled','Installed applications inventory'),('performance_review_enabled','Detailed process/resource review'),('startup_review_enabled','Startup/service inventory review'),('duplicates_enabled','Local duplicate scan/report: ' + '; '.join(settings['duplicate_roots'])),('cloud_review_enabled','Connected personal OneDrive metadata scan/report (sign-in required)'),('driver_updates_enabled','Driver update scan/report; no installation')):
            if settings.get(key):
                reviews.append(text)
        if settings.get('software_updates_enabled') and settings['software_update_policy'] == 'review':
            reviews.append('Software update scan/report; no agreements accepted or installation')
        lines = ['CADENCE',CADENCES[settings['automation_mode']], '', 'CLEANUP AGE', str(settings.get('cleanup_age_days',7)) + ' days (' + settings.get('intensity','Standard') + ' preset)', '', 'AUTHORIZED CHANGES / PERSISTENT ANALYSIS']
        lines.extend('- ' + text for text in actions or ['None; this configuration is scan/report only.'])
        lines.extend(['', 'SCAN / REPORT ONLY'])
        lines.extend('- ' + text for text in reviews)
        lines.extend(['', 'ALWAYS PRESERVED / SEPARATE REVIEW', 'Bing Wallpaper is protected. No automatic duplicate/cloud deletion, startup disabling or driver installation. Optional removals may require reinstallation; cleanup is permanent.', '', 'Save does not run maintenance unless Run selected maintenance now is checked.'])
        self.summary.setPlainText('\n'.join(lines))

    def validateCurrentPage(self):
        try:
            if self.currentId() == self.page_ids['duplicates'] and self.controls['duplicates_enabled'].isChecked():
                if not self._roots():
                    raise ValueError('Add at least one local folder, or turn Duplicate Finder off.')
            if self.currentId() == self.page_ids['software']:
                self.collect_choices()
        except ValueError as error:
            QMessageBox.warning(self, 'Review setup choices', str(error))
            return False
        return super().validateCurrentPage()

    def accept(self):
        try:
            preferences = self.collect_choices()
            from .automation import save_setup
            saved = save_setup(self.backend.data_dir, preferences, self.backend.save_preferences,
                               run_now=self.run_now_checkbox.isChecked())
            if not isinstance(saved, dict) or int(saved.get('setup_version', 0)) < 1:
                raise RuntimeError('The settings engine did not retain setup choices. No maintenance was started.')
        except Exception as error:
            self.save_error.setText('Choices were not saved: ' + str(error))
            self.refresh_summary()
            return
        self.saved_preferences = copy.deepcopy(saved)
        self.run_now = self.run_now_checkbox.isChecked()
        super().accept()
