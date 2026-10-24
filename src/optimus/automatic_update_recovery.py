"""Close a proven native WinGet completion after an Optimus callback crash.

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


def validate_quiescence(probe, evidence, owner, *, now, current_console=None):
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
        if row['pid'] == evidence['native_pid']:
            _require(_utc_epoch(row.get('started_utc')) > evidence['finished_at'], 'The prior native updater may still be present.')
        servicing = name in ('trustedinstaller.exe', 'tiworker.exe')
        if servicing:
            windows = probe.get('windows_directory')
            path = row.get('path')
            _require(isinstance(windows, str) and ntpath.isabs(windows) and isinstance(path, str)
                and ntpath.normcase(path).startswith(ntpath.normcase(windows.rstrip('\\/') + '\\'))
                and _utc_epoch(row.get('started_utc')) > evidence['finished_at'],
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
        _require(isinstance(path, str) and ntpath.normcase(path) != ntpath.normcase(evidence['installer_path']),
            'The exact native installer is still present.')
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


def reconcile_completed_updates(backend, *, lease=None, folder=None, now=None):
    """Best-effort under a RunLease; preserve every unproven journal and hold."""
    result = {'schema': 1, 'status': 'completed', 'reconciled': [], 'unresolved': [],
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
        result['status'] = 'reconciled' if result['reconciled'] else 'unchanged'
        return result
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        return dict(result, status='unavailable', error=str(error)[:500])
    finally:
        if owned:
            lease.release()
