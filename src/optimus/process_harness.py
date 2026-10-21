"""Read-only process evidence and explicitly selected, recoverable actions.

Public reports omit command lines. Action authority is an opaque, expiring
in-memory snapshot owned by this Backend; exported JSON cannot authorize writes.
"""
from __future__ import annotations

import copy
import math
import ntpath
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .automated_system import startup_descriptor
from .workflow import atomic_json
from . import performance

TTL_SECONDS = 900
MAX_ACTIONS = 25
_ID = re.compile(r'^[0-9a-f]{32}$')


def _text(value, maximum=1024):
    text = re.sub(r'[\x00-\x1f]', ' ', str(value or ''))
    return re.sub(r'https?://\S+', '[URL omitted]', text, flags=re.I)[:maximum]


def _norm(path):
    return ntpath.normcase(ntpath.normpath(str(path or ''))) if path else ''


def _within(path, root):
    path, root = _norm(path), _norm(root)
    return bool(root) and (path == root or path.startswith(root.rstrip('\\') + '\\'))


def _identity(record):
    try:
        pid, created, exe = record['pid'], record['create_time'], record['exe']
        if type(pid) is not int or pid <= 4 or type(created) not in (int, float) or not math.isfinite(created) or not exe:
            return None
        if not isinstance(exe, str) or not ntpath.isabs(exe) or '\x00' in exe:
            return None
        return pid, float(created), _norm(exe)
    except (TypeError, KeyError):
        return None


def _protected_executable(executable, name=''):
    combined = re.sub('[^a-z0-9]', '', (str(name) + str(executable)).casefold())
    basename = ntpath.basename(str(executable)).casefold()
    if 'bingwallpaper' in combined:
        return 'Bing Wallpaper is preserved.'
    if basename in performance._SECURITY_NAMES or str(name).casefold() in performance._SECURITY_NAMES:
        return 'Windows or security infrastructure is protected.'
    if basename in performance._CODEX_RUNTIME_NAMES or 'codex' in combined or performance._trusted_codex_exe(str(executable)):
        return 'Codex and its runtime are protected.'
    if _within(executable, os.environ.get('SystemRoot', r'C:\Windows')):
        return 'Windows executables are protected.'
    if re.search(r'\\(?:windows defender|microsoft defender|microsoft antimalware|eset|avast software|avg|sophos|bitdefender|malwarebytes|crowdstrike|sentinelone|kaspersky lab|norton|mcafee)\\', _norm(executable)):
        return 'Security product paths are protected.'
    return ''


def _protection(record):
    if record.get('pid') == os.getpid():
        return 'Optimus itself is protected.'
    reason = _protected_executable(record.get('exe', ''), record.get('name', ''))
    if reason:
        return reason
    if _identity(record) is None:
        return 'The complete process identity is unavailable.'
    if record.get('protected') is not False or record.get('can_sleep') is not True:
        return _text(record.get('reason')) or 'Process ownership or protection excludes changes.'
    return ''


def _command_executable(command):
    """Extract a conservative executable identity; never execute or export args."""
    command = ntpath.expandvars(str(command or '').strip())
    if command.startswith('"'):
        end = command.find('"', 1)
        candidate = command[1:end] if end > 1 else ''
    else:
        candidate = command.split(None, 1)[0] if command else ''
    return candidate if ntpath.isabs(candidate) and candidate.casefold().endswith('.exe') and '%' not in candidate else ''


def _startup_approval(record):
    descriptor = startup_descriptor(record)
    executable = _command_executable(descriptor['Command'])
    if not executable or _protected_executable(executable, descriptor['Name']):
        raise ValueError('The startup executable is unknown or protected.')
    return descriptor, executable


def _ownership(executable, applications):
    matches = []
    for app in applications:
        root = app.get('InstallLocation') or app.get('install_location')
        if isinstance(root, str) and ntpath.isabs(root) and _within(executable, root):
            matches.append((len(_norm(root)), _text(app.get('Name') or app.get('name') or 'Installed application')))
    if not matches:
        return [], 'No installed-application location matches this executable.'
    longest = max(length for length, _ in matches)
    owners = sorted({name for length, name in matches if length == longest})
    return owners, 'Exact executable containment in the most specific registered InstallLocation; this is ownership evidence, not proof of a launch source.'


