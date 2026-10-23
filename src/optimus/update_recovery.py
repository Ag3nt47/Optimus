"""Reconcile one proven 0.8.0 pre-launch cache refusal, never an installer.

Missing historical launch arguments are not evidence of a frozen runtime. That
case additionally requires a fresh, exact-record operator review with hashed
native logs and quiescence evidence. This module has no update/process actions.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import stat
import time

from .automation import RunLease
from .filesystem import _ordinary_data_path
from .speed_enhancements import _current_owner, _private_json

LEGACY_VERSION = '0.8.0'
LEGACY_CACHE_GUARD = 'Select an unambiguous winget package from a fresh update scan.'
LEGACY_SOURCE_SHA256 = 'f777c168fc8b22dc0e45d77973ac51f8655f0a6e3ce5c88ef0f3d2676ce76d7b'
REVIEW_KIND = 'operator-reviewed-legacy-cache-guard'
REVIEW_RUNTIME = 'not_recorded; operator reviewed source, native logs, quiescence'
REVIEW_CONCLUSION = 'failed_attempt_did_not_start_installer'
RECOVERY_KIND = 'legacy-0.8.0-cache-guard-v1'
REVIEW_MAX_AGE_SECONDS = 600
_TITLE = 'Review or update configured software'
_STOP = 'The updater raised an error; verify installer state before continuing.'
_ID = re.compile(r'^[a-f0-9]{32}$')
_SHA = re.compile(r'^[a-f0-9]{64}$')
_PACKAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+\-]{1,127}$')
_MAX_PARENT = 8 * 1024 * 1024
_MAX_JOURNAL = 256 * 1024
_MAX_SUPPORT = 4 * 1024 * 1024


def finished_package_attempt(record):
    """Recognize only a receipt closing this journal's latest exact intent.

    This is a read-only crash-gap check, not an inference from age, process
    absence, a batch status or a successful search. Legacy journals without a
    token remain unresolved. A subsequent package boundary replaces the token
    before its native call and therefore invalidates every earlier closure.
    """
    if not isinstance(record, dict) or type(record.get('schema')) is not int or record['schema'] != 1:
        return False
    attempt = record.get('package_attempt')
    history = record.get('terminal_items')
    if (not isinstance(attempt, dict) or attempt.get('kind') != 'terminal-package-attempt-v1'
            or not _identity(attempt.get('id')) or attempt.get('state') != 'finished'
            or not isinstance(attempt.get('item_id'), str) or not attempt['item_id']
            or len(attempt['item_id']) > 280 or not isinstance(history, list)
            or not 1 <= len(history) <= 1000 or type(attempt.get('terminal_index')) is not int
            or attempt['terminal_index'] != len(history) - 1
            or not isinstance(attempt.get('terminal_sha256'), str)
            or not _SHA.fullmatch(attempt['terminal_sha256'])):
        return False
    for key in ('prepared_at', 'finished_at'):
        value = attempt.get(key)
        if type(value) not in (int, float) or not 0 < value <= 32503680000 or not math.isfinite(value):
            return False
    if attempt['finished_at'] < attempt['prepared_at']:
        return False
    row = history[-1]
    if (not isinstance(row, dict) or row.get('id') != attempt['item_id']
            or row.get('status') not in ('completed', 'applied', 'failed', 'skipped', 'needs_review')):
        return False
    try:
        digest = _digest(json.dumps(row, sort_keys=True, ensure_ascii=True, allow_nan=False,
                                    separators=(',', ':')).encode('utf-8'))
    except (TypeError, ValueError, RecursionError):
        return False
    if digest != attempt['terminal_sha256']:
        return False
    # Every earlier receipt must also be explicit. An unrelated terminal item
    # cannot wash out a previously recorded unknown installer.
    for item in history:
        if not isinstance(item, dict):
            return False
        result = item.get('result')
        if (not isinstance(result, dict) or result.get('outcomeKnown') is not True
                or result.get('running') is not False or type(result.get('installer_started')) is not bool):
            return False
        code = result.get('exitCode')
        if (type(code) is int and (code & 0xffffffff) in
                (3010, 1641, 1618, 0x8A150109, 0x8A15010A, 0x8A15010B, 0x8A150102)):
            return False
        stack = [item]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key in ('restartNeeded', 'reboot_required', 'rebootRequired'):
                    if key not in value or value[key] is False:
                        continue
                    # Maintenance's captured WinGet result uses None for its
                    # explicitly checked no-restart case. Preserve that real
                    # shape only with a strict terminal non-restart exit.
                    code = result.get('exitCode')
                    winget_clear = (key == 'restartNeeded' and value[key] is None
                        and record.get('title') == _TITLE and type(code) is int
                        and (code & 0xffffffff) not in (3010, 1641, 1618, 0x8A150109, 0x8A15010A, 0x8A15010B, 0x8A150102))
                    if not winget_clear:
                        return False
                if (value.get('running') is True or value.get('outcomeKnown') is False
                        or value.get('uncertain') is True or value.get('restartNeeded') is True
                        or value.get('reboot_required') is True or value.get('rebootRequired') is True
                        or value.get('exitCode') in (3010, 1641)):
                    return False
                stack.extend(part for part in value.values() if isinstance(part, (dict, list)))
            elif isinstance(value, list):
                stack.extend(value)
    return True


class _ReviewRequired(ValueError):
    def __init__(self, context):
        super().__init__('Historical launch arguments were not recorded; an exact operator review is required.')
        self.context = context


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _timestamp(value):
    _require(type(value) in (int, float) and math.isfinite(value) and value > 0, 'Invalid historical timestamp.')
    return float(value)


def _pairs(values):
    result = {}
    for key, value in values:
        _require(key not in result, 'Duplicate JSON property.')
        result[key] = value
    return result


def _decode(raw):
    def invalid(value):
        raise ValueError('Non-finite JSON value: ' + value)
    value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs, parse_constant=invalid)
    stack, nodes = [(value, 0)], 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        _require(nodes <= 500000 and depth <= 64, 'JSON structure exceeds its bounds.')
        if isinstance(current, dict):
            stack.extend((part, depth + 1) for part in current.values())
        elif isinstance(current, list):
            stack.extend((part, depth + 1) for part in current)
        elif isinstance(current, str):
            _require(len(current) <= 2 * 1024 * 1024, 'JSON string exceeds its bound.')
    _require(isinstance(value, dict), 'A JSON object is required.')
    return value


def _read(path, maximum):
    path = Path(path)
    _require(path.is_absolute(), 'Recovery evidence requires absolute paths.')
    before = _ordinary_data_path(path, file=True)
    _require(before is not None and before.st_size <= maximum, 'Evidence is missing or exceeds its byte limit.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        _require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                 and (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), 'Evidence identity changed.')
        raw = stream.read(maximum + 1)
    after = _ordinary_data_path(path, file=True)
    _require(after is not None and len(raw) <= maximum
             and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
             == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), 'Evidence changed during reading.')
    return raw


def _same_path(left, right):
    if not isinstance(left, (str, Path)) or not isinstance(right, (str, Path)):
        return False
    if ntpath.splitdrive(str(left))[0] or ntpath.splitdrive(str(right))[0]:
        return ntpath.normcase(ntpath.normpath(str(left))) == ntpath.normcase(ntpath.normpath(str(right)))
    return os.path.abspath(left) == os.path.abspath(right)


def _identity(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _package(value):
    return isinstance(value, str) and bool(_PACKAGE.fullmatch(value)) and not value.endswith('.') and '..' not in value


def _no_unknown_flags(value, *, allow_null_restart=False):
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, part in current.items():
                if key in ('running', 'uncertain', 'reboot_required', 'cancelled', 'blocked_by_prior_installer'):
                    _require(part is False, 'A live, cancelled, restart or unknown flag prevents recovery.')
                elif key in ('restartNeeded', 'rebootRequired'):
                    _require(part is False or allow_null_restart and part is None, 'An unverified restart prevents recovery.')
                elif key == 'outcomeKnown':
                    _require(part is True, 'An unknown installer outcome prevents recovery.')
                elif key == 'installer_started':
                    _require(type(part) is bool, 'An unknown installer lifecycle prevents recovery.')
                elif key == 'exitCode':
                    _require(type(part) is int and part == 0, 'A nonzero or unknown installer exit prevents recovery.')
                if isinstance(part, (dict, list)):
                    stack.append(part)
        elif isinstance(current, list):
            stack.extend(current)


def _release_source(parent):
    paths = []
    for title in ('Inspect current resource usage', 'Capture final resource report'):
        stages = parent.get('stages')
        _require(isinstance(stages, list) and len(stages) <= 2000, 'The parent stage inventory is invalid.')
        rows = [row for row in stages if isinstance(row, dict) and row.get('title') == title]
        _require(len(rows) == 1 and isinstance(rows[0].get('result'), dict), 'The historical runtime audit is ambiguous.')
        processes = rows[0]['result'].get('Processes')
        _require(isinstance(processes, list) and len(processes) <= 10000, 'Historical process coverage is unavailable.')
        matches = [row for row in processes if isinstance(row, dict) and str(row.get('Name', '')).casefold() == 'optimus.exe']
        _require(len(matches) == 1 and isinstance(matches[0].get('Path'), str), 'The historical release path is ambiguous.')
        executable = Path(matches[0]['Path'])
        _require(executable.is_absolute() and executable.name.casefold() == 'optimus.exe'
                 and executable.parent.name == LEGACY_VERSION, 'The historical executable is not an exact versioned 0.8.0 release.')
        _require(_ordinary_data_path(executable, file=True) is not None, 'The original release executable is absent.')
        paths.append(executable)
    _require(_same_path(paths[0], paths[1]), 'The historical release executable paths differ.')
    return paths[0], paths[0].parent / 'source/src/optimus/maintenance.py'


def _runtime_proof(parent, journal, executable):
    audits = []
    for title in ('Inspect current resource usage', 'Capture final resource report'):
        rows = [row for row in parent['stages'] if isinstance(row, dict) and row.get('title') == title]
        _require(len(rows) == 1 and rows[0].get('status') == 'completed', 'The initial/final runtime audit is missing or ambiguous.')
        audit = rows[0].get('result')
        _require(isinstance(audit, dict) and audit.get('ApplicationVersion') == LEGACY_VERSION
                 and audit.get('ToolVersion') == LEGACY_VERSION, 'The historical application version is unverified.')
        processes = audit.get('Processes')
        _require(isinstance(processes, list) and len(processes) <= 10000, 'Historical process coverage is unavailable.')
        matching = [row for row in processes if isinstance(row, dict) and str(row.get('Name', '')).casefold() == 'optimus.exe']
        _require(len(matching) == 1 and _same_path(matching[0].get('Path'), executable), 'The exact historical release path is unverified.')
        process = matching[0]
        _require(type(process.get('ProcessId')) is int and process['ProcessId'] > 0
                 and isinstance(process.get('StartTimeUtc'), str) and process['StartTimeUtc'], 'The historical application process identity is incomplete.')
        captured = datetime.fromisoformat(str(audit.get('CapturedUtc', '')).replace('Z', '+00:00'))
        _require(captured.tzinfo is not None, 'The audit capture timestamp has no timezone.')
        instant = captured.timestamp()
        _require(parent['started_at'] - 5 <= instant <= parent['finished_at'] + 5, 'The runtime audit is outside the parent run.')
        runtime = audit.get('Runtime', {})
        _require(isinstance(runtime, dict), 'Unknown historical runtime metadata.')
        for record in (process, runtime):
            for key in ('source_workspace', 'SourceWorkspace', 'OPTIMUS_SOURCE_WORKSPACE'):
                _require(record.get(key) in (None, ''), 'An editable/custom historical runtime cannot use this recovery rule.')
            _require(record.get('mode') in (None, '', 'frozen') and record.get('frozen', True) is True,
                     'An editable/custom historical runtime cannot use this recovery rule.')
        command = process.get('CommandLine', runtime.get('command_line'))
        known_frozen = False
        if command not in (None, '', []):
            if isinstance(command, str):
                _require('--source-workspace' not in command.casefold(), 'An editable historical runtime cannot use this recovery rule.')
                tokens = command.strip().split()
                tokens = [token.strip('"') for token in tokens]
            else:
                _require(isinstance(command, list) and all(isinstance(token, str) for token in command), 'Unknown historical launch arguments.')
                tokens = command
            _require(tokens and _same_path(tokens[0], executable)
                     and all(token in ('--admin-relaunch', '--no-orb') for token in tokens[1:]), 'Unrecognized historical launch arguments prevent recovery.')
            known_frozen = True
        audits.append((process, instant, known_frozen))
    _require(audits[0][0]['ProcessId'] == audits[1][0]['ProcessId']
             and audits[0][0]['StartTimeUtc'] == audits[1][0]['StartTimeUtc'], 'Historical runtime identities differ.')
    _require(audits[0][1] <= journal['started_at'] + 5 and audits[1][1] >= journal['finished_at'] - 5, 'Runtime audits do not bracket the update attempt.')
    return all(row[2] for row in audits)


def _classify(journal, parent, data_dir, journal_id, owner, executable):
    _require(type(journal.get('schema')) is int and journal['schema'] == 1 and journal.get('id') == journal_id
             and journal.get('owner') == owner and owner.startswith('sid:'), 'The journal schema or current SID ownership is unverified.')
    _require(journal.get('status') == 'uncertain' and journal.get('installer_prepared') is True
             and journal.get('title') == _TITLE, 'This is not the supported legacy unresolved software journal.')
    parent_id = journal.get('parent_run_id')
    _require(_identity(parent_id) and parent.get('id') == parent_id, 'Parent run identities do not match.')
    _require(parent.get('status') in ('completed_with_errors', 'failed') and parent.get('cancelled') is False
             and _same_path(parent.get('log_path'), data_dir / 'runs' / (parent_id + '.json')), 'The completed parent run is unverified.')
    for record in (parent, journal):
        _timestamp(record.get('started_at'))
        _timestamp(record.get('finished_at'))
        _require(record['started_at'] <= record['finished_at'], 'Historical timestamps are reversed.')
    _require(parent['started_at'] <= journal['started_at'] <= journal['finished_at'] <= parent['finished_at'], 'The worker timestamps are outside its parent run.')
    stages = parent.get('stages')
    _require(isinstance(stages, list) and len(stages) <= 2000, 'The parent stage inventory is invalid.')
    stages = [row for row in stages if isinstance(row, dict) and row.get('title') == _TITLE]
    _require(len(stages) == 1, 'The parent software stage is ambiguous.')
    stage = stages[0]
    _require(stage.get('name') == _TITLE and stage.get('status') == 'failed' and stage.get('background') is True
             and _same_path(stage.get('worker_log_path'), data_dir / 'parallel-updates' / (journal_id + '.json')),
             'The failed parent stage is not bound to this worker.')
    _require(stage.get('started_at') == journal['started_at'] and type(stage.get('finished_at')) in (int, float)
             and journal['finished_at'] <= stage['finished_at'] <= min(parent['finished_at'], journal['finished_at'] + 5), 'Worker/stage timestamps do not match.')
    result, saved = stage.get('result'), journal.get('result')
    _require(isinstance(result, dict) and isinstance(saved, dict), 'Worker receipts are missing.')
    for record in (result, saved):
        _require(type(record.get('failed_items')) is int and record['failed_items'] == 1
                 and record.get('cancelled') is False and record.get('restartNeeded') is False
                 and record.get('stop_reason') == _STOP, 'The exact single preflight-failure shape is unverified.')
    _require(result.get('review_only') is False and result.get('policy') in ('all_supported', 'selected'), 'This was not a supported software apply stage.')
    settings = parent.get('settings')
    _require(isinstance(settings, dict) and settings.get('software_updates_enabled') is True
             and settings.get('software_update_agreements_accepted') is True
             and settings.get('software_update_policy') == result['policy'], 'The historical approved software scope is unverified.')
    if result['policy'] == 'all_supported':
        _require(settings.get('software_update_all_approved') is True, 'The historical all-supported scope is unverified.')
    scan = result.get('scan')
    _require(isinstance(scan, dict) and type(scan.get('exitCode')) is int and scan['exitCode'] == 0
             and scan.get('structured') is True and scan.get('source') == 'winget', 'The historical WinGet source scan is unverified.')
    offers = scan.get('updates')
    _require(isinstance(offers, list) and 1 <= len(offers) <= 1000, 'The historical package inventory is invalid.')
    catalog = {}
    for offer in offers:
        _require(isinstance(offer, dict) and _package(offer.get('id')) and offer.get('package_id') == offer['id']
                 and offer.get('source') == 'winget' and offer['id'].casefold() not in {key.casefold() for key in catalog}
                 and all(isinstance(offer.get(key), str) and offer[key] for key in ('version', 'available')), 'A historical package identity is ambiguous.')
        catalog[offer['id']] = offer
    exclusions = settings.get('software_update_exclude_ids', [])
    _require(isinstance(exclusions, list) and all(_package(value) for value in exclusions), 'The historical exclusions are invalid.')
    if result['policy'] == 'all_supported':
        selected = [identity for identity in catalog if identity.casefold() not in {value.casefold() for value in exclusions}]
    else:
        ids = settings.get('software_update_ids')
        _require(isinstance(ids, list) and all(_package(value) for value in ids), 'The historical selected scope is invalid.')
        selected = [identity for identity in ids if identity in catalog and identity.casefold() not in {value.casefold() for value in exclusions}]
    items, compact = result.get('items'), saved.get('items')
    _require(isinstance(items, list) and 1 <= len(items) <= 1000 and isinstance(compact, list) and len(items) == len(compact), 'Historical item receipts differ.')
    current = journal.get('current_item')
    _require(isinstance(current, dict) and current.get('item') == len(items) and type(current.get('item')) is int
             and current.get('current') == len(items) - 1 and type(current.get('current')) is int
             and current.get('total') == len(selected) and type(current.get('total')) is int, 'The current failed-item position is inconsistent.')
    _require([row.get('id') for row in items if isinstance(row, dict)] == selected[:len(items)]
             and result.get('deferred_ids') == selected[len(items):], 'The attempted and deferred identities do not match the approved scan.')
    for index, (row, short) in enumerate(zip(items, compact)):
        _require(isinstance(row, dict) and isinstance(short, dict) and row.get('id') == short.get('id'), 'Historical receipt identities differ.')
        if index == len(items) - 1:
            _require(row.get('status') == short.get('status') == 'failed' and row.get('error') == LEGACY_CACHE_GUARD
                     and set(row) == {'id', 'status', 'error'} and short.get('result') == {}
                     and set(short) <= {'id', 'status', 'result', 'error'}
                     and short.get('error', LEGACY_CACHE_GUARD) == LEGACY_CACHE_GUARD, 'This is not the exact pre-launch cache refusal.')
        else:
            receipt = row.get('result')
            _require(row.get('status') == short.get('status') == 'completed' and not row.get('error')
                     and isinstance(receipt, dict) and receipt.get('id') == row['id']
                     and receipt.get('package_id') == row['id'] and receipt.get('source') == 'winget'
                     and receipt.get('status') == 'completed' and receipt.get('unattended') is True
                     and type(receipt.get('exitCode')) is int and receipt['exitCode'] == 0
                     and receipt.get('running') is False and 'restartNeeded' in receipt
                     and receipt['restartNeeded'] in (None, False), 'An earlier installer lacks a complete successful exit receipt.')
            expected = catalog[row['id']]
            _require(all(row.get(field) == receipt.get(field) == expected[version]
                         for field, version in (('from_version', 'version'), ('to_version', 'available'))), 'An earlier package version receipt differs.')
            _require(short.get('result') == {key: receipt[key] for key in ('exitCode', 'running', 'restartNeeded')}, 'A compact prior receipt is inconsistent.')
    failed_id = items[-1]['id']
    _require(current.get('id') == failed_id, 'The current journal item does not identify the failed attempt.')
    expected = catalog[failed_id]
    description = f'Updating approved package {len(items)}/{len(selected)}: {failed_id} ({expected["version"]} to {expected["available"]})'
    _require(current.get('description') == description, 'The current package/version description is inconsistent.')
    _no_unknown_flags(result, allow_null_restart=True)
    _no_unknown_flags(saved, allow_null_restart=True)
    frozen = _runtime_proof(parent, journal, executable)
    return {'failed_id': failed_id, 'prior_success_ids': [row['id'] for row in items[:-1]], 'runtime_frozen': frozen}


def _review(evidence, context, journal, parent, *, now, archived_support=None):
    if isinstance(evidence, (str, Path)):
        raw = _read(Path(evidence), _MAX_JOURNAL)
        evidence = _decode(raw)
    else:
        _require(isinstance(evidence, dict), 'Historical launch arguments were not recorded; an exact operator review is required.')
        raw = json.dumps(evidence, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        _require(len(raw) <= _MAX_JOURNAL, 'Operator evidence exceeds its byte limit.')
        evidence = _decode(raw)
    _require(type(evidence.get('schema')) is int and evidence['schema'] == 1
             and evidence.get('kind') == REVIEW_KIND and evidence.get('owner') == context['owner'], 'Operator review ownership or schema is invalid.')
    for key in ('journal_id', 'parent_run_id', 'journal_sha256', 'parent_sha256', 'source_sha256', 'failed_id', 'prior_success_ids'):
        _require(evidence.get(key) == context[key], 'Operator review does not match the exact ' + key + '.')
    _require(evidence.get('historical_runtime') == REVIEW_RUNTIME and evidence.get('conclusion') == REVIEW_CONCLUSION
             and isinstance(evidence.get('note'), str) and 0 < len(evidence['note']) <= 4000, 'The historical-runtime limitation or limited conclusion is missing.')
    reviewed = _timestamp(evidence.get('reviewed_at'))
    quiet = evidence.get('quiescence')
    _require(isinstance(quiet, dict) and quiet.get('installer_processes') == [] and type(quiet.get('installer_processes')) is list, 'Current installer quiescence was not explicitly reviewed.')
    checked = _timestamp(quiet.get('checked_at'))
    _require(0 <= now - reviewed <= REVIEW_MAX_AGE_SECONDS and 0 <= now - checked <= REVIEW_MAX_AGE_SECONDS
             and checked >= journal['finished_at'] and reviewed >= checked, 'Operator review or quiescence evidence is expired or inconsistent.')
    window = evidence.get('native_log_scan')
    _require(isinstance(window, dict) and window.get('failed_package_command_absent') is True
             and _timestamp(window.get('window_started_at')) <= journal['started_at']
             and _timestamp(window.get('window_finished_at')) >= journal['finished_at'], 'Native-log coverage does not bracket the failed attempt.')
    files = evidence.get('supporting_files')
    _require(isinstance(files, list) and 3 <= len(files) <= 32, 'Bounded supporting native-log/quiescence files are required.')
    roles, snapshots, total, seen = [], [], 0, set()
    for entry in files:
        _require(isinstance(entry, dict) and entry.get('kind') in ('native_log', 'native_log_inventory', 'quiescence')
                 and isinstance(entry.get('sha256'), str) and _SHA.fullmatch(entry['sha256']), 'Supporting evidence identity is invalid.')
        path = Path(entry.get('path', ''))
        _require(str(path).casefold() not in seen, 'A supporting evidence file was repeated.')
        seen.add(str(path).casefold())
        _require(path.is_absolute(), 'Supporting evidence paths must be absolute.')
        if archived_support is None:
            content = _read(path, _MAX_SUPPORT)
        else:
            archived = archived_support.get(str(path).casefold())
            _require(archived is not None and archived[1] == entry['kind'], 'An archived evidence file does not match the reviewed file.')
            content = archived[0]
        total += len(content)
        _require(total <= 16 * 1024 * 1024 and _digest(content) == entry['sha256'], 'Supporting evidence changed or exceeds its bound.')
        roles.append(entry['kind'])
        snapshots.append((str(path), content, entry['kind']))
    _require({'native_log', 'native_log_inventory', 'quiescence'} <= set(roles)
             and roles.count('native_log') >= len(context['prior_success_ids']), 'Supporting evidence does not cover earlier successful installers and current quiescence.')
    if archived_support is not None:
        _require(len(archived_support) == len(files), 'Unreviewed archived supporting evidence was added.')
    return raw, snapshots


def _paths(data_dir, journal_id):
    data = Path(data_dir)
    _require(data.is_absolute() and _identity(journal_id), 'An absolute data directory and one exact journal ID are required.')
    _ordinary_data_path(data)
    return data, data / 'parallel-updates' / (journal_id + '.json'), data / 'update-recovery' / journal_id


def _snapshot(data_dir, journal_id, reviewed_evidence=None):
    data, path, directory = _paths(data_dir, journal_id)
    raw = _read(path, _MAX_JOURNAL)
    journal = _decode(raw)
    _require('recovery' not in journal, 'A reconciliation marker already exists; verify its completed audit receipt.')
    parent_id = journal.get('parent_run_id')
    _require(_identity(parent_id), 'Invalid parent run identity.')
    parent_raw = _read(data / 'runs' / (parent_id + '.json'), _MAX_PARENT)
    parent = _decode(parent_raw)
    executable, source_path = _release_source(parent)
    source_raw = _read(source_path, 1024 * 1024)
    _require(_digest(source_raw) == LEGACY_SOURCE_SHA256, 'The reviewed frozen 0.8.0 guard source is absent or changed.')
    owner = _current_owner()
    bound = _classify(journal, parent, data, journal_id, owner, executable)
    context = dict(journal_id=journal_id, parent_run_id=parent_id, journal_sha256=_digest(raw),
        parent_sha256=_digest(parent_raw), source_sha256=_digest(source_raw), owner=owner,
        legacy_executable=str(executable), legacy_source=str(source_path), **bound)
    review_raw, support = None, []
    if reviewed_evidence is None and not bound['runtime_frozen']:
        raise _ReviewRequired(context)
    if reviewed_evidence is not None or not bound['runtime_frozen']:
        review_raw, support = _review(reviewed_evidence, context, journal, parent, now=time.time())
    return dict(context=context, data=data, path=path, directory=directory, raw=raw, journal=journal,
        parent_raw=parent_raw, source_raw=source_raw, review_raw=review_raw, support=support)


def _repaired(snapshot, recorded_at):
    value = copy.deepcopy(snapshot['journal'])
    context = snapshot['context']
    failed = value['result']['items'][-1]
    failed.update(error=LEGACY_CACHE_GUARD, uncertain=False)
    failed['result'] = {'installer_started': False, 'outcomeKnown': True, 'running': False,
        'failureKind': 'preflight_failed', 'status': 'blocked', 'restartNeeded': False}
    value['status'] = 'needs_review'
    value['result'].update(installer_started=bool(context['prior_success_ids']), outcomeKnown=True,
        running=False, uncertain=False)
    value['recovery'] = {'schema': 1, 'kind': RECOVERY_KIND, 'recorded_at': recorded_at,
        'original_journal_sha256': context['journal_sha256'], 'parent_sha256': context['parent_sha256'],
        'source_sha256': context['source_sha256'], 'failed_id': context['failed_id'],
        'reviewed': snapshot['review_raw'] is not None,
        'note': 'Only the final failed attempt is proven not to have started an installer. Earlier successful installations remain recorded. No retry or new maintenance permission was granted.'}
    return value


def _encoded(value):
    return json.dumps(value, allow_nan=False).encode('utf-8')


def _backup(path, raw):
    _ordinary_data_path(path, file=True)
    if path.exists():
        _require(_read(path, max(len(raw), 1)) == raw, 'An existing recovery backup differs; evidence was not overwritten.')
        return
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    _require(_read(path, max(len(raw), 1)) == raw, 'Recovery backup verification failed.')


def _existing(data_dir, journal_id):
    data, path, directory = _paths(data_dir, journal_id)
    raw = _read(path, _MAX_JOURNAL)
    journal = _decode(raw)
    marker = journal.get('recovery')
    if marker is None:
        return None
    receipt = _decode(_read(directory / 'receipt.json', _MAX_JOURNAL))
    _require(receipt.get('schema') == 1 and receipt.get('kind') == RECOVERY_KIND
             and receipt.get('owner') == _current_owner() and receipt.get('journal_id') == journal_id
             and receipt.get('status') in ('prepared', 'completed') and receipt.get('repaired_sha256') == _digest(raw), 'The reconciliation audit or repaired journal changed.')
    original = _read(directory / 'journal.original.json', _MAX_JOURNAL)
    parent = _read(directory / 'parent.original.json', _MAX_PARENT)
    source = _read(directory / 'maintenance.original.py', 1024 * 1024)
    _require(_digest(original) == receipt.get('journal_sha256') and _digest(parent) == receipt.get('parent_sha256')
             and _digest(source) == receipt.get('source_sha256') == LEGACY_SOURCE_SHA256, 'Original recovery backups changed.')
    original_parent = _decode(parent)
    executable, source_path = _release_source(original_parent)
    original_journal = _decode(original)
    _require(receipt.get('parent_run_id') == original_journal.get('parent_run_id'), 'The recovery receipt parent identity changed.')
    context = _classify(original_journal, original_parent, data, journal_id, _current_owner(), executable)
    context.update(journal_id=journal_id, parent_run_id=receipt['parent_run_id'], journal_sha256=_digest(original),
        parent_sha256=_digest(parent), source_sha256=_digest(source), owner=_current_owner(),
        legacy_executable=str(executable), legacy_source=str(source_path))
    _require(all(receipt.get(key) == value for key, value in context.items()), 'The recovery receipt evidence context changed.')
    recorded_at = _timestamp(receipt.get('recorded_at'))
    _require(recorded_at <= time.time() + 5, 'The recovery receipt timestamp is in the future.')
    if receipt['status'] == 'completed':
        _require(recorded_at <= _timestamp(receipt.get('completed_at')) <= time.time() + 5, 'The completed recovery timestamp is inconsistent.')
    else:
        _require('completed_at' not in receipt, 'An unfinished recovery contains a completion claim.')
    if 'supersedes_receipt_sha256' in receipt:
        previous_sha = receipt.get('supersedes_receipt_sha256')
        _require(isinstance(previous_sha, str) and _SHA.fullmatch(previous_sha)
                 and receipt.get('superseded_receipt') == 'receipt.previous-' + previous_sha + '.json', 'The superseded audit identity is invalid.')
        previous_raw = _read(directory / receipt['superseded_receipt'], _MAX_JOURNAL)
        _require(_digest(previous_raw) == previous_sha, 'The superseded original audit changed.')
        previous = _decode(previous_raw)
        _require(previous.get('status') == 'prepared' and all(previous.get(key) == context[key]
                 for key in ('journal_id', 'parent_run_id', 'journal_sha256', 'parent_sha256', 'source_sha256', 'owner')),
                 'The superseded audit does not describe this original record.')
    support = receipt.get('support')
    _require(isinstance(support, list) and len(support) <= 32, 'The archived supporting evidence list is invalid.')
    evidence_directory = directory
    if marker.get('reviewed') is True:
        evidence_name = receipt.get('evidence_directory')
        _require(isinstance(evidence_name, str) and re.fullmatch(r'review-[a-f0-9]{64}', evidence_name)
                 and evidence_name == 'review-' + str(receipt.get('review_sha256')), 'The archived operator-evidence directory is invalid.')
        evidence_directory = directory / evidence_name
        _ordinary_data_path(evidence_directory)
    archived_support, names, total = {}, set(), 0
    for entry in support:
        _require(_identity(journal_id) and isinstance(entry, dict) and isinstance(entry.get('name'), str)
                 and re.fullmatch(r'support-[0-9]{2}\.bin', entry['name']), 'Invalid supporting backup path.')
        _require(entry['name'] not in names and isinstance(entry.get('original'), str)
                 and entry['original'].casefold() not in archived_support, 'A supporting recovery backup was repeated.')
        names.add(entry['name'])
        content = _read(evidence_directory / entry['name'], _MAX_SUPPORT)
        total += len(content)
        _require(total <= 16 * 1024 * 1024 and _digest(content) == entry.get('sha256'), 'A supporting recovery backup changed.')
        archived_support[entry['original'].casefold()] = (content, entry.get('kind'))
    review_raw = None
    if marker.get('reviewed') is True:
        review_raw = _read(evidence_directory / 'review.original.json', _MAX_JOURNAL)
        _require(_digest(review_raw) == receipt.get('review_sha256'), 'The operator-review backup changed.')
        _review(_decode(review_raw), context, original_journal, original_parent,
                now=recorded_at, archived_support=archived_support)
    else:
        _require(context['runtime_frozen'] and receipt.get('review_sha256') is None and not support,
                 'The original frozen-runtime evidence is unverified.')
    snapshot = dict(journal=_decode(original), context=context, review_raw=review_raw)
    _require(_encoded(_repaired(snapshot, receipt.get('recorded_at'))) == raw, 'The repaired lifecycle fields do not match the original evidence.')
    return {'status': 'already_reconciled' if receipt['status'] == 'completed' else 'audit_pending',
        'eligible': False, 'journal_id': journal_id, 'parent_run_id': receipt['parent_run_id'],
        'receipt': str(directory / 'receipt.json'), 'changed': False, 'installer_actions': 0,
        'context': context, '_receipt': receipt}


def inspect_legacy_cache_guard(data_dir, journal_id, *, reviewed_evidence=None):
    """Read bounded evidence; unknown, edited or conflicting records stay blocked."""
    try:
        existing = _existing(data_dir, journal_id)
        if existing is not None:
            return {key: value for key, value in existing.items() if not key.startswith('_')}
        snapshot = _snapshot(data_dir, journal_id, reviewed_evidence)
        return {'status': 'eligible', 'eligible': True, 'changed': False, 'installer_actions': 0,
                **snapshot['context'], 'reviewed': snapshot['review_raw'] is not None}
    except _ReviewRequired as error:
        return {'status': 'blocked', 'eligible': False, 'changed': False, 'installer_actions': 0,
                **error.context, 'requires_operator_review': True, 'reason': str(error)}
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        return {'status': 'blocked', 'eligible': False, 'changed': False, 'installer_actions': 0,
                'journal_id': str(journal_id), 'reason': str(error)[:2000]}


def _lease(data_dir, lease):
    _require(isinstance(lease, RunLease) and lease.handle is not None and not lease.handle.closed
             and _same_path(lease.path, Path(data_dir) / 'maintenance-run.lock'), 'A currently held RunLease for this data directory is required.')
    info = _ordinary_data_path(lease.path, file=True)
    held = os.fstat(lease.handle.fileno())
    _require(info is not None and (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino), 'The held maintenance lease anchor changed.')


def reconcile_legacy_cache_guard(data_dir, journal_id, *, lease, reviewed_evidence=None, inspection=None):
    """Repair metadata once under the caller's held lease; never run installers."""
    changed = False
    try:
        _lease(data_dir, lease)
        existing = _existing(data_dir, journal_id)
        if existing is not None:
            if existing['status'] == 'audit_pending':
                receipt = dict(existing['_receipt'], status='completed', completed_at=time.time())
                _private_json(Path(existing['receipt']), receipt)
                existing['status'] = 'already_reconciled'
            return {key: value for key, value in existing.items() if not key.startswith('_')}
        snapshot = _snapshot(data_dir, journal_id, reviewed_evidence)
        context, directory = snapshot['context'], snapshot['directory']
        if inspection is not None:
            _require(isinstance(inspection, dict) and inspection.get('status') == 'eligible'
                     and all(inspection.get(key) == context[key] for key in ('journal_id', 'parent_run_id', 'journal_sha256', 'parent_sha256', 'source_sha256', 'owner')),
                     'Inspected evidence changed before reconciliation.')
        _ordinary_data_path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _ordinary_data_path(directory)
        _backup(directory / 'journal.original.json', snapshot['raw'])
        _backup(directory / 'parent.original.json', snapshot['parent_raw'])
        _backup(directory / 'maintenance.original.py', snapshot['source_raw'])
        support = []
        review_sha = _digest(snapshot['review_raw']) if snapshot['review_raw'] is not None else None
        evidence_name = 'review-' + review_sha if review_sha is not None else None
        evidence_directory = directory
        if snapshot['review_raw'] is not None:
            # A later fresh operator review gets a new append-only archive.
            # Earlier proof survives even if the first intent write failed.
            evidence_directory = directory / evidence_name
            _ordinary_data_path(evidence_directory)
            evidence_directory.mkdir(mode=0o700, exist_ok=True)
            _ordinary_data_path(evidence_directory)
            _backup(evidence_directory / 'review.original.json', snapshot['review_raw'])
        for index, (original, raw, kind) in enumerate(snapshot['support']):
            name = f'support-{index:02d}.bin'
            _backup(evidence_directory / name, raw)
            support.append(dict(name=name, original=original, sha256=_digest(raw), kind=kind))
        receipt_path = directory / 'receipt.json'
        receipt_info = _ordinary_data_path(receipt_path, file=True)
        if receipt_info is None:
            receipt = dict(schema=1, kind=RECOVERY_KIND, status='prepared', recorded_at=time.time(),
                **context, support=support, review_sha256=review_sha, evidence_directory=evidence_name)
        else:
            previous_receipt_raw = _read(receipt_path, _MAX_JOURNAL)
            receipt = _decode(previous_receipt_raw)
            _require(receipt.get('status') == 'prepared' and all(receipt.get(key) == context[key]
                     for key in ('journal_id', 'parent_run_id', 'journal_sha256', 'parent_sha256', 'source_sha256', 'owner')),
                     'Existing recovery intent differs; it was not overwritten.')
            if receipt.get('review_sha256') != review_sha:
                # The journal is still the exact original uncertain record and
                # the new review passed all fresh checks. Archive old intent
                # bytes before replacing the pending intent, without erasing it.
                previous_sha = _digest(previous_receipt_raw)
                previous_name = 'receipt.previous-' + previous_sha + '.json'
                _backup(directory / previous_name, previous_receipt_raw)
                receipt = dict(schema=1, kind=RECOVERY_KIND, status='prepared', recorded_at=time.time(),
                    **context, support=support, review_sha256=review_sha, evidence_directory=evidence_name,
                    supersedes_receipt_sha256=previous_sha, superseded_receipt=previous_name)
            else:
                _require(receipt.get('support') == support and receipt.get('evidence_directory') == evidence_name,
                         'Existing recovery intent evidence differs; it was not overwritten.')
        repaired = _repaired(snapshot, receipt['recorded_at'])
        receipt['repaired_sha256'] = _digest(_encoded(repaired))
        _private_json(receipt_path, receipt)  # Backups and intent precede the status change.
        fresh = _snapshot(data_dir, journal_id, reviewed_evidence)
        _require(fresh['context'] == context, 'Evidence changed while preparing the private recovery audit.')
        _private_json(snapshot['path'], repaired)
        changed = True
        _require(_read(snapshot['path'], _MAX_JOURNAL) == _encoded(repaired), 'The repaired journal could not be verified.')
        _private_json(receipt_path, dict(receipt, status='completed', completed_at=time.time()))
        return {'status': 'reconciled', 'eligible': False, 'changed': True, 'installer_actions': 0,
            **context, 'receipt': str(receipt_path), 'backup_directory': str(directory),
            'note': repaired['recovery']['note']}
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        return {'status': 'audit_pending' if changed else 'blocked', 'eligible': False, 'changed': changed,
            'installer_actions': 0, 'journal_id': str(journal_id), 'reason': str(error)[:2000]}
