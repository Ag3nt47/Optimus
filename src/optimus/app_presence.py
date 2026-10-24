"""Exact saved application presence, inside the software worker's installer lane.

Only the built-in WinGet community source and known quiet installer handlers
are supported. No command strings, manifests, force switches, dependency removal,
automatic reboot or background service are created. Read-only inspection does
not authorize an installation. Caller owns RunLease and the shared installer gate.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import uuid

from .automation import CURRENT_SETUP_VERSION
from .filesystem import _ordinary_data_path
from .speed_enhancements import _current_owner, _private_json
from .update_holds import load_update_holds, matching_update_hold
from .item_progress import BoundItemProgress, accepts_progress_callback, normalize_item_progress

SOURCES = (
    'https://learn.microsoft.com/en-us/windows/package-manager/winget/install',
    'https://learn.microsoft.com/en-us/windows/package-manager/winget/uninstall',
    'https://learn.microsoft.com/en-us/windows/package-manager/winget/show',
    'https://learn.microsoft.com/en-us/windows/package-manager/winget/source',
    'https://github.com/microsoft/winget-cli/blob/master/src/AppInstallerCommonCore/Manifest/ManifestCommon.cpp',
)
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+\-]{1,127}\Z')
_VERSION = re.compile(r'[vV]?[0-9][0-9A-Za-z._+~\-]{0,127}\Z')
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
# WinGet has native quiet + no-restart switches for MSI/WiX/Burn and Inno.
# Generic EXE/archive/portable and Nullsoft (no standard no-restart switch)
# require separate manual review. MSIX/AppX uses the native deployment handler.
SUPPORTED_INSTALLER_TYPES = ('msi', 'wix', 'msix', 'appx', 'burn', 'inno')
_QUIET_TYPES = frozenset(SUPPORTED_INSTALLER_TYPES)
_NO_APPLICATIONS = 0x8A150014
_TITLE = 'Apply saved application presence rules'


class PresencePreflightError(ValueError):
    installer_started = False
    outcomeKnown = True
    running = False

    def __init__(self, reason, *, failure_kind='preflight_unverified', failure_scope='shared'):
        super().__init__(reason)
        self.failureKind = failure_kind
        self.failure_scope = failure_scope
        self.shared_barrier = failure_scope == 'shared'


class PresenceCancelled(PresencePreflightError):
    pass


def _require(value, reason, *, failure_kind='preflight_unverified', failure_scope='shared'):
    if not value:
        raise PresencePreflightError(reason, failure_kind=failure_kind, failure_scope=failure_scope)


def _check_cancel(event):
    if event is not None and event.is_set():
        raise PresenceCancelled('The current application presence item was skipped before launch.')


def _identity(value):
    _require(isinstance(value, str) and _ID.fullmatch(value) and not value.endswith('.')
             and '..' not in value, 'Choose a complete exact WinGet application ID.')
    return value


def _version(value):
    _require(isinstance(value, str) and _VERSION.fullmatch(value) and not value.endswith('.')
             and '..' not in value, 'The application version is unknown or incomplete.',
             failure_kind='unsupported_application', failure_scope='package')
    return value


def _text(result):
    _require(type(result.returncode) is int, 'The package manager returned no exit status.')
    value = result.stdout or ''
    _require(isinstance(value, str) and len(value) <= 2 * 1024 * 1024,
             'The package manager output is invalid or too large.')
    return _ANSI.sub('', value).replace('\r', '')


def protected_application(identity, name=''):
    """Preserve system/framework, security and the user's explicitly kept apps."""
    value = re.sub(r'[^a-z0-9]', '', identity.casefold() + ' ' + name.casefold())
    protected = ('bingwallpaper', 'optimus', 'codex', 'angerona', 'windowsdefender',
        'microsoftdefender', 'antivirus', 'antimalware', 'crowdstrike', 'sentinelone',
        'bitdefender', 'malwarebytes', 'kaspersky', 'sophos', 'avast',
        'avgantivirus', 'trendmicro', 'cylance', 'carbonblack', 'symantec', 'norton',
        'webroot', 'fsecure', 'paloaltonetworks', 'fortinet', 'securityhealth')
    prefixes = ('microsoft.windows', 'microsoft.vcredist', 'microsoft.dotnet',
        'microsoft.edgewebview2', 'microsoft.desktopappinstaller', 'microsoft.winget',
        'microsoft.powershell', 'microsoft.directx', 'microsoft.ui.xaml',
        'microsoft.vclibs', 'microsoft.net', 'microsoft.appinstaller')
    return (any(part in value for part in protected) or identity.casefold().startswith(prefixes)
            or identity.casefold().startswith('eset.') or re.search(r'\beset\b', name, re.I) is not None)


