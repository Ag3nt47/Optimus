"""Reconcile proven WinGet completion or a captured interruption after reboot.

This is bookkeeping, never an updater/retry. Native logs, the exact prepared
intent, the original owner's exit, fresh installer quiescence and the installed
version must agree. Missing evidence leaves the original journal intact.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import sys
import time
import uuid

from .automation import RunLease
from .filesystem import _ordinary_data_path
from .speed_enhancements import _current_owner, _private_json
from .update_recovery import _read, _decode, finished_package_attempt

KIND = 'native-winget-completion-v1'
_ID = re.compile(r'^[a-f0-9]{32}$')
_PACKAGE = r'[A-Za-z0-9][A-Za-z0-9_.+\-]{1,127}'
_VERSION = r'[A-Za-z0-9][A-Za-z0-9_.+\-]{0,79}'
_STAMP = r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}'
RUNTIME_OWNER_KIND = 'optimus-runtime-owner-v1'
RESTART_RECOVERY_KIND = 'native-winget-console-quiescent-after-reboot-v1'
_RESTART_DIR = 'update-reconciliation-after-boot'
_CONSOLE_START = 'The console supervisor started but its target outcome is unknown; no retry was attempted.'
_CONSOLE_EXIT = ('The console supervisor exited without a verified target outcome; '
                 'its console remains retained and no automatic retry was attempted.')
_UPDATE_TIMEOUT = 'The update timed out. A vendor installer may still be running; verify its outcome before trying again.'
_BOOT_SKEW = 120
_BOOT_FENCE = 10
_JOURNAL_BYTES = 8 * 1024 * 1024
_HOLD_BYTES = 64 * 1024


def capture_runtime_owner(owner, journal_id, parent_run_id):
    """Persist the actual journal writer's native identity before any launch.

    This also covers update-only console runs, which deliberately have neither
    a GUI name nor a full resource audit. No argv or process census is stored.
    """
    import psutil
    process = psutil.Process(os.getpid())
    created = float(process.create_time())
    executable, name = process.exe(), process.name()
    captured = time.time()
    _require(isinstance(owner, str) and owner and _ID.fullmatch(journal_id)
        and _ID.fullmatch(parent_run_id), 'The runtime journal ownership is invalid.')
    _require(math.isfinite(created) and 0 < created <= captured
        and isinstance(executable, str) and executable and len(executable) <= 32767
        and isinstance(name, str) and name and len(name) <= 260,
        'The native journal writer identity could not be established.')
    return {'schema': 1, 'kind': RUNTIME_OWNER_KIND, 'owner': owner,
        'journal_id': journal_id, 'parent_run_id': parent_run_id, 'pid': process.pid,
        'created_at': created, 'created_utc': datetime.fromtimestamp(created, timezone.utc).isoformat(),
        'executable': executable, 'name': name, 'captured_at': captured,
        'frozen': getattr(sys, 'frozen', False) is True}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _epoch(stamp):
    # WinGet and Inno write native local wall time, including on Windows.
    return datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S.%f').astimezone().timestamp()


def _utc_epoch(value):
    _require(isinstance(value, str), 'A native process birth timestamp is unavailable.')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(parsed.tzinfo is not None, 'A native process birth timestamp has no timezone.')
    return parsed.timestamp()


def _intent(record):
    attempt = record.get('package_attempt')
    item = record.get('current_item')
    _require(record.get('title') == 'Review or update configured software'
        and record.get('status') == 'running' and record.get('installer_prepared') is True
        and isinstance(attempt, dict) and attempt.get('kind') == 'terminal-package-attempt-v1'
        and attempt.get('state') == 'prepared' and isinstance(attempt.get('id'), str)
        and _ID.fullmatch(attempt['id']) and isinstance(item, dict), 'No exact unfinished WinGet intent.')
    package = attempt.get('item_id')
    _require(isinstance(package, str) and re.fullmatch(_PACKAGE, package)
        and item.get('id') == package, 'Prepared package identities differ.')
    description = item.get('description')
    match = re.fullmatch(r'Updating approved package [1-9]\d{0,3}/[1-9]\d{0,3}: '
        + re.escape(package) + r' \((' + _VERSION + r') to (' + _VERSION + r')\)',
        description if isinstance(description, str) else '')
    _require(match is not None, 'The exact target version was not recorded.')
    prepared = attempt.get('prepared_at')
    _require(type(prepared) in (int, float) and math.isfinite(prepared)
        and 0 < prepared <= 32503680000, 'Invalid prepared timestamp.')
    history = record.get('terminal_items', [])
    _require(isinstance(history, list) and len(history) < 1000,
        'The earlier package receipt inventory is invalid or full.')
    _require(record.get('result') is None, 'A captured updater outcome must not be replaced.')
    return package, match[2], float(prepared)


def parse_completed_winget_log(raw, record, *, now):
    """Recognize one exact native command and its linked Inno terminal log."""
    package, version, prepared = _intent(record)
    text = raw.decode('utf-8-sig', errors='strict').replace('\r\n', '\n')
    starts = re.findall(r'^(' + _STAMP + r') <I> \[CORE\] Process: winget\.exe\[([1-9]\d*)\],', text, re.M)
    _require(len(starts) == 1, 'The native log process is ambiguous.')
    started, pid = _epoch(starts[0][0]), int(starts[0][1])
    _require(0 <= started - prepared <= 180 and started <= now, 'The native command is outside its prepared intent.')
    commands = re.findall(r"^" + _STAMP + r" <I> \[CLI \] WinGet invoked with arguments: (.+)$", text, re.M)
    _require(len(commands) == 1, 'The native command identity is ambiguous.')
    tokens = re.findall(r"'([^']*)'", commands[0])
    _require(' '.join("'" + token + "'" for token in tokens) == commands[0], 'Unexpected native argument syntax.')
    required = ['upgrade', '--id', package, '--exact', '--source', 'winget', '--version', version]
    _require(tokens[:len(required)] == required, 'The native command does not match the exact package version.')
    tail = tokens[len(required):]
    _require(len(tail) == len(set(tail)) and set(tail) <= {'--force', '--silent', '--disable-interactivity',
        '--accept-source-agreements', '--accept-package-agreements'}
        and {'--silent', '--disable-interactivity'} <= set(tail), 'Unexpected update options.')
    _require('[CLI ] Installer hash verified' in text and '[CLI ] Found one app. App id: ' + package + ' App name: ' in text
        and re.search(r'\[CLI \] Manifest fields: Name \[[^\r\n]+\], Version \[' + re.escape(version) + r'\]', text),
        'The exact native publisher package was not verified.')
    successes = re.findall(r'^(' + _STAMP + r') <I> \[CLI \] Leaf command succeeded: root:upgrade$', text, re.M)
    _require(len(successes) == 1 and not re.search(r'<E>|Leaf command failed|restart required|reboot required', text, re.I),
        'No unambiguous successful native command outcome.')
    finished = _epoch(successes[0])
    _require(started <= finished <= now and finished - started <= 86400, 'The native completion timestamp is invalid.')
    arp = re.findall(r"\[CLI \] The entry determined to be associated with the package is '([^'\r\n]+)', with publisher '([^'\r\n]+)'", text)
    _require(len(arp) == 1, 'The installed application association is ambiguous.')
    _require(version in arp[0][0], 'The associated application version is missing.')
    starts_installer = re.findall(r"\[CLI \] Starting: '([^'\r\n]+)' with arguments '([^\r\n]+)'", text)
    _require(len(starts_installer) == 1, 'The exact native installer is ambiguous.')
    installer, arguments = starts_installer[0]
    logs = re.findall(r'/LOG="([^"\r\n]+)"', arguments)
    _require(len(logs) == 1 and ntpath.basename(logs[0]).startswith(package + '.' + version + '-')
        and logs[0].lower().endswith('.log') and '/NORESTART' in arguments,
        'A bounded native no-restart installer log was not linked.')
    return {'package_id': package, 'version': version, 'prepared_at': prepared,
        'native_arguments': tokens,
        'attempt_id': record['package_attempt']['id'], 'native_pid': pid,
        'started_at': started, 'finished_at': finished, 'application_name': arp[0][0],
        'publisher': arp[0][1], 'installer_path': installer, 'installer_log': logs[0]}


def validate_inno_completion(raw, evidence):
    text = raw.decode('utf-8-sig', errors='strict').replace('\r\n', '\n')
    stamps = []
    for marker in ('Installation process succeeded.', 'Need to restart Windows? No', 'Log closed.'):
        found = re.findall(r'^(' + _STAMP + r')\s+' + re.escape(marker) + '$', text, re.M)
        _require(len(found) == 1, 'The native installer has no exact success/no-restart/close receipt.')
        stamps.append(_epoch(found[0]))
    _require(evidence['started_at'] <= stamps[0] <= stamps[1] <= stamps[2] <= evidence['finished_at']
        and not re.search(r'Need to restart Windows\? Yes|Installation process failed|Aborting', text, re.I),
        'The native installer receipt is incomplete or requests a restart.')


def _owner_process(parent):
    stages = parent.get('stages')
    _require(isinstance(stages, list) and len(stages) <= 2000, 'Missing parent audit.')
    audits = [row.get('result') for row in stages if isinstance(row, dict)
        and row.get('title') == 'Inspect current resource usage' and row.get('status') == 'completed']
    _require(len(audits) == 1 and isinstance(audits[0], dict), 'Missing exact parent process audit.')
    rows = audits[0].get('Processes')
    _require(isinstance(rows, list) and len(rows) <= 10000, 'The parent process audit is incomplete.')
    owners = [row for row in rows if isinstance(row, dict) and str(row.get('Name', '')).lower() == 'optimus.exe']
    _require(len(owners) == 1 and type(owners[0].get('ProcessId')) is int
        and owners[0]['ProcessId'] > 0 and isinstance(owners[0].get('StartTimeUtc'), str)
        and owners[0]['StartTimeUtc'] and isinstance(owners[0].get('Path'), str)
        and ntpath.basename(owners[0]['Path']).lower() == 'optimus.exe', 'The prior GUI identity is ambiguous.')
    return owners[0]


def _journal_runtime_owner(record):
    value = record.get('runtime_owner')
    _require(isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 1
        and value.get('kind') == RUNTIME_OWNER_KIND and value.get('owner') == record.get('owner')
        and value.get('owner') == _current_owner() and value.get('journal_id') == record.get('id')
        and value.get('parent_run_id') == record.get('parent_run_id'),
        'The journal runtime identity has an invalid ownership binding.')
    pid = value.get('pid')
    executable, name = value.get('executable'), value.get('name')
    _require(type(pid) is int and 0 < pid <= 0xffffffff and isinstance(executable, str)
        and 0 < len(executable) <= 32767 and ntpath.isabs(executable)
        and bool(ntpath.splitdrive(executable)[0])
        and executable.lower().endswith('.exe') and '\x00' not in executable
        and isinstance(name, str) and 0 < len(name) <= 260
        and ntpath.basename(executable).casefold() == name.casefold()
        and type(value.get('frozen')) is bool, 'The journal runtime executable identity is invalid.')
    created, captured = value.get('created_at'), value.get('captured_at')
    started = record.get('started_at')
    prepared = record.get('package_attempt', {}).get('prepared_at')
    _require(all(type(t) in (int, float) and math.isfinite(t) and 0 < t <= 32503680000
        for t in (created, captured, started, prepared))
        and created <= started <= captured <= prepared and captured - started <= 30
        and abs(_utc_epoch(value.get('created_utc')) - created) <= .001,
        'The journal runtime birth/capture does not match its prepared attempt.')
    return {'ProcessId': pid, 'StartTimeUtc': value['created_utc'], 'Path': executable,
        'Name': name, 'source': RUNTIME_OWNER_KIND}


def _recovery_owner(data_dir, record):
    parent_id = record.get('parent_run_id')
    _require(isinstance(parent_id, str) and _ID.fullmatch(parent_id), 'The parent workflow identity is unknown.')
    parent_path = Path(data_dir) / 'runs' / (parent_id + '.json')
    _ordinary_data_path(parent_path, file=True)
    parent_raw, parent = None, None
    if parent_path.exists():
        parent_raw = _read(parent_path, 8 * 1024 * 1024)
        parent = _decode(parent_raw)
        _require(parent.get('id') == parent_id, 'The parent workflow identity differs.')
    if 'runtime_owner' in record:
        # Invalid new evidence cannot silently downgrade to a legacy GUI audit.
        original_owner = _journal_runtime_owner(record)
    else:
        _require(parent is not None, 'The legacy parent process audit is unavailable.')
        original_owner = _owner_process(parent)
    return original_owner, parent_raw


def _current_console_identity():
    """Authenticate only this frozen update command, never another helper PID.

    Called after acquiring the maintenance lease. The native census must still
    independently agree with PID, executable path and creation time below.
    """
    if getattr(sys, 'frozen', False) is not True or ntpath.basename(sys.executable).casefold() != 'optimusconsole.exe':
        return None
    import psutil
    process = psutil.Process(os.getpid())
    executable = process.exe()
    _require(ntpath.normcase(executable) == ntpath.normcase(sys.executable)
        and process.name().casefold() == 'optimusconsole.exe', 'The current console identity is unavailable.')
    return {'pid': os.getpid(), 'path': executable, 'created_at': process.create_time(),
            'owner': _current_owner()}


def _validate_idle_probe(probe, owner, *, finished, now, current_console=None,
                         native_pid=None, installer_path=None):
    _require(isinstance(probe, dict) and type(probe.get('schema')) is int and probe.get('schema') == 1 and probe.get('complete') is True
        and probe.get('review_only') is True and probe.get('wua_busy') is False
        and probe.get('reboot_required') is False and probe.get('owner') == _current_owner(),
        'Native installer/restart state is unavailable or busy.')
    captured = probe.get('captured_at')
    _require(type(captured) in (float, int) and 0 <= now - captured <= 30, 'The native census is stale.')
    rows = probe.get('processes')
    _require(isinstance(rows, list) and 5 <= len(rows) <= 10000, 'The native process census is incomplete.')
    seen = set()
    for row in rows:
        _require(isinstance(row, dict) and type(row.get('pid')) is int and row['pid'] >= 0
            and isinstance(row.get('name'), str) and 0 < len(row['name']) <= 260, 'The native process census is malformed.')
        _require(row['pid'] not in seen, 'The native process census repeats a PID.')
        seen.add(row['pid'])
        name = row['name'].lower()
        if row['pid'] == owner['ProcessId']:
            _require(_utc_epoch(row.get('started_utc')) > _utc_epoch(owner['StartTimeUtc']) + 0.01,
                'The prior GUI identity may still be present.')
        if row['pid'] == native_pid:
            _require(_utc_epoch(row.get('started_utc')) > finished, 'The prior native updater may still be present.')
        servicing = name in ('trustedinstaller.exe', 'tiworker.exe')
        if servicing:
            windows = probe.get('windows_directory')
            path = row.get('path')
            _require(isinstance(windows, str) and ntpath.isabs(windows) and isinstance(path, str)
                and ntpath.normcase(path).startswith(ntpath.normcase(windows.rstrip('\\/') + '\\'))
                and _utc_epoch(row.get('started_utc')) > finished,
                'Windows servicing overlaps the prior installer or its identity is unavailable.')
        authenticated_self = (name == 'optimusconsole.exe' and isinstance(current_console, dict)
            and type(current_console.get('pid')) is int and row['pid'] == current_console['pid']
            and current_console.get('owner') == probe['owner']
            and isinstance(row.get('path'), str) and isinstance(current_console.get('path'), str)
            and ntpath.normcase(row['path']) == ntpath.normcase(current_console['path'])
            and type(current_console.get('created_at')) in (int, float)
            and abs(_utc_epoch(row.get('started_utc')) - current_console['created_at']) <= .01)
        _require((name not in ('winget.exe', 'msiexec.exe', 'optimusconsole.exe') or authenticated_self)
            and (servicing or not re.search(r'setup|installer|^wusa\.|^dism\.', name)), 'A native installer may still be running.')
        path = row.get('path') or ''
        _require(isinstance(path, str) and (installer_path is None
            or ntpath.normcase(path) != ntpath.normcase(installer_path)), 'The exact native installer is still present.')


def validate_quiescence(probe, evidence, owner, *, now, current_console=None):
    _validate_idle_probe(probe, owner, finished=evidence['finished_at'], now=now, current_console=current_console,
                         native_pid=evidence['native_pid'], installer_path=evidence['installer_path'])
    applications = probe.get('applications')
    _require(isinstance(applications, list) and len(applications) <= 10000, 'The installed application census is incomplete.')
    matches = [row for row in applications if isinstance(row, dict)
        and row.get('name') == evidence['application_name'] and row.get('publisher') == evidence['publisher']]
    _require(len(matches) == 1 and matches[0].get('version') == evidence['version'], 'The current installed version does not match native completion.')


def _exclusive(path, raw):
    _ordinary_data_path(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ordinary_data_path(path, file=True)
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _log_folder():
    from .retention_cleanup import _known_folder
    return _known_folder('F1B32785-6FBA-4FCF-9D55-7B8E7F157091') / 'Packages/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe/LocalState/DiagOutputDir'


def _completed_evidence(folder, record, *, now):
    _, _, prepared = _intent(record)
    _ordinary_data_path(folder)
    candidates, entries = [], 0
    for path in folder.iterdir():
        entries += 1
        _require(entries <= 4096, 'The native log inventory exceeds its bound.')
        match = re.fullmatch(r'WinGet-(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.\d{3})\.log', path.name)
        if not match:
            continue
        stamp = datetime.strptime(match[1], '%Y-%m-%d-%H-%M-%S.%f').astimezone().timestamp()
        if not 0 <= stamp - prepared <= 180:
            continue
        _require(len(candidates) < 16, 'The native command inventory is ambiguous.')
        candidates.append(path)
    proven = []
    for path in candidates:
        raw = _read(path, 2 * 1024 * 1024)
        try:
            evidence = parse_completed_winget_log(raw, record, now=now)
        except ValueError:
            continue
        linked = Path(evidence['installer_log'])
        _require(ntpath.normcase(str(linked.parent)) == ntpath.normcase(str(folder)), 'The linked installer log is outside native authority.')
        inno = _read(linked, 4 * 1024 * 1024)
        validate_inno_completion(inno, evidence)
        evidence['winget_log'] = str(path)
        evidence['winget_log_sha256'] = hashlib.sha256(raw).hexdigest()
        evidence['installer_log_sha256'] = hashlib.sha256(inno).hexdigest()
        proven.append(evidence)
    _require(len(proven) == 1, 'No unique completed native installer matched the exact intent.')
    return proven[0]


def _epoch_number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= 32503680000


def _captured_console_attempt(record):
    """Bind the single captured unknown to its exact approved WinGet offer."""
    from .parallel_updates import installer_barrier
    from .update_holds import _PACKAGE as HOLD_ID, _VERSION as HOLD_VERSION

    _require(isinstance(record, dict) and type(record.get('schema')) is int and record['schema'] == 1
             and record.get('title') == 'Review or update configured software'
             and record.get('status') == 'uncertain' and record.get('installer_prepared') is True
             and isinstance(record.get('id'), str) and _ID.fullmatch(record['id'])
             and isinstance(record.get('parent_run_id'), str) and _ID.fullmatch(record['parent_run_id']),
             'The journal is not a captured application update outcome.')
    attempt, item, batch = (record.get(key) for key in ('package_attempt', 'current_item', 'result'))
    _require(isinstance(attempt, dict) and attempt.get('kind') == 'terminal-package-attempt-v1'
             and attempt.get('state') == 'prepared'
             and isinstance(attempt.get('id'), str) and _ID.fullmatch(attempt['id'])
             and isinstance(item, dict) and isinstance(batch, dict), 'The exact prepared console attempt is unavailable.')
    package = attempt.get('item_id')
    _require(isinstance(package, str) and HOLD_ID.fullmatch(package) and not package.endswith('.')
             and '..' not in package and item.get('id') == package, 'The exact captured package identity is unavailable.')
    description = item.get('description')
    match = re.fullmatch(r'Updating approved package ([1-9]\d{0,3})/([1-9]\d{0,3}): '
                         + re.escape(package) + r' \(([^() ]+) to ([^() ]+)\)',
                         description if isinstance(description, str) and len(description) <= 512 else '')
    _require(match is not None and 1 <= int(match[1]) <= int(match[2]) <= 1000
             and all(HOLD_VERSION.fullmatch(version) and not version.endswith('.') and '..' not in version
                     for version in match.group(3, 4)), 'The exact captured offer version is unavailable.')
    from .automated_software import is_newer_version
    _require(is_newer_version(match[3], match[4], package_id=package, source='winget'),
             'The captured offer is not an exact newer release.')
    started, prepared, finished = record.get('started_at'), attempt.get('prepared_at'), record.get('finished_at')
    _require(all(map(_epoch_number, (started, prepared, finished))) and started <= prepared <= finished
             and finished - started <= 86400, 'The captured console timeline is invalid.')
    owner = _journal_runtime_owner(record)  # New/captured records must have exact original ownership.
    history, rows = record.get('terminal_items'), batch.get('items')
    _require(isinstance(history, list) and 1 <= len(history) <= 1000 and history == rows
             and isinstance(history[-1], dict), 'Captured and terminal item histories disagree.')
    last = history[-1]
    captured = last.get('result')
    _require(set(last) == {'id', 'binding', 'status', 'uncertain', 'presence', 'source', 'error', 'reason', 'result'}
             and last['id'] == package and last['status'] == 'failed' and last['uncertain'] is False
             and last['presence'] == last['reason'] == '' and last['source'] in ('', 'winget')
             and last['binding'] == {} and isinstance(captured, dict),
             'The terminal item is not this WinGet attempt.')
    note, failure = captured.get('note'), captured.get('failureKind')
    _require(note == last.get('error') and
             ((failure == 'console_outcome_unknown' and note in (_CONSOLE_START, _CONSOLE_EXIT))
              or (failure == 'preflight_failed' and note in (_CONSOLE_EXIT, _UPDATE_TIMEOUT))
              or (failure == 'update_timeout' and note == _UPDATE_TIMEOUT)),
             'The captured failure is not a recognized updater interruption.')
    expected = {'installer_started': True, 'outcomeKnown': False, 'running': True, 'uncertain': True,
                'requiresReview': True, 'status': 'failed', 'failure_scope': 'app_batch'}
    _require(set(captured) == set(expected) | {'note', 'failureKind'}
             and all(captured.get(key) is value if type(value) is bool else captured.get(key) == value
                     for key, value in expected.items()),
             'The captured console lifecycle or restart metadata is inconsistent.')
    for previous in history[:-1]:
        value = previous.get('result') if isinstance(previous, dict) else None
        _require(isinstance(value, dict) and value.get('outcomeKnown') is True
                 and value.get('running') is False and type(value.get('installer_started')) is bool
                 and installer_barrier(previous) is None, 'Another earlier installer remains unresolved.')
    _require(batch.get('installer_started') is True and batch.get('outcomeKnown') is False
             and batch.get('running') is True and batch.get('uncertain', True) is True
             and batch.get('stop_reason') == 'The updater raised an error; verify installer state before continuing.'
             and all(batch.get(key, False) is False for key in ('restartNeeded', 'reboot_required',
                 'rebootRequired', 'cancelled', 'shared_barrier', 'blocked_by_prior_installer', 'installation_blocked'))
             and type(batch.get('failed_items')) is int and batch['failed_items'] >= 1,
             'The batch has other unknown or restart evidence.')
    # The captured item is the only unknown evidence. Remove precisely its
    # enclosing markers, then apply the existing shared barrier to remaining metadata.
    remaining = {key: value for key, value in batch.items()
                 if key not in ('items', 'running', 'uncertain', 'outcomeKnown', 'stop_reason', 'failed_items')}
    _require(installer_barrier(remaining) is None, 'Another captured installer or shared safety barrier remains unresolved.')
    return {'package_id': package, 'version': match[4], 'attempt_id': attempt['id'],
            'prepared_at': prepared, 'finished_at': finished}, owner


def is_restart_recovery_candidate(record):
    try:
        _captured_console_attempt(record)
        return True
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, RecursionError):
        return False


def _prior_boot(data_dir, record, original_owner):
    path = data_dir / 'runs' / (record['parent_run_id'] + '.json')
    raw = _read(path, _JOURNAL_BYTES)
    parent = _decode(raw)
    started, executed = parent.get('started_at'), record.get('execution_started_at')
    stages = parent.get('stages')
    _require(parent.get('id') == record['parent_run_id'] and _epoch_number(started) and _epoch_number(executed)
             and started <= record['started_at'] <= executed <= record['package_attempt']['prepared_at']
             and isinstance(stages, list) and len(stages) <= 2000,
             'The earlier workflow cannot be bound to this captured installer.')
    audits = [row for row in stages if isinstance(row, dict) and row.get('title') == 'Inspect current resource usage']
    updates = [row for row in stages if isinstance(row, dict) and row.get('title') == record['title']]
    _require(len(audits) == len(updates) == 1 and audits[0].get('status') == 'completed'
             and updates[0].get('started_at') == executed and updates[0].get('finished_at') == record['finished_at'],
             'The earlier native boot audit or exact application stage is unavailable.')
    audit = audits[0]
    captured = audit.get('result')
    system = captured.get('System') if isinstance(captured, dict) else None
    _require(isinstance(system, dict) and _epoch_number(audit.get('started_at')) and _epoch_number(audit.get('finished_at'))
             and started <= audit['started_at'] <= audit['finished_at'] <= record['package_attempt']['prepared_at'],
             'The earlier native boot audit does not precede the captured installer.')
    previous = system.get('LastBootUtc')
    prior_epoch = _utc_epoch(previous)
    _require(_epoch_number(prior_epoch) and prior_epoch <= _utc_epoch(original_owner['StartTimeUtc']),
             'The earlier Windows boot does not match the original updater process.')
    return {'parent_sha256': hashlib.sha256(raw).hexdigest(), 'previous_boot_utc': previous}


def _boot_evidence(probe, finish, *, observed_boot, previous_boot):
    _require(isinstance(probe, dict), 'Native boot provenance is unavailable.')
    boot = _utc_epoch(probe.get('boot_utc'))
    event = _utc_epoch(probe.get('boot_event_utc'))
    observed = (boot, event, observed_boot)
    _require(all(map(_epoch_number, observed)) and _epoch_number(probe.get('captured_at'))
             and max(observed) - min(observed) <= _BOOT_SKEW
             and max(boot, event) <= probe['captured_at'] and observed_boot <= probe['captured_at'] + 1,
             'Native OS boot time, startup event and uptime did not agree.')
    _require(min(observed) > max(finish, _utc_epoch(previous_boot)) + _BOOT_FENCE,
             'A Windows restart after this captured installer outcome is not verified.')
    return {'boot_utc': probe['boot_utc'], 'boot_event_utc': probe['boot_event_utc'],
            'boot_identity': observed_boot, 'captured_at': probe['captured_at']}


def _hold_for_console_attempt(data_dir, package, version, owner):
    from .update_holds import load_update_holds
    return next((row for row in load_update_holds(data_dir, owner=owner)
                 if row['id'].casefold() == package.casefold() and row['available_version'] == version), None)


def _append_console_hold(data_dir, package, version, owner):
    from .update_holds import load_update_holds
    path = data_dir / 'update-holds.json'
    before = _ordinary_data_path(path, file=True)
    original = _read(path, _HOLD_BYTES) if before is not None else None
    holds = load_update_holds(data_dir, owner=owner)
    if any(row['id'].casefold() == package.casefold() and row['available_version'] == version for row in holds):
        return
    _require(len(holds) < 100, 'The saved automatic update hold list is full.')
    holds.append({'id': package, 'available_version': version,
                  'reason': 'Windows restarted after this exact offer had an unverified WinGet console outcome. Review the offer before retrying.'})
    proposed = {'schema': 1, 'owner': owner, 'holds': holds}
    _require(len(json.dumps(proposed, allow_nan=False).encode('utf-8')) <= _HOLD_BYTES,
             'The saved automatic update hold file would exceed its size limit.')
    unchanged = (_ordinary_data_path(path, file=True) is None if original is None else _read(path, _HOLD_BYTES) == original)
    _require(unchanged, 'The saved update holds changed during recovery.')
    _private_json(path, proposed)
    _require(_hold_for_console_attempt(data_dir, package, version, owner) is not None,
             'The exact held offer could not be verified after saving.')


def _restart_marker(data_dir, journal_id):
    return data_dir / _RESTART_DIR / (journal_id + '.json')


def reconciled_console_outcome(data_dir, path, record, *, owner):
    """Read only the exact committed release of an old liveness barrier."""
    from .update_resume import boot_identity
    data_dir, path = Path(data_dir), Path(path)
    try:
        _require(isinstance(record, dict) and record.get('owner') == owner == _current_owner()
                 and _ID.fullmatch(path.stem) and record.get('id') == path.stem
                 and path == data_dir / 'parallel-updates' / (path.stem + '.json'),
                 'The journal ownership or path does not match.')
        marker = _restart_marker(data_dir, path.stem)
        if _ordinary_data_path(marker, file=True) is None:
            return False
        receipt = _decode(_read(marker, _HOLD_BYTES))
        raw = _read(path, _JOURNAL_BYTES)
        _require(_decode(raw) == record, 'The journal changed during validation.')
        attempt, original_owner = _captured_console_attempt(record)
        prior = _prior_boot(data_dir, record, original_owner)
        expected = {'schema': 1, 'kind': RESTART_RECOVERY_KIND, 'owner': owner,
                    'journal_id': path.stem, 'original_sha256': hashlib.sha256(raw).hexdigest(),
                    'prior_boot': prior, 'attempt': attempt, 'historical_outcome_known': False,
                    'installers_started': 0, 'retries_started': 0}
        _require(set(receipt) == set(expected) | {'proof', 'recorded_at'}
                 and all(type(receipt[key]) is type(value) and receipt[key] == value for key, value in expected.items()),
                 'The committed reboot receipt does not match the original journal.')
        proof, recorded = receipt['proof'], receipt['recorded_at']
        _require(isinstance(proof, dict) and proof.get('schema') == 1 and proof.get('owner') == owner
                 and proof.get('complete') is True and proof.get('review_only') is True
                 and proof.get('wua_busy') is False and proof.get('reboot_required') is False
                 and type(proof.get('process_count')) is int and 5 <= proof['process_count'] <= 10000
                 and _epoch_number(recorded) and _epoch_number(proof.get('captured_at'))
                 and 0 <= recorded - proof['captured_at'] <= 30 and recorded <= time.time(),
                 'The committed native idle proof is invalid.')
        _boot_evidence(proof, attempt['finished_at'], observed_boot=proof.get('boot_identity'),
                       previous_boot=prior['previous_boot_utc'])
        current_boot = boot_identity()
        _require(_epoch_number(current_boot) and current_boot >= proof['boot_identity'] - _BOOT_SKEW
                 and current_boot > attempt['finished_at'] + _BOOT_FENCE,
                 'Current Windows boot no longer validates the committed receipt.')
        return _hold_for_console_attempt(data_dir, attempt['package_id'], attempt['version'], owner) is not None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        return False


def _reconcile_console_after_restart(backend, path, raw, record, *, owner, now, current_console):
    from .update_resume import boot_identity
    data_dir = Path(backend.data_dir)
    attempt, original_owner = _captured_console_attempt(record)
    marker = _restart_marker(data_dir, path.stem)
    if _ordinary_data_path(marker, file=True) is not None:
        _require(reconciled_console_outcome(data_dir, path, record, owner=owner),
                 'The saved reboot recovery receipt or its exact held offer needs review.')
        return None  # A committed receipt is already active.
    prior = _prior_boot(data_dir, record, original_owner)
    native = backend.automatic_update_recovery_probe()
    recorded = time.time() if now is None else now
    _validate_idle_probe(native, original_owner, finished=attempt['finished_at'], now=recorded,
                         current_console=current_console)
    proof = _boot_evidence(native, attempt['finished_at'], observed_boot=boot_identity(),
                           previous_boot=prior['previous_boot_utc'])
    proof.update({key: native[key] for key in ('schema', 'owner', 'complete', 'review_only', 'wua_busy', 'reboot_required')})
    proof['process_count'] = len(native['processes'])
    _require(_read(path, _JOURNAL_BYTES) == raw, 'The updater journal changed before reboot reconciliation.')
    _append_console_hold(data_dir, attempt['package_id'], attempt['version'], owner)
    _require(_read(path, _JOURNAL_BYTES) == raw and _ordinary_data_path(marker, file=True) is None,
             'The updater journal or reboot receipt changed before committing.')
    # Commit atomically under the caller's RunLease. If a crash precedes this
    # write the original remains blocked and the exact hold is idempotent.
    receipt = {'schema': 1, 'kind': RESTART_RECOVERY_KIND, 'owner': owner, 'journal_id': path.stem,
               'original_sha256': hashlib.sha256(raw).hexdigest(), 'prior_boot': prior, 'attempt': attempt,
               'historical_outcome_known': False, 'recorded_at': recorded, 'proof': proof,
               'installers_started': 0, 'retries_started': 0}
    _private_json(marker, receipt)
    _require(reconciled_console_outcome(data_dir, path, record, owner=owner), 'The reboot receipt readback failed.')
    return {'id': path.stem, 'package_id': attempt['package_id'], 'version': attempt['version']}


def reconcile_completed_updates(backend, *, lease=None, folder=None, now=None):
    """Best-effort under a RunLease; preserve every unproven journal and hold."""
    result = {'schema': 1, 'status': 'completed', 'reconciled': [], 'reconciled_after_reboot': [], 'unresolved': [],
        'installers_started': 0, 'retries_started': 0, 'records_deleted': 0}
    if getattr(backend, 'system', os.sys.platform) != 'win32':
        return dict(result, status='unsupported')
    owned = lease is None
    if owned:
        try:
            lease = RunLease(backend.data_dir).acquire()
        except RuntimeError:
            return dict(result, status='busy')
    try:
        _require(isinstance(lease, RunLease) and lease.handle is not None
            and lease.path == Path(backend.data_dir) / 'maintenance-run.lock', 'A current maintenance lease is required.')
        current_console = _current_console_identity()
        directory = Path(backend.data_dir) / 'parallel-updates'
        _ordinary_data_path(directory)
        if not directory.exists():
            return result
        paths = list(directory.glob('*.json'))
        _require(len(paths) <= 2000, 'The updater journal inventory exceeds its bound.')
        owner = _current_owner()
        for path in paths:
            try:
                original = _read(path, 8 * 1024 * 1024)
                record = _decode(original)
                _require(type(record.get('schema')) is int and record.get('schema') == 1 and record.get('owner') == owner
                    and record.get('id') == path.stem and _ID.fullmatch(path.stem), 'The updater journal ownership is unknown.')
                if record.get('status') not in ('prepared', 'running', 'uncertain') or record.get('installer_prepared') is False:
                    continue
                if finished_package_attempt(record):
                    continue
                if record.get('status') == 'uncertain':
                    recovered = _reconcile_console_after_restart(backend, path, original, record, owner=owner,
                                                                now=now, current_console=current_console)
                    if recovered is not None:
                        result['reconciled_after_reboot'].append(recovered)
                    continue
                _intent(record)
                current = time.time() if now is None else now
                evidence = _completed_evidence(Path(folder) if folder is not None else _log_folder(), record, now=current)
                original_owner, parent_raw = _recovery_owner(backend.data_dir, record)
                probe = backend.automatic_update_recovery_probe()
                current = time.time() if now is None else now
                validate_quiescence(probe, evidence, original_owner, now=current, current_console=current_console)
                _require(_read(path, 8 * 1024 * 1024) == original, 'The updater journal changed before reconciliation.')
                audit_dir = Path(backend.data_dir) / 'update-reconciliation' / (
                    path.stem + '-' + evidence['attempt_id'] + '-' + uuid.uuid4().hex)
                audit = {'schema': 1, 'kind': KIND, 'owner': owner, 'journal_id': path.stem,
                    'original_sha256': hashlib.sha256(original).hexdigest(),
                    'parent_sha256': hashlib.sha256(parent_raw).hexdigest() if parent_raw is not None else None,
                    'runtime_owner_source': original_owner.get('source', 'legacy-parent-process-audit'), 'evidence': evidence,
                    'quiescence': probe, 'recorded_at': current, 'installers_started': 0, 'retries_started': 0}
                row = {'id': evidence['package_id'], 'status': 'completed', 'uncertain': False,
                    'result': {'exitCode': 0, 'outcomeKnown': True, 'running': False,
                        'installer_started': True, 'restartNeeded': False, 'verified': True,
                        'note': 'Exact native success and current installed version automatically reconciled; no installer was retried.'}}
                updated = copy.deepcopy(record)
                updated['terminal_items'] = list(updated.get('terminal_items', [])) + [row]
                updated['package_attempt'].update(state='finished', terminal_index=len(updated['terminal_items']) - 1,
                    finished_at=evidence['finished_at'], terminal_sha256=hashlib.sha256(json.dumps(row,
                        sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest())
                # Keep status=running: only this exact receipt clears the old barrier.
                # The unfinished batch and parent report are never rewritten as success.
                updated['automatic_reconciliation'] = {'kind': KIND, 'evidence_path': str(audit_dir / 'evidence.json'),
                    'recorded_at': current, 'original_sha256': audit['original_sha256']}
                _require(finished_package_attempt(updated), 'The proposed exact receipt is invalid.')
                # Validate all prior receipts before producing any new audit.
                # Both exact original and proof are durable before changing the barrier.
                _exclusive(audit_dir / 'original.json', original)
                _exclusive(audit_dir / 'evidence.json', json.dumps(audit, allow_nan=False, sort_keys=True).encode('utf-8'))
                _require(_read(path, 8 * 1024 * 1024) == original, 'The updater journal changed after audit preparation.')
                _private_json(path, updated)
                _require(finished_package_attempt(_decode(_read(path, 8 * 1024 * 1024))), 'The terminal receipt readback failed.')
                result['reconciled'].append({'id': path.stem, 'package_id': evidence['package_id'], 'version': evidence['version']})
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
                result['unresolved'].append({'id': path.stem, 'reason': str(error)[:500]})
        result['status'] = ('reconciled' if result['reconciled'] or result['reconciled_after_reboot']
                            else 'unchanged')
        return result
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        return dict(result, status='unavailable', error=str(error)[:500])
    finally:
        if owned:
            lease.release()
