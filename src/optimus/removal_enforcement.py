"""Saved, exact application-removal choices and periodic reappearance checks.

This does not disable Windows Update, Store updates, WebView2 or remote access.
Scheduled execution requires a saved scope approval, never defaults alone.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .app_removal_protection import WINDOWS_APP_NAME, WINDOWS_APP_FAMILY, validate_app_removal_exclusions

SCOPE_KEY = 'windows_removal_enforcement_scope_hash'
GROUPS = (
    ('consumer_debloat_enabled', 'consumer-apps'),
    ('m365_debloat_enabled', 'm365'),
    ('office365_desktop_debloat_enabled', 'office365-desktop'),
    ('office_desktop_debloat_enabled', 'office-desktop'),
    ('outlook_debloat_enabled', 'outlook'),
    ('edge_debloat_enabled', 'edge'),
)
_EDGE_IDS = ('Microsoft.Edge',)
_M365_IDS = ('Microsoft.Office', 'Microsoft.Office365Business', 'Microsoft.Office365ProPlus',
             'Microsoft.Microsoft365', 'Microsoft.Microsoft365Apps')
_OFFICE_IDS = ('Microsoft.Office.2016', 'Microsoft.Office.2019', 'Microsoft.Office.2021',
               'Microsoft.Office.2024', 'Microsoft.Office2021', 'Microsoft.Office2024')


def approval_scope(settings):
    flags = {key: settings.get(key, False) for key, _ in GROUPS}
    flags.update({key: value for key, value in settings.items() if key.startswith('privacy_')})
    if any(type(value) is not bool for value in flags.values()):
        raise ValueError('Removal and privacy choices must be explicit booleans.')
    names = settings.get('consumer_debloat_package_names', [])
    if type(names) is not list or any(not isinstance(name, str) for name in names):
        raise ValueError('Save exact selected Windows package names.')
    return {'schema': 1, 'flags': flags, 'consumer_packages': sorted(set(names)),
            'exclusions': validate_app_removal_exclusions(settings.get('app_removal_exclusions', [])),
            'preserved': [WINDOWS_APP_FAMILY, 'Microsoft.EdgeWebView2'], 'include_provisioned': True}


def scope_hash(settings):
    raw = json.dumps(approval_scope(settings), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def stamp_approval(settings):
    result = dict(settings)
    result[SCOPE_KEY] = scope_hash(result) if result.get('windows_removal_enforcement_enabled') is True else ''
    return result


def approved(settings):
    return (settings.get('setup_version') == 2 and settings.get('windows_removal_enforcement_enabled') is True
            and isinstance(settings.get(SCOPE_KEY), str) and settings[SCOPE_KEY] == scope_hash(settings))


def blocked_package_ids(settings):
    """Keep Optimus's own updater/presence rules from reinstalling removals."""
    result = set()
    if settings.get('edge_debloat_enabled') is True:
        result.update(_EDGE_IDS)
    if settings.get('office365_desktop_debloat_enabled') is True:
        result.update(_M365_IDS)
    if settings.get('office_desktop_debloat_enabled') is True:
        result.update(_OFFICE_IDS)
    if settings.get('m365_debloat_enabled') is True:
        result.update(('Microsoft.MicrosoftOfficeHub', 'Microsoft.M365Companions'))
    return result


def package_blocked(settings, identity):
    return isinstance(identity, str) and identity.casefold() in {value.casefold() for value in blocked_package_ids(settings)}