def parse_installed(result, identity):
    """Accept one complete exact source-scoped row, or WinGet's no-match HRESULT.

    CLI tables are localized; unsupported layouts stay unverified, never absent.
    The caller always fixes --source winget even when that column is omitted.
    """
    identity = _identity(identity)
    text = _text(result)
    if result.returncode & 0xffffffff == _NO_APPLICATIONS:
        _require(not re.search(r'^Name +Id +Version', text, re.M),
                 'The installed query returned conflicting no-match and application table evidence.')
        return None
    _require(result.returncode == 0, 'The installed application query failed; absence is unverified.')
    lines = text.splitlines()
    headers = [(index, line) for index, line in enumerate(lines)
        if re.fullmatch(r'Name {2,}Id {2,}Version(?: {2,}Available)?(?: {2,}Source)? *', line)]
    _require(len(headers) == 1, 'The installed application table is unsupported or ambiguous.')
    index, header = headers[0]
    _require(index + 1 < len(lines) and re.fullmatch(r'-{10,}', lines[index + 1].strip()),
             'The installed application table is incomplete.')
    labels = [name for name in ('Name', 'Id', 'Version', 'Available', 'Source') if name in header]
    starts = [header.index(label) for label in labels]
    rows = []
    for line in lines[index + 2:]:
        if not line.strip() or re.fullmatch(r'\d+ upgrades? available\.', line.strip()):
            continue
        row = {label: line[start:starts[position + 1] if position + 1 < len(starts) else len(line)].strip()
               for position, (label, start) in enumerate(zip(labels, starts))}
        _require(row.get('Id') == identity and row.get('Name') and
                 all('...' not in value and '\u2026' not in value for value in row.values()) and
                 row.get('Source', 'winget') == 'winget',
                 'The installed query returned another, truncated or ambiguous application.')
        rows.append({'id': identity, 'source': 'winget', 'name': row['Name'],
                     'version': _version(row.get('Version'))})
    _require(len(rows) == 1, 'Exactly one installed application version must be identified.')
    return rows[0]


def parse_catalog(result, identity):
    text = _text(result)
    _require(result.returncode == 0, 'A fresh exact application catalog entry is unavailable.',
             failure_kind='package_unavailable' if result.returncode & 0xffffffff == _NO_APPLICATIONS else 'source_unavailable',
             failure_scope='package' if result.returncode & 0xffffffff == _NO_APPLICATIONS else 'shared')
    found = re.findall(r'^Found ([^\n\r]+) \[([^\]\n\r]+)\] *$', text, re.M)
    _require(len(found) == 1 and found[0][1] == identity and '...' not in found[0][0]
             and '\u2026' not in found[0][0], 'The catalog entry does not identify exactly the selected application.')
    def field(name):
        values = re.findall(r'^ *' + re.escape(name) + r': ([^\n\r]+) *$', text, re.M)
        _require(len(values) == 1, 'The catalog entry has incomplete or repeated ' + name + ' metadata.')
        return values[0].strip()
    kind = field('Installer Type').casefold()
    digest = field('Installer SHA256').lower()
    url = field('Installer Url')
    _require(kind in _QUIET_TYPES, 'This installer type has no supported automatic presence action; review it manually.',
             failure_kind='unsupported_installer', failure_scope='package')
    _require(re.fullmatch('[0-9a-f]{64}', digest) and url.startswith('https://')
             and not any(ord(c) < 32 for c in url), 'The catalog installer hash or HTTPS location is unverified.')
    return {'id': _identity(identity), 'source': 'winget', 'name': found[0][0],
            'version': _version(field('Version')), 'publisher': field('Publisher'),
            'installer_type': kind, 'installer_sha256': digest, 'installer_url': url}