class ProcessHarness:
    def __init__(self, backend):
        self.backend = backend
        self.data_dir = Path(backend.data_dir) / 'process-harness'
        self._plans = {}
        self._reports = {}
        self._lock = threading.RLock()

    def scan(self, cancel_event=None):
        now = time.time()
        report = {'id': uuid.uuid4().hex, 'created_at': now, 'expires_at': now + TTL_SECONDS,
                  'processes': [], 'actions': [], 'complete': True, 'cancelled': False, 'errors': [],
                  'notes': ['Read-only scan. No action is selected or applied automatically.',
                    'Memory use, age, helper names and a short sample do not establish that an application is obsolete or unnecessary.',
                    'Parent and startup matches provide evidence, not proof of the historical launch source.',
                    'Sleep is reversible through Wake All; startup disabling has the existing change-history recovery. Save work first.',
                    'Uninstalling is review-only in Windows Installed apps. This harness never deletes programs or invokes uninstallers.']}
        def stopped():
            return cancel_event is not None and cancel_event.is_set()
        def inventory(name, operation, expected):
            if stopped():
                report['cancelled'] = True
                report['complete'] = False
                return [] if expected is list else {}
            try:
                value = operation()
                if not isinstance(value, expected):
                    raise ValueError('Unexpected inventory format.')
                return value
            except Exception as error:
                report['complete'] = False
                report['errors'].append(name + ' unavailable (' + type(error).__name__ + ').')
                return [] if expected is list else {}
        processes = inventory('Process inventory', self.backend.processes, list)
        sampled = inventory('Resource sample', lambda: self.backend.slowdown_report(cancel_event=cancel_event), dict)
        startup = inventory('Startup inventory', self.backend.startup_list, list)
        applications = inventory('Installed application inventory', self.backend.installed_apps, list)
        if sampled.get('complete') is not True:
            report['complete'] = False
            report['errors'].append('The resource sample is incomplete; resource comparisons are partial.')
        if sampled.get('truncated'):
            report['notes'].append('Sampled application groups were truncated; every process inventory row is retained and unmatched metrics remain unavailable.')
        report['cancelled'] = report['cancelled'] or stopped() or sampled.get('cancelled') is True
        if report['cancelled']:
            report['complete'] = False
        metrics = {_norm(item.get('exe')): item for item in sampled.get('applications', []) if isinstance(item, dict) and item.get('exe')}
        startup_matches, private = [], {}
        for record in startup:
            if not isinstance(record, dict):
                report['complete'] = False
                report['errors'].append('A startup inventory row is invalid.')
                continue
            try:
                descriptor, executable = _startup_approval(record)
            except ValueError:
                continue
            startup_matches.append((descriptor, executable))
            action = {'id': uuid.uuid4().hex, 'kind': 'disable_startup',
                      'label': 'Disable sign-in launch: ' + _text(descriptor['Name']),
                      'target': _text(descriptor['Name']) + ' | ' + _text(descriptor['Source']),
                      'reason': 'This enabled, manageable registration launches ' + _text(executable) + '. Disabling prevents this registration from launching at sign-in; it does not uninstall the program.',
                      'selected': False}
            report['actions'].append(action)
            private[action['id']] = {'kind': action['kind'], 'descriptor': descriptor}
        by_pid = {row.get('pid'): row for row in processes if isinstance(row, dict)}
        for source in processes:
            if not isinstance(source, dict):
                report['complete'] = False
                report['errors'].append('A process inventory row is invalid.')
                continue
            record = copy.deepcopy(source)
            exe, pid = record.get('exe', ''), record.get('pid')
            protected = _protection(record)
            owners, owner_reason = _ownership(exe, [app for app in applications if isinstance(app, dict)])
            parent_record = by_pid.get(record.get('ppid'))
            parent = None
            if parent_record and _identity(parent_record) and _identity(record) and parent_record['create_time'] <= record['create_time']:
                parent = {'pid': parent_record['pid'], 'name': _text(parent_record.get('name')), 'exe': _text(parent_record.get('exe'))}
            matched = [descriptor for descriptor, launch_exe in startup_matches if _norm(launch_exe) == _norm(exe)] if exe else []
            evidence = []
            if matched:
                evidence.append('The executable exactly matches enabled sign-in registration(s): ' + ', '.join(_text(item['Name']) for item in matched) + '. This does not prove which registration launched this instance.')
            if parent:
                evidence.append('The current recorded parent is ' + parent['name'] + '; its creation time predates this child. Historical ancestry can still be unavailable.')
            confidence = 'evidence' if evidence else 'unknown'
            if not evidence and re.search(r'update|helper|launcher|background', str(record.get('name', '')), re.I):
                evidence.append('Its name suggests an updater, helper or launcher; this is a naming inference only.')
                confidence = 'inferred'
            measure = metrics.get(_norm(exe))
            resource = None if measure is None else {key: copy.deepcopy(measure.get(key)) for key in ('cpu_percent', 'memory_rss', 'memory_percent', 'read_bytes_per_second', 'write_bytes_per_second', 'severity', 'measured_intervals')}
            row = {key: record.get(key) for key in ('pid', 'ppid', 'create_time', 'memory_rss', 'private_bytes')}
            row.update(name=_text(record.get('name')), exe=_text(exe), application=', '.join(owners) or 'Unidentified application',
                       owners=owners, ownership_evidence=owner_reason, parent=parent,
                       why_running=' '.join(evidence) or 'The launch source is unknown from the available inventory.',
                       why_running_confidence=confidence, startup_matches=[{'name': _text(item['Name']), 'source': _text(item['Source'])} for item in matched],
                       protected=bool(protected), protection_reason=protected,
                       metrics=resource, measurement_available=measure is not None,
                       recommendation='Protected: review its details only.' if protected else 'Review the measured work and whether you still need it. Save work and close normally, or explicitly select reversible Sleep. Startup and uninstall decisions require your review.')
            report['processes'].append(row)
            if not protected:
                action = {'id': uuid.uuid4().hex, 'kind': 'sleep_process',
                          'label': 'Sleep ' + row['name'] + ' (PID ' + str(pid) + ')',
                          'target': row['name'] + ' | PID ' + str(pid) + ' | ' + _text(exe),
                          'reason': 'Optional reversible pause for this exact process lifetime. Select only if its work is unnecessary now; RAM or age alone does not establish that.', 'selected': False}
                report['actions'].append(action)
                private[action['id']] = {'kind': action['kind'], 'record': record}
        report['summary'] = {'processes': len(report['processes']), 'protected': sum(row['protected'] for row in report['processes']),
                             'optional_actions': len(report['actions']), 'measured_processes': sum(row['measurement_available'] for row in report['processes'])}
        report['log_path'] = str(self.data_dir / 'scans' / (report['id'] + '.json'))
        atomic_json(Path(report['log_path']), report)
        with self._lock:
            for key in list(self._plans):
                if self._plans[key]['report']['expires_at'] < now:
                    del self._plans[key]
            while len(self._plans) >= 10:
                del self._plans[next(iter(self._plans))]
            self._plans[report['id']] = {'report': copy.deepcopy(report), 'actions': private}
            self._reports[report['id']] = copy.deepcopy(report)
            while len(self._reports) > 20:
                del self._reports[next(iter(self._reports))]
        return report

    def apply(self, request, cancel_event=None):
        if not isinstance(request, dict) or request.get('confirmed') is not True:
            raise ValueError('Explicit confirmation is required for the selected harness actions.')
        plan_id, selected = request.get('plan_id'), request.get('selected_ids')
        if not isinstance(plan_id, str) or not _ID.fullmatch(plan_id) or type(selected) is not list or len(selected) > MAX_ACTIONS or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in selected) or len(set(selected)) != len(selected):
            raise ValueError('Select at most 25 distinct action IDs from one current scan.')
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan:
                raise ValueError('This scan does not belong to the current Optimus session; scan again.')
            report = plan['report']
            if time.time() > report['expires_at'] or time.time() < report['created_at']:
                raise ValueError('The scan expired. Scan again before selecting actions.')
            if report.get('complete') is not True or report.get('cancelled'):
                raise ValueError('The scan was incomplete or cancelled. A fresh complete scan is required.')
            if any(value not in plan['actions'] for value in selected):
                raise ValueError('An action does not belong to this scan.')
            if not selected:
                return {'plan_id': plan_id, 'status': 'no_changes', 'items': [], 'failed_items': 0}
            del self._plans[plan_id]  # Single-use authority; retry requires a fresh scan.
            result = {'id': uuid.uuid4().hex, 'plan_id': plan_id, 'selected_ids': list(selected), 'status': 'running', 'items': [], 'failed_items': 0, 'cancelled': False,
                      'note': 'Use Wake All for Optimus-owned sleeping processes and change history to restore supported startup changes.'}
            path = self.data_dir / 'actions' / (result['id'] + '.json')
            result['log_path'] = str(path)
            atomic_json(path, result)  # No action starts without a durable journal.
            public = {action['id']: action for action in report['actions']}
            for action_id in selected:
                if cancel_event is not None and cancel_event.is_set():
                    result['cancelled'] = True
                    break
                action = plan['actions'][action_id]
                item = {'id': action_id, 'kind': action['kind'], 'target': public[action_id]['target'], 'status': 'prepared'}
                result['items'].append(item)
                atomic_json(path, result)
                try:
                    fresh = self.backend.processes()
                    if not isinstance(fresh, list) or any(not isinstance(row, dict) for row in fresh):
                        raise ValueError('Fresh process inventory is incomplete.')
                    if cancel_event is not None and cancel_event.is_set():
                        result['cancelled'] = True
                        item['status'] = 'cancelled'
                        atomic_json(path, result)
                        break
                    if action['kind'] == 'sleep_process':
                        expected = _identity(action['record'])
                        matches = [row for row in fresh if _identity(row) == expected]
                        if len(matches) != 1 or _protection(matches[0]):
                            raise ValueError('The selected process ended, changed or is now protected.')
                        if cancel_event is not None and cancel_event.is_set():
                            result['cancelled'] = True
                            item['status'] = 'cancelled'
                            break
                        outcome = self.backend.sleep_process(copy.deepcopy(matches[0]))
                        if not isinstance(outcome, dict) or outcome.get('status') != 'sleeping':
                            raise RuntimeError('Sleep did not report a confirmed sleeping state.')
                        item.update(status='completed', changed=outcome.get('changed') is True, recovery='Wake All')
                    else:
                        current = self.backend.startup_list()
                        if not isinstance(current, list) or any(not isinstance(row, dict) for row in current):
                            raise ValueError('Fresh startup inventory is unavailable.')
                        matches = [row for row in current if isinstance(row, dict) and str(row.get('Id', '')).casefold() == action['descriptor']['Id'].casefold()]
                        if len(matches) != 1:
                            raise ValueError('The startup registration changed.')
                        descriptor, _ = _startup_approval(matches[0])
                        if descriptor != action['descriptor']:
                            raise ValueError('The startup fingerprint or command changed.')
                        if cancel_event is not None and cancel_event.is_set():
                            result['cancelled'] = True
                            item['status'] = 'cancelled'
                            break
                        outcome = self.backend.startup_set(copy.deepcopy(matches[0]), False)
                        if not isinstance(outcome, dict) or not (outcome.get('Id') or outcome.get('id')):
                            raise RuntimeError('Startup change did not return a recovery record.')
                        item.update(status='completed', changed=True, recovery='Change history', change_id=_text(outcome.get('Id') or outcome.get('id')))
                except Exception as error:
                    item.update(status='failed', error='The action was refused or did not complete (' + type(error).__name__ + '). Review current process/startup state and recovery history before retrying.')
                    result['failed_items'] += 1
                atomic_json(path, result)
            result['status'] = 'cancelled' if result['cancelled'] else 'completed_with_errors' if result['failed_items'] else 'completed'
            result['deferred_ids'] = selected[len(result['items']):]
            result['finished_at'] = time.time()
            atomic_json(path, result)
            return result

    def export(self, plan_id, destination):
        with self._lock:
            report = copy.deepcopy(self._reports.get(plan_id)) if isinstance(plan_id, str) else None
        if report is None:
            raise ValueError('Choose a scan from this Optimus session to export.')
        path = Path(destination)
        if path.suffix.casefold() != '.json':
            path = path / ('process-harness-' + plan_id + '.json')
        if path.exists():
            raise FileExistsError('Choose a new JSON filename for this export.')
        atomic_json(path, report)
        return {'path': str(path), 'processes': len(report['processes']), 'changes': 0}