def run_saved_removals(backend, *, apply_privacy=True):
    from .automation import RunLease
    from .workflow import atomic_json
    settings = backend.preferences()
    report = {'schema': 1, 'id': uuid.uuid4().hex, 'started_at': time.time(), 'actions': [],
              'windows_app_preserved': True, 'webview2_preserved': True,
              'note': 'Supported installer policies are edition dependent. Scheduled removal detects reappearance; it cannot prevent every Windows servicing payload from restoring files.'}
    if getattr(backend, 'system', sys.platform) != 'win32':
        return dict(report, status='unavailable', reason='Windows-only removal enforcement.')
    if not approved(settings):
        return dict(report, status='approval_required', reason='Save the current removal and privacy choices before unattended enforcement.')
    try:
        lease = RunLease(backend.data_dir).acquire()
    except (OSError, RuntimeError):
        return dict(report, status='busy_deferred', reason='Another maintenance operation owns the run; this check started no uninstallers.')
    try:
        current = backend.preferences()
        if not approved(current) or scope_hash(current) != scope_hash(settings):
            report['actions'].append({'status': 'approval_changed'})
        if (approved(current) and scope_hash(current) == scope_hash(settings)
                and settings.get('edge_debloat_enabled') is True and callable(getattr(backend, 'edge_install_policy', None))):
            try:
                report['edge_install_policy'] = backend.edge_install_policy()
            except Exception as error:
                report['edge_install_policy'] = {'applied': False, 'error': str(error)[:2000]}
        for flag, action in GROUPS:
            if settings.get(flag) is not True:
                continue
            if not approved(backend.preferences()) or scope_hash(backend.preferences()) != scope_hash(settings):
                report['actions'].append({'action': action, 'status': 'approval_changed'})
                break
            try:
                options = {'action': action, 'includeProvisioned': action not in ('office365-desktop', 'office-desktop')}
                plan = backend.debloat_preview(options)
                targets = plan.get('targets', [])
                if action == 'consumer-apps':
                    selected = set(settings['consumer_debloat_package_names'])
                    targets = [row for row in targets if row.get('name') in selected]
                targets = [row for row in targets if row.get('name') != WINDOWS_APP_NAME
                           and str(row.get('appIdentity') or '').casefold() != WINDOWS_APP_FAMILY.casefold()]
                ids = [row['id'] for row in targets]
                current = backend.preferences()
                if not approved(current) or scope_hash(current) != scope_hash(settings):
                    report['actions'].append({'action': action, 'status': 'approval_changed'})
                    break
                result = (backend.debloat_apply({'planId': plan['planId'], 'selectedIds': ids, 'confirmed': True})
                          if ids else {'skipped': True, 'reason': 'No selected installed targets found.', 'results': []})
                report['actions'].append({'action': action, 'result': result})
                # An unknown native installer is a shared prerequisite; never
                # launch another installer against a possibly live one.
                if any(isinstance(row, dict) and (row.get('running') is True or row.get('outcomeKnown') is False)
                       for row in result.get('results', [])):
                    report['actions'].append({'status': 'installer_outcome_pending'})
                    break
            except Exception as error:
                report['actions'].append({'action': action, 'status': 'failed', 'error': str(error)[:2000]})
                if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
                    # Keep the foreground lease until the already launched
                    # native operation settles. Independent terminal failures
                    # continue; a timeout cannot authorize a second installer.
                    report['actions'].append({'status': 'installer_outcome_pending'})
                    observer = getattr(getattr(backend, 'bridge', None), 'wait_for_pending_operation', None)
                    if callable(observer):
                        observer('maintenance_apply')
                    break
        if apply_privacy and approved(backend.preferences()) and scope_hash(backend.preferences()) == scope_hash(settings):
            try:
                report['privacy'] = backend.apply_system_privacy(settings)
            except Exception as error:
                report['privacy'] = {'status': 'failed', 'error': str(error)[:2000]}
        issues = any(row.get('status') in ('failed', 'approval_changed', 'installer_outcome_pending')
                     or any(not isinstance(item, dict) or item.get('requiresReview') or item.get('error')
                            for item in row.get('result', {}).get('results', []))
                     for row in report['actions'])
        issues = issues or report.get('privacy', {}).get('status') in ('failed', 'completed_with_errors')
        issues = issues or bool(report.get('edge_install_policy', {}).get('error'))
        report.update(status='completed_with_findings' if issues else 'completed', finished_at=time.time())
        destination = Path(backend.data_dir) / 'removal-enforcement'
        destination.mkdir(exist_ok=True)
        atomic_json(destination / (report['id'] + '.json'), report)
        atomic_json(destination / 'latest.json', report)
        return report
    finally:
        lease.release()


class RemovalGuardRegistration:
    def __init__(self, data_dir, *, runner=None):
        self.data_dir = Path(data_dir)
        self.runner = runner or self._native

    def _native(self, request):
        import os
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = Path(__file__).with_name('engine') / 'RemovalGuardTasks.ps1'
        result = subprocess.run([str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(script), '-RequestJson'],
                                input=json.dumps(request), capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        value = json.loads(result.stdout.lstrip('\ufeff'))
        if result.returncode or not isinstance(value, dict) or value.get('ok') is not True:
            raise RuntimeError(value.get('error', 'The removal guard task could not be configured.'))
        return value

    def configure(self, enabled, settings=None):
        request = {'operation': 'enable' if enabled else 'disable', 'dataRoot': str(self.data_dir),
                   'launchManifest': str(self.data_dir / 'startup-launch.json')}
        if enabled:
            if not isinstance(settings, dict) or not approved(settings):
                raise ValueError('Save the current removal choices before enabling scheduled enforcement.')
            request.update(approvedScope=approval_scope(settings), scopeHash=scope_hash(settings))
        return self.runner(request)

    def status(self):
        return self.runner({'operation': 'status', 'dataRoot': str(self.data_dir),
                            'launchManifest': str(self.data_dir / 'startup-launch.json')})