class WindowsAppPresence:
    """Read-only exact catalog/installed probes plus an internal guarded launcher."""
    def __init__(self, backend):
        self.backend = backend
        self.maintenance = backend.maintenance

    def _run(self, arguments, timeout=90, *, progress_callback=None):
        options = {'timeout': timeout}
        if progress_callback is not None and accepts_progress_callback(self.maintenance._run):
            options['progress_callback'] = progress_callback
        return self.maintenance._run([self.maintenance._winget(), *arguments], **options)

    def _source(self):
        result = self._run(['source', 'export', '--name', 'winget', '--disable-interactivity'])
        _require(result.returncode == 0, 'The trusted package source could not be verified.')
        def pairs(values):
            data = {}
            for key, value in values:
                _require(key not in data, 'Duplicate source metadata is invalid.')
                data[key] = value
            return data
        value = json.loads(_text(result), object_pairs_hook=pairs)
        _require(isinstance(value, dict) and value.get('Name') == 'winget'
                 and value.get('Arg') == 'https://cdn.winget.microsoft.com/cache'
                 and value.get('Type') == 'Microsoft.PreIndexed.Package'
                 and value.get('Identifier') == value.get('Data') == 'Microsoft.Winget.Source_8wekyb3d8bbwe',
                 'The winget source is not the built-in trusted community repository.')

    def installed(self, identity):
        return parse_installed(self._run(['list', '--id', _identity(identity), '--exact', '--source', 'winget',
            '--accept-source-agreements', '--disable-interactivity']), identity)

    def inspect(self, identity, source='winget', cancel_event=None, *, desired_presence=None):
        """Read the exact installed state before requiring mutation metadata.

        Manual callers omit desired_presence and still receive the full catalog.
        A saved rule already satisfied needs neither an installer nor its catalog.
        """
        _require(source == 'winget', 'Only the built-in WinGet source supports saved application presence.')
        _require(desired_presence in (None, 'keep_installed', 'keep_uninstalled'),
                 'Choose a supported desired application presence.')
        _check_cancel(cancel_event)
        self._source()
        _check_cancel(cancel_event)
        installed = self.installed(identity)
        _check_cancel(cancel_event)
        if (desired_presence == 'keep_installed' and installed is not None
                or desired_presence == 'keep_uninstalled' and installed is None):
            return {'id': identity, 'source': 'winget', 'installed': installed, 'already_satisfied': True}
        args = ['show', '--id', _identity(identity), '--exact', '--source', 'winget',
                '--accept-source-agreements', '--disable-interactivity']
        # For removal verify the installed release's type, never a different
        # newer catalog release. Absent applications use the current offer.
        if installed is not None:
            args += ['--version', installed['version']]
        catalog = parse_catalog(self._run(args), identity)
        _check_cancel(cancel_event)
        _require(installed is None or catalog['version'] == installed['version'],
                 'The catalog no longer describes the exact installed release.')
        value = {'id': identity, 'source': 'winget', 'installed': installed, 'catalog': catalog,
                 'protected': protected_application(identity, catalog['name']) or
                    installed is not None and protected_application(identity, installed['name'])}
        value['fingerprint'] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return value

    def readiness(self, snapshot, action):
        _require(snapshot.get('protected') is False, 'Windows, security, frameworks, Bing Wallpaper, Codex and Optimus are preserved.',
                 failure_kind='protected_application', failure_scope='package')
        _require(self.backend.is_admin() is True, 'Administrator access is required; no elevation was requested.', failure_kind='admin_required')
        status = self.backend.security_status()
        _require(isinstance(status, dict) and status.get('reboot_required') is False,
                 'A cleared Windows restart state is required before application presence changes.', failure_kind='restart_required')
        if action == 'install':
            disk = self.backend.update_disk_readiness()
            _require(isinstance(disk, dict) and disk.get('ready') is True,
                     'Application installation space is insufficient or unverified.', failure_kind='insufficient_disk')
        package = snapshot['catalog']
        allowed, reason, untrusted = self.maintenance._pin_status(package)
        _require(allowed is True, reason or 'The current application pin inventory needs review.',
                 failure_kind='pin_inventory_untrusted' if untrusted else 'pinned',
                 failure_scope='shared' if untrusted else 'package')
        holds = load_update_holds(self.backend.data_dir)
        hold = matching_update_hold({'id': package['id'], 'available': package['version']}, holds)
        _require(hold is None, 'The exact application release has a saved automatic update hold.',
                 failure_kind='update_hold', failure_scope='package')

    def execute(self, snapshot, action, cancel_event=None, *, navigation=None, approval_callback=None,
                progress_callback=None):
        """Internal: caller saved durable intent and still owns the installer lane."""
        _require(action in ('install', 'uninstall'), 'Unsupported application presence action.')
        if progress_callback is not None and not callable(progress_callback):
            raise PresencePreflightError('An optional application progress observer must be callable.')
        if cancel_event is not None and cancel_event.is_set():
            return _known('Cancelled before the application installer started.', cancelled=True, skipped=True)
        try:
            fresh = self.inspect(snapshot['id'], snapshot['source'], cancel_event)
            _require(fresh == snapshot, 'The exact application identity, version or catalog changed before launch.')
            self.readiness(fresh, action)
        except PresencePreflightError:
            raise
        except Exception as error:
            raise PresencePreflightError('Application preflight failed before launch (' + type(error).__name__ + ').') from error
        if cancel_event is not None and cancel_event.is_set():
            return _known('Cancelled before the application installer started.', cancelled=True, skipped=True)
        if approval_callback is not None:
            try:
                approval_callback()
            except PresencePreflightError:
                raise
            except Exception as error:
                raise PresencePreflightError('Saved application approval could not be read before launch.') from error
        if cancel_event is not None and cancel_event.is_set():
            return _known('Cancelled before the application installer started.', cancelled=True, skipped=True)
        arguments = [action, '--id', fresh['id'], '--exact', '--source', 'winget',
            '--version', fresh['catalog']['version'], '--silent', '--disable-interactivity', '--accept-source-agreements']
        if action == 'install':
            arguments += ['--accept-package-agreements', '--no-upgrade', '--installer-type', fresh['catalog']['installer_type']]
        if navigation is not None:
            navigation.phase('installing' if action == 'install' else 'uninstalling', mutation_started=True,
                             message='The quiet application operation must return before this worker continues.')
        if cancel_event is not None and cancel_event.is_set():
            return _known('Cancelled before the application installer started.', cancelled=True, skipped=True)
        try:
            result = self._run(arguments, timeout=1800, progress_callback=progress_callback)
        except (FileNotFoundError, PermissionError) as error:
            raise PresencePreflightError('WinGet could not be started; no application installer was launched.') from error
        except Exception as error:
            if (getattr(error, 'installer_started', None) is False
                    and getattr(error, 'outcomeKnown', None) is True
                    and getattr(error, 'running', None) is False):
                raise PresencePreflightError('The native progress console could not be prepared; no installer was launched.') from error
            return {'installer_started': True, 'outcomeKnown': False, 'running': True,
                'uncertain': True, 'requiresReview': True, 'error': str(error)[:2000],
                'note': 'Application installer outcome is unknown; no automatic retry is allowed.'}
        try:
            _require(getattr(result, 'capture_complete', True) is True,
                     'The package-manager transcript was incomplete; its native outcome needs review.')
            text = _text(result)
            code = result.returncode & 0xffffffff
        except Exception as error:
            return {'installer_started': True, 'outcomeKnown': False, 'running': False,
                'uncertain': True, 'requiresReview': True, 'error': str(error)[:2000],
                'note': 'The launched package manager returned invalid completion metadata; automatic retry is blocked.'}
        restart = code in (3010, 1641, 0x8A150109, 0x8A15010A, 0x8A15010B) or any(message in text.casefold()
            for message in ('restart your pc to finish installation.', 'your pc will restart to finish installation.'))
        running = code in (1618, 0x8A150102)
        value = {'installer_started': True, 'outcomeKnown': not running, 'running': running,
                 'restartNeeded': restart, 'exitCode': result.returncode, 'verified': False,
                 'note': 'The exact quiet package-manager operation returned; its requested presence still requires readback.'}
        if running:
            value.update(uncertain=True, requiresReview=True,
                         note='Another installer is still running; no retry or later conflicting action was started.')
            return value
        if progress_callback is not None:
            try:
                progress_callback({'phase': 'verify', 'percent': None})
            except Exception:
                pass  # A display observer cannot change native lifecycle handling.
        try:
            self._source()
            after = self.installed(fresh['id'])
        except Exception as error:
            return dict(value, outcomeKnown=False, uncertain=True, requiresReview=True,
                        note='The application postcondition could not be verified; automatic retry is blocked.', error=str(error)[:2000])
        verified = (after is None if action == 'uninstall' else after is not None and
                    after['version'] == fresh['catalog']['version'])
        value.update(verified=verified and code in (0, 3010, 1641), after=after,
            note='Requested application presence confirmed by a fresh exact installed query.' if verified and code in (0, 3010, 1641)
                 else 'The requested application presence was not confirmed; review the package-manager result.')
        if progress_callback is not None and value['verified'] is True and not restart:
            try:
                progress_callback({'phase': action, 'percent': 100.0, 'item_terminal': True})
            except Exception:
                pass
        return value


