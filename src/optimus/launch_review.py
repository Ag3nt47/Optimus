"""Per-launch review of saved maintenance scopes, without persisted consent.

The fingerprint is deliberately conservative: any saved configuration change
invalidates this process's approval. Reading a warning or constructing the
dialog never runs maintenance, probes the machine or changes preferences.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading


def scope_fingerprint(settings):
    if not isinstance(settings, dict):
        raise ValueError('Saved maintenance choices must be an object.')
    body = json.dumps(settings, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    if len(body) > 4 * 1024 * 1024:
        raise ValueError('Saved maintenance choices exceed the launch-review limit.')
    return hashlib.sha256(body.encode()).hexdigest()


class LaunchConsent:
    def __init__(self, *, required=True):
        self.required = required is True
        self._approved = None
        self._lock = threading.RLock()

    def approve(self, settings):
        fingerprint = scope_fingerprint(settings)
        with self._lock:
            self._approved = fingerprint
        return fingerprint

    def invalidate(self):
        with self._lock:
            self._approved = None

    def allows(self, settings):
        if not self.required:
            return True
        fingerprint = scope_fingerprint(settings)
        with self._lock:
            if self._approved != fingerprint:
                self._approved = None
                return False
            return True


def _listed(values, *, maximum=8):
    if not isinstance(values, list):
        return 'unverified saved selection'
    rows = [str(value) for value in values]
    return ', '.join(rows[:maximum]) + (f' (+{len(rows)-maximum} more)' if len(rows) > maximum else '') or 'none selected'


def launch_warning(settings):
    """Describe actual saved requests; native/applicability checks still apply."""
    scope_fingerprint(settings)
    settings = copy.deepcopy(settings)
    from .software_app_rules import app_rules_summary, apply_update_rules
    rule_scope = app_rules_summary(settings)
    actions, checks = [], []
    from .toolkit_configuration import launch_descriptions
    extra_actions, extra_checks = launch_descriptions(settings)
    actions.extend(extra_actions)
    checks.extend(extra_checks)
    categories = list(settings.get('cleanup_category_ids') or [])
    if settings.get('cleanup_enabled') is True:
        categories.insert(0, 'user-temp')
    if categories:
        age = settings.get('cleanup_age_days', 7)
        age = max(30, age) if settings.get('intensity') == 'Low' and type(age) is int else age
        actions.append('Permanently delete eligible temporary/cache files in saved categories: '
                       + _listed(list(dict.fromkeys(categories))) + f'. Saved minimum file age: {age} day(s).')
    from .retention_configuration import retention_actions
    actions.extend(retention_actions(settings))
    if settings.get('drive_space_guard_enabled', True) is True:
        checks.append('Read-only drive-space check before and after maintenance; warn below '
            + str(settings.get('drive_space_guard_target_gib', 2.0))
            + ' GiB free. This does not reserve space or authorize additional deletion; installers may require more space.')
    browser_categories = [title for key, title in (
        ('browser_cache_enabled', 'cache'), ('browser_history_enabled', 'history'),
        ('browser_cookies_enabled', 'cookies'), ('browser_download_history_enabled', 'download-list records'))
        if settings.get(key) is True]
    if browser_categories:
        browsers = settings.get('browser_cleanup_browsers') or []
        if browsers:
            actions.append('Clean browser ' + ', '.join(browser_categories) + ' for: ' + _listed(browsers)
                           + '. Each open browser gets a close-and-clean or skip choice.')
            if settings.get('browser_cookies_enabled') is True:
                actions.append('Cookie cleaning can sign you out. Kept cookie domains: ' + _listed(settings.get('browser_keep_domains') or []) + '.')
        else:
            checks.append('Browser cleanup has no selected browser; no browser family is authorized.')
    if settings.get('consumer_debloat_enabled') is True:
        packages = settings.get('consumer_debloat_package_names') or []
        if packages:
            actions.append('Uninstall selected optional Windows consumer apps if installed: ' + _listed(packages) + '. App-local data may be removed.')
        else:
            checks.append('Consumer-app removal has no selected package names.')
    for key, description in (
        ('m365_debloat_enabled', 'Uninstall Microsoft 365 hub/companion Store apps if installed; app-local data may be removed.'),
        ('outlook_debloat_enabled', 'Uninstall the new Outlook Store app if installed; app-local data may be removed.'),
        ('edge_debloat_enabled', 'Request Microsoft Edge removal through its supported uninstaller if available.')):
        if settings.get(key) is True:
            actions.append(description)
    for prefix, label, roots in (('duplicate', 'local duplicate', settings.get('duplicate_roots') or []),
                                 ('cloud', 'OneDrive duplicate', settings.get('cloud_folder_ids') or [])):
        enabled = settings.get('duplicates_enabled' if prefix == 'duplicate' else 'cloud_review_enabled') is True
        if enabled and settings.get(prefix + '_policy', 'review') != 'review' and settings.get(prefix + '_changes_approved') is True:
            actions.append('Recycle approved ' + label + ' copies under saved folder/keeper/age/count limits: ' + _listed(roots) + '.')
        elif enabled:
            checks.append(label.capitalize() + ' review only; no automatic recycling is approved.')
    if settings.get('startup_review_enabled') is True:
        if settings.get('startup_policy') == 'disable_selected' and settings.get('startup_changes_approved') is True:
            entries = settings.get('startup_approved_entries') or []
            actions.append(f'Disable up to {len(entries)} exact approved startup registration(s) if unchanged.')
        else:
            checks.append('Startup review only; no startup entries are selected for automatic disabling.')
    if settings.get('software_updates_enabled') is True:
        effective = apply_update_rules(settings)
        if (effective.get('software_update_policy') in ('selected', 'all_supported')
                and settings.get('software_update_agreements_accepted') is True):
            scope = ('all supported current package-manager offers; saved exclusions: ' + _listed(effective.get('software_update_exclude_ids') or [])
                     if effective.get('software_update_policy') == 'all_supported' and settings.get('software_update_all_approved') is True
                     else 'saved exact package IDs: ' + _listed(effective.get('software_update_ids') or []))
            actions.append('Install available eligible software updates for ' + scope + '. Installer/restart checks still apply.')
        elif not rule_scope['automatic_actions']:
            checks.append('Software update discovery only; automatic installation is not approved.')
        for key, text in (
            ('always_update', 'Always update only available eligible newer versions for exact app rules: '),
            ('always_ignore', 'Always ignore updates for exact app rules: '),
            ('keep_installed', 'Install or reinstall these exact apps if missing: '),
            ('keep_uninstalled', 'Uninstall these exact apps if installed; app-local data can be permanently lost: ')):
            if rule_scope[key]:
                actions.append(text + ', '.join(rule_scope[key]) + '.')
        if rule_scope['automatic_actions']:
            actions.append('Saved per-app rules apply on each maintenance run. Reinstalling an app does not restore app-local data removed by uninstalling it. '
                           'Per-app actions require saved package agreements, fresh identity/source checks and protected-app/installer checks. Saved update exclusions and pins still win over Always update. '
                           'Install/uninstall rules currently run only on Windows with WinGet; other platforms report unsupported actions.')
    if settings.get('driver_updates_enabled') is True:
        if (settings.get('driver_policy') in ('install_approved', 'install_matching')
                and settings.get('driver_changes_approved') is True and settings.get('driver_update_agreements_accepted') is True):
            actions.append('Install applicable Windows driver updates within saved exact update/device approvals, after backup and native safety checks. A needed restart is reported.')
        else:
            checks.append('Driver update discovery only; automatic installation is not approved.')
    if settings.get('windows_security_updates_enabled') is True:
        if settings.get('windows_security_updates_install_approved') is True and settings.get('windows_security_update_agreements_accepted') is True:
            actions.append('Install eligible Windows Security/Critical updates under saved approval; a needed restart is reported.')
        else:
            checks.append('Windows security-update discovery only.')
    for key, description in (
        ('privacy_advertising_enabled', 'Apply saved supported Windows advertising/privacy preferences with recovery records.'),
        ('privacy_diagnostics_enabled', 'Apply saved supported optional/app diagnostic-sharing preferences with recovery records.'),
        ('privacy_block_silent_consumer_apps_enabled', 'Apply supported preferences/policies to reduce silent consumer-app promotions; Windows edition limits apply.'),
        ('privacy_disable_cloud_promotions_enabled', 'Apply supported policies to reduce cloud/account promotions; Windows edition limits apply.'),
        ('privacy_disable_clipboard_sync_enabled', 'Turn off supported Windows cross-device clipboard syncing; local clipboard history is unchanged. Windows edition limits apply.'),
        ('privacy_disable_recall_snapshots_enabled', 'Disable supported Windows Recall snapshot saving. Windows also permanently deletes previous Recall snapshots; undoing this policy cannot recover deleted snapshots. Windows edition/build limits apply.'),
        ('speed_enhancements_enabled', 'Apply supported saved power/performance preferences; power use or heat can increase.'),
        ('drive_optimization_enabled', 'Run eligible Windows SSD TRIM/HDD optimization after native checks.'),
        ('codex_priority_enabled', 'Apply the saved Codex process-priority profile.'),
        ('spyware_scan_enabled', 'Run the configured malware scan using Windows Defender\'s existing remediation policy.'),
        ('auto_continue_fixes_enabled', 'Automatically continue supported post-scan security fixes/updates within existing saved approvals.'),
        ('smart_cleaning_enabled', 'Allow app-open Smart Cleaning at the saved junk-size threshold and cooldown.'),
        ('browser_exit_cleaning_enabled', 'Allow cleanup of selected browser data after an observed browser exit, using saved categories and cookie exceptions.')):
        if settings.get(key) is True:
            actions.append(description)
    if settings.get('background_analysis_enabled') is True:
        checks.append('Resume the saved background resource analysis after approval.')
    if not actions:
        checks.insert(0, 'No automatic deletion, app-removal or update-install request is enabled in this saved configuration.')
    header = ('Auto Clean runs the saved requests below. File deletion, browser-data removal and app uninstallation can be irreversible; app data can be lost.'
              if any(settings.get(key) is True for key in ('cleanup_enabled', 'browser_cache_enabled', 'browser_history_enabled',
                  'browser_cookies_enabled', 'browser_download_history_enabled', 'consumer_debloat_enabled',
                  'm365_debloat_enabled', 'outlook_debloat_enabled', 'edge_debloat_enabled',
                  'codex_archives_cleanup_enabled', 'angerona_logs_cleanup_enabled', 'failed_downloads_cleanup_enabled',
                  'privacy_disable_recall_snapshots_enabled')) or categories or rule_scope['enabled'] and rule_scope['keep_uninstalled']
              else 'Auto Clean runs the saved checks and approved actions below. Native eligibility and saved scopes still apply.')
    lines = [header, '', *('• ' + action for action in actions)]
    if checks:
        lines.extend(['', 'Checks and review:', *('• ' + check for check in checks)])
    if settings.get('setup_version', 0) < 2:
        lines.extend(['', 'Current setup is incomplete. Auto Clean first opens setup, then shows a fresh review of the saved choices before any cleanup.'])
    lines.extend(['', 'Adjust settings is the default. It opens your choices and keeps automatic maintenance paused. '
                  'Closing this review also leaves maintenance paused. No universal undo or guaranteed performance gain is claimed.'])
    return '\n'.join(lines)


def ask_launch_review(parent, settings):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout
    dialog = QDialog(parent)
    dialog.setWindowTitle('Optimus — review this launch')
    dialog.setProperty('_optimus_no_reveal', True)
    dialog.setMinimumSize(480, 400)
    dialog.resize(760, 660)
    layout = QVBoxLayout(dialog)
    title = QLabel('Review saved actions before Auto Clean')
    title.setTextFormat(Qt.PlainText)
    title.setWordWrap(True)
    layout.addWidget(title)
    details = QPlainTextEdit(launch_warning(settings))
    details.setReadOnly(True)
    details.setAccessibleName('Saved actions and data-removal warning for this launch')
    layout.addWidget(details, 1)
    bar = QHBoxLayout()
    auto = QPushButton('Auto Clean')
    auto.setAutoDefault(False)
    auto.setAccessibleName('Approve these saved actions and start Auto Clean')
    adjust = QPushButton('Adjust settings')
    adjust.setDefault(True)
    adjust.setAutoDefault(True)
    adjust.setAccessibleName('Adjust settings; automatic maintenance stays paused')
    for button in (auto, adjust):
        button.setMinimumHeight(42)
        bar.addWidget(button)
    layout.addLayout(bar)
    choice = {'value': None}
    def select(value):
        choice['value'] = value
        dialog.accept()
    auto.clicked.connect(lambda:select('auto_clean'))
    adjust.clicked.connect(lambda:select('adjust_settings'))
    adjust.setFocus(Qt.OtherFocusReason)
    if parent is not None:
        parent._launch_review_dialog = dialog
    try:
        dialog.exec()
        return choice['value']
    finally:
        if parent is not None:
            parent._launch_review_dialog = None
        dialog.deleteLater()