def _known(note, **fields):
    return {'installer_started': False, 'outcomeKnown': True, 'running': False, 'note': note, **fields}


def _finish(items, **extra):
    outcomes = [row.get('result', {}) for row in items]
    known = all(value.get('outcomeKnown') is True and value.get('running') is False
                and value.get('uncertain') is not True for value in outcomes)
    return {'items': items, 'failed_items': sum(row.get('status') in ('failed', 'needs_review') for row in items),
        'navigation_cancelled_items': sum(value.get('navigation_skipped') is True for value in outcomes),
        'installer_started': any(value.get('installer_started') is True for value in outcomes),
        'outcomeKnown': known, 'running': any(value.get('running') is True for value in outcomes),
        'restartNeeded': any(value.get('restartNeeded') is True for value in outcomes),
        'review_only': not any(value.get('installer_started') is True for value in outcomes), **extra}


def run_app_presence(backend, settings, cancel_event=None, item_callback=None, before_install=None, *, navigation=None):
    """Apply saved exact presence rules; never acquire or release the caller's lane.

    Default/no-op configurations perform no host action. Native presence is
    explicitly unsupported. The same source/type/pin checks run immediately
    before launch and a fresh installed query verifies the requested result.
    Durable receipts share the existing owner-scoped installer barrier format.
    """
    from .software_app_rules import validate_app_rules
    if settings.get('software_updates_enabled') is not True:
        return _finish([], skipped=True, note='Software Updater is off; saved application presence rules are inactive and no query was started.')
    rules = validate_app_rules(settings.get('software_app_rules', []), native=getattr(backend, 'system', 'win32') != 'win32')
    rules = [rule for rule in rules if rule['presence'] != 'keep_current']
    if not rules:
        return _finish([], skipped=True, note='No application presence rule requested a change.')
    cancelled = lambda: cancel_event is not None and cancel_event.is_set()
    if cancelled():
        return _finish([], cancelled=True, skipped=True)
    if getattr(backend, 'system', sys.platform) != 'win32':
        return _finish([{'id': rule['id'], 'source': rule['source'], 'status': 'needs_review',
            'result': _known('Automatic install/uninstall presence is not supported by this native manager; existing packages were preserved.',
                             skipped=True, requiresReview=True, failure_scope='package', failureKind='unsupported_application')} for rule in rules])
    _require(type(settings.get('setup_version')) is int and settings['setup_version'] == CURRENT_SETUP_VERSION and
             settings.get('software_update_agreements_accepted') is True,
             'Complete current setup and approve package/source agreements before automatic application presence changes.')
    _require(getattr(getattr(backend, 'workflow', None), 'active', False) is True,
             'Application presence changes must run inside the owned maintenance workflow.')
    provider = WindowsAppPresence(backend)
    scope_keys = ('setup_version', 'software_updates_enabled', 'software_update_agreements_accepted', 'software_app_rules')
    approved_scope = copy.deepcopy({key: settings.get(key) for key in scope_keys})
    items = []
    gate_open = False
    journal = None
    shared_reason = ''
    def approvals_current():
        _require({key: settings.get(key) for key in scope_keys} == approved_scope,
                 'Saved application rules or approvals changed during this run.')
        loader = getattr(backend, 'preferences', None)
        if callable(loader):
            current = loader()
            _require(isinstance(current, dict) and {key: current.get(key) for key in scope_keys} == approved_scope,
                     'Saved application presence approval changed; review its current scope before continuing.')
    def save():
        if journal is not None:
            _ordinary_data_path(path.parent)
            _ordinary_data_path(path, file=True)
            _private_json(path, journal)
    for index, rule in enumerate(rules):
        if cancelled():
            break
        row = {'id': rule['id'], 'source': rule['source'], 'presence': rule['presence']}
        launched = False
        reporter = None
        if navigation is not None and navigation.begin_item('presence:' + rule['source'] + ':' + rule['id'],
                'Apply saved presence: ' + rule['id'], kind='mutation', retryable=False, phase='checking') is not True:
            break
        item_cancel = navigation.cancellation(cancel_event) if navigation is not None else cancel_event
        try:
            approvals_current()
            snapshot = provider.inspect(rule['id'], rule['source'], item_cancel, desired_presence=rule['presence'])
            row['before'] = copy.deepcopy(snapshot['installed'])
            action = 'install' if rule['presence'] == 'keep_installed' else 'uninstall'
            satisfied = (snapshot['installed'] is not None) if action == 'install' else snapshot['installed'] is None
            if satisfied:
                row.update(status='already_satisfied', result=_known('The saved application presence is already satisfied.'))
                items.append(row)
                continue
            row.update(catalog_version=snapshot['catalog']['version'],
                       catalog_fingerprint=snapshot['fingerprint'])
            provider.readiness(snapshot, action)
            _check_cancel(item_cancel)
            if not gate_open and before_install is not None:
                if before_install({'operation': 'software', 'candidate_count': len(rules) - index,
                                   'review_only': False, 'scan_complete': True}) is not True:
                    _check_cancel(item_cancel)
                    shared_reason = 'The exclusive installer gate did not permit application presence changes.'
                    row.update(status='needs_review', result=_known(shared_reason, skipped=True,
                               requiresReview=True, shared_barrier=True, failure_scope='shared'))
                    items.append(row)
                    break
                gate_open = True
            _check_cancel(item_cancel)
            if journal is None:
                identifier = uuid.uuid4().hex
                path = Path(backend.data_dir) / 'parallel-updates' / (identifier + '.json')
                journal = {'schema': 1, 'id': identifier, 'parent_run_id': identifier,
                    'owner': _current_owner(), 'title': _TITLE, 'status': 'prepared',
                    'installer_prepared': False, 'started_at': time.time(), 'items': items}
                save()
            if item_callback:
                progress_key = 'presence:' + rule['source'] + ':' + rule['id']
                progress_state = {'current': index, 'total': len(rules), 'item': index + 1, 'id': rule['id'],
                    'description': ('Installing missing approved app: ' if action == 'install' else 'Uninstalling approved app: ') + rule['id']}
                reporter = BoundItemProgress(item_callback, progress_key, rule['id'], progress_state)
                item_callback(dict(progress_state, item_progress=normalize_item_progress({
                    'key': progress_key, 'label': rule['id'], 'phase': action, 'percent': None})))
            _check_cancel(item_cancel)
            approvals_current()
            journal.update(status='running', installer_prepared=True,
                           current_item={'id': rule['id'], 'action': action, 'fingerprint': snapshot['fingerprint']})
            save()  # Durable intent before any package-manager mutation.
            launched = True
            outcome = provider.execute(snapshot, action, item_cancel, navigation=navigation,
                                       approval_callback=approvals_current, progress_callback=reporter)
            if navigation is not None and navigation.local_requested() and not cancelled() and outcome.get('cancelled') is True:
                outcome = dict(outcome, cancelled=False, navigation_skipped=True, skipped=True)
            row.update(status='completed' if outcome.get('verified') is True else 'cancelled' if outcome.get('cancelled') is True
                       else 'skipped' if outcome.get('navigation_skipped') is True else 'needs_review', result=outcome)
            items.append(row)
            journal['result'] = _finish(items)
            journal.update(status='uncertain' if journal['result']['outcomeKnown'] is not True else
                           'needs_review' if journal['result']['restartNeeded'] else 'completed', finished_at=time.time())
            save()
            from .navigation import shared_update_refusal
            shared_reason = shared_update_refusal(outcome) or ''
            # A returned, known failed postcondition is retained as needs_review
            # and does not prevent trying the next distinct approved rule. An
            # unknown installer, failed receipt or restart still closes the lane.
            if (shared_reason or outcome.get('outcomeKnown') is not True or outcome.get('running') is not False
                    or outcome.get('uncertain') is True or outcome.get('restartNeeded') is True
                    or outcome.get('exitCode') in (3010, 1641)):
                break
        except Exception as error:
            known = isinstance(error, PresencePreflightError) or not launched
            skipped = isinstance(error, PresenceCancelled)
            row.update(status='skipped' if skipped else 'needs_review', result=_known(str(error)[:2000], requiresReview=not skipped,
                skipped=True, navigation_skipped=skipped and navigation is not None and navigation.local_requested() and not cancelled(),
                cancelled=cancelled(), **({} if skipped else {
                    'failureKind': getattr(error, 'failureKind', 'preflight_unverified'),
                    'failure_scope': getattr(error, 'failure_scope', 'shared'),
                    'shared_barrier': getattr(error, 'shared_barrier', True)})) if known else
                {'installer_started': True, 'outcomeKnown': False, 'running': False, 'uncertain': True,
                 'requiresReview': True, 'error': str(error)[:2000], 'note': 'The application outcome receipt could not be established; automatic retry is blocked.'})
            if not any(item is row for item in items):
                items.append(row)
            if journal is not None:
                journal['result'] = _finish(items)
                journal.update(status='completed' if known else 'uncertain', finished_at=time.time())
                try: save()
                except Exception: pass  # The durable running intent remains a barrier.
            if not known:
                break
            from .navigation import shared_update_refusal
            shared_reason = shared_update_refusal(row['result']) or ''
            if shared_reason:
                break
        finally:
            if reporter is not None:
                reporter.close()
            if navigation is not None:
                outcome = row.get('result', {})
                navigation.finish(row.get('status', 'cancelled' if cancelled() else 'not_started'),
                    message=outcome.get('note'), mutation_started=outcome.get('installer_started') is True,
                    outcome_known=outcome.get('outcomeKnown', True) is True)
    result = _finish(items, cancelled=cancelled(), deferred_ids=[row['id'] for row in rules[len(items):]],
        note='Exact saved presence rules only. Reinstallation does not restore deleted application data. No system-wide reinstall blocker was created and no automatic restart was requested.')
    if shared_reason:
        result.update(shared_barrier=True, stop_reason=shared_reason)
    if journal is not None:
        result['worker_log_path'] = str(path)
    return result
