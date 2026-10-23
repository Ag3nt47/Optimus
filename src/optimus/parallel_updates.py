"""Concurrent update preparation with a coordinated installer lane.

The workflow owns the global maintenance lease for this worker's entire life.
Discovery overlaps foreground maintenance; no installer is killed on cancel.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import threading
import time
import uuid

from .filesystem import _ordinary_data_path
from .navigation import local_skip, shared_update_refusal, can_fail_forward

WINDOWS_SOFTWARE = 'Review or update configured software'
NATIVE_SOFTWARE = 'Review or install approved native package updates'
WINDOWS_DRIVERS = 'Install approved driver updates'
WINDOWS_DRIVER_REVIEW = 'Review available driver updates'
DRIVER_TITLES = frozenset({WINDOWS_DRIVERS, WINDOWS_DRIVER_REVIEW})
WAITING_FOR_UPDATES = 'Waiting for background updates'
ACTIVE_JOB_STATES = frozenset({'pending', 'running', 'waiting', 'scanning', 'preparing',
                               'waiting_for_maintenance', 'waiting_for_installer', 'installing'})
BEFORE_UPDATES = frozenset({
    'Clean temporary files', 'Clean selected additional categories', 'Clean selected browser data',
    'Clean approved native temporary files and caches', 'Clean selected browser data after close or skip choice',
})
INDEPENDENT = frozenset({
    'Review installed applications', 'Identify programs contributing to slowdowns',
    'Apply supported speed enhancements', 'Prepare approved Windows CPU upgrade workaround',
    'Apply Codex performance profile', 'Enable resource analysis', 'Apply saved privacy controls',
    'Review installed native applications', 'Measure applications contributing to pressure',
    'Review native kernel and hardware-support inventory', 'Review native security observations',
    'Review vendor support and patch limitations', 'Start requested read-only background analysis',
    'Review startup registrations', 'Review duplicate configuration', 'Review connected cloud drive',
    'Review available driver updates', 'Review native kernel and hardware-support coverage',
    'Review native startup registrations', 'Review native duplicate copies',
})
INSTALLER_STAGES = frozenset({
    'Install approved driver updates', 'Check or install Windows security updates',
    'Optimize eligible internal drives', 'Remove selected consumer app group',
    'Remove Microsoft 365 consumer packages', 'Remove new Outlook package',
    'Uninstall Microsoft Edge using the supported installer',
})
BLOCKED_STAGES = INSTALLER_STAGES | frozenset({
    WINDOWS_SOFTWARE, NATIVE_SOFTWARE, 'Clean temporary files', 'Clean selected additional categories',
    'Clean selected browser data', 'Recycle approved local duplicates', 'Recycle approved cloud duplicates',
    'Disable approved startup entries', 'Scan for spyware and malware',
    'Review or disable approved user startup entries', 'Find or recycle approved duplicate copies',
    'Clean approved native temporary files and caches', 'Clean selected browser data after close or skip choice',
})


def independent_stage(title):
    return title in INDEPENDENT or title.startswith('Review duplicates: ')


def isolated_backend(backend, *, isolate_bridge=False):
    """Copy known mutable updater caches without discovering host adapters."""
    result = copy.copy(backend)
    if isolate_bridge:
        from .bridge import PowerShellBridge
        if type(getattr(backend, 'bridge', None)) is PowerShellBridge:
            # A long driver install must not hold the foreground inventory
            # bridge's request lock. Construction does not start PowerShell.
            result.bridge = PowerShellBridge(backend.bridge.data_dir)
    from .maintenance import Maintenance
    from .native_operations import NativePackages
    service = getattr(backend, 'maintenance', None)
    if type(service) is Maintenance:
        result.maintenance = copy.copy(service)
        result.maintenance._last_update_ids = set()
        result.maintenance._last_update_packages = {}
        result.maintenance._last_update_scan = 0.0
        if hasattr(result, 'bridge'):
            result.maintenance.bridge = result.bridge
    service = getattr(backend, 'native_packages', None)
    if type(service) is NativePackages:
        result.native_packages = copy.copy(service)
        result.native_packages._last_scan = None
        # Preserve the manager's serialization lock, while isolating its cache.
        # Independent inventory never starts another package installer.
    return result


class ProgressMux:
    """Keep foreground stage/progress stable while background item text changes."""
    def __init__(self, callback):
        self.callback = callback
        self.lock = threading.RLock()
        self.foreground = {}
        self.jobs = {}
        self.worker_started = {}
        self.worker_finished = {}

    def _event(self):
        event = dict(self.foreground)
        if self.jobs:
            event['jobs'] = []
            processes = list(event.get('processes', []))
            for job_id, job in self.jobs.items():
                current = dict(job)
                if job_id in self.worker_started:
                    finished = self.worker_finished.get(job_id)
                    current['elapsed_seconds'] = max(0, (time.monotonic() if finished is None else finished)
                                                     - self.worker_started[job_id])
                event['jobs'].append(current)
                if job['status'] in ACTIVE_JOB_STATES:
                    processes.append(job['description'])
                    event['percentage'] = min(99, event.get('percentage', 0))
            active = next((job['id'] for job in event['jobs'] if job['status'] in ('running', 'installing')), None)
            if active:
                event['active_job_id'] = active
            if event.get('waiting_for') in ('installer_updates', 'software_updates', 'driver_updates'):
                active_jobs = [job for job in event['jobs'] if job['status'] in ACTIVE_JOB_STATES]
                # A finished app with held offers is not the worker that owns
                # the wait. Prefer the actual installer, otherwise a sole job.
                installing = [job for job in active_jobs if job['status'] in ('running', 'installing')]
                waiting = installing[0] if len(installing) == 1 else active_jobs[0] if len(active_jobs) == 1 else None
                if waiting is not None and waiting['id'] in ('software_updates', 'driver_updates'):
                    noun = 'driver updates' if waiting['id'] == 'driver_updates' else 'application updates'
                    detail = str(waiting.get('description') or waiting.get('current_package') or 'Current operation')[:600]
                    reason = 'Waiting for ' + noun + ': ' + detail
                    event.update(waiting_for=waiting['id'], maintenance_status='waiting', maintenance_wait_reason=reason,
                        description=reason + '. ' + ('Waiting before: ' + str(event['next_stage']) + '. ' if event.get('next_stage') else '')
                        + 'The current installer must return; cancellation stops further attempts.')
            event['processes'] = list(dict.fromkeys(processes))
        return event

    def __call__(self, event):
        with self.lock:
            self.foreground = dict(event)
            self.callback(self._event())

    def background(self, status, state=None, *, job_id='software_updates'):
        state = state if isinstance(state, dict) else {}
        with self.lock:
            previous = self.jobs.get(job_id, {})
            package = state.get('id', previous.get('current_package'))
            now = time.monotonic()
            self.worker_started.setdefault(job_id, now)
            # The card measures the whole worker, including discovery and both
            # gates. Package changes and a later join must not reset its clock
            # or extend the duration after the worker's first terminal event.
            if status not in ACTIVE_JOB_STATES:
                self.worker_finished.setdefault(job_id, now)
            self.jobs[job_id] = {'id': job_id, 'status': status,
                        'description': str(state.get('description') or previous.get('description')
                                           or ('Driver' if job_id == 'driver_updates' else 'Application') + ' update worker: ' + status)[:2000],
                        'current': state.get('current', previous.get('current', 0)),
                        'total': state.get('total', previous.get('total', 0)),
                        'scan_label': str(state.get('scan_label', previous.get('scan_label', '')))[:200],
                        'current_package': package}
            for key in ('scan_complete', 'installation_blocked', 'blocked_by_prior_installer'):
                if type(state.get(key)) is bool:
                    self.jobs[job_id][key] = state[key]
            if type(state.get('offers_count')) is int and state['offers_count'] >= 0:
                self.jobs[job_id]['offers_count'] = state['offers_count']
            for key in ('scan_status', 'installation_block_reason', 'wait_reason'):
                if isinstance(state.get(key), str):
                    self.jobs[job_id][key] = state[key][:4000]
            for key in ('worker_finished', 'continuation_blocked'):
                if type(state.get(key)) is bool:
                    self.jobs[job_id][key] = state[key]
            if state.get('presentation_status') == 'finished_with_skips':
                self.jobs[job_id]['presentation_status'] = 'finished_with_skips'
            if isinstance(state.get('app_counts'), dict):
                self.jobs[job_id]['app_counts'] = {key: value for key, value in state['app_counts'].items()
                    if key in ('updated', 'skipped', 'failed', 'unverified', 'held', 'source_unverified') and type(value) is int and 0 <= value <= 1000000}
            # Serialize emission too, so an older background event cannot
            # replace a newer foreground stage in the queued UI callbacks.
            try:
                output = self._event()
                output.pop('log', None)  # The foreground log line was already emitted.
                self.callback(output)
            except Exception:
                # A display failure must never abandon a vendor installer.
                pass

    def wait_for_updates(self, next_stage=None):
        """Replace stale foreground text, preserving actual package progress."""
        with self.lock:
            description = (('Independent maintenance has finished. ' if next_stage else '')
                           + 'Background updates are still running. '
                           + ('Waiting before: ' + str(next_stage) + '. ' if next_stage else '')
                           + 'The current installer is not restarted; Cancel stops further package attempts.')
            self.foreground = dict(self.foreground, stage=WAITING_FOR_UPDATES,
                stage_status='waiting', waiting_for='installer_updates', next_stage=next_stage, description=description,
                processes=['Optimus foreground worker: waiting for background updates'],
                log='WAITING: Background updates before ' + str(next_stage or 'workflow completion'))
            try:
                self.callback(self._event())
            except Exception:
                pass
            self.foreground.pop('log', None)

    def wait_for_preparation(self):
        with self.lock:
            description = 'Waiting for app and driver discovery to finish before cleaning shared temporary files. No installer has started.'
            self.foreground = dict(self.foreground, maintenance_status='waiting_for_preparation',
                maintenance_wait_reason=description, description=description,
                processes=['Optimus maintenance worker: waiting for update discovery'])
            self.heartbeat()

    def heartbeat(self):
        with self.lock:
            try:
                self.callback(self._event())
            except Exception:
                pass

    def for_job(self, job_id):
        parent = self
        class JobProgress:
            def background(self, status, state=None):
                parent.background(status, state, job_id=job_id)
            def wait_for_updates(self, next_stage=None):
                parent.wait_for_updates(next_stage)
            def heartbeat(self):
                parent.heartbeat()
        return JobProgress()


def _known_preflight(value):
    """Explicit evidence distinguishes a failed check from a live installer."""
    if not isinstance(value, dict):
        return False
    if value.get('installer_started') is False and value.get('outcomeKnown') is True:
        return True
    if isinstance(value.get('result'), dict):
        return _known_preflight(value['result'])
    items = value.get('items')
    return (isinstance(items, list) and bool(items)
            and all(isinstance(row, dict) and _known_preflight(row) for row in items))


def _known_outcome(value):
    return (isinstance(value, dict) and (value.get('outcomeKnown') is True
            or isinstance(value.get('result'), dict) and _known_outcome(value['result'])))


def _restart_needed(value):
    if isinstance(value, dict):
        return (value.get('restartNeeded') is True or value.get('reboot_required') is True
                or value.get('exitCode') in (3010, 1641) or any(_restart_needed(item) for item in value.values()))
    return isinstance(value, list) and any(_restart_needed(item) for item in value)


def installer_barrier(result):
    """One barrier for application, driver and Windows installer outcomes."""
    if not isinstance(result, dict):
        return 'The update worker outcome is unknown; review installer state before another mutation.'
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if value.get('blocked_by_prior_installer') is True:
                return str(value.get('stop_reason') or 'A previous installer remains unresolved; later conflicting operations were deferred.')[:4000]
            if shared_update_refusal(value):
                return shared_update_refusal(value)
            if isinstance(value.get('result'), dict) and shared_update_refusal(value['result']):
                return shared_update_refusal(value['result'])
            if value.get('cancelled') is True and not local_skip(value):
                return 'Updating was cancelled; later conflicting operations were deferred.'
            if (value.get('restartNeeded') is True or value.get('reboot_required') is True
                    or value.get('exitCode') in (3010, 1641)):
                return 'An update requires a restart; later conflicting operations were deferred.'
            known_preflight = _known_preflight(value)
            known_batch = isinstance(value.get('items'), list) and _known_outcome(value)
            known_terminal = can_fail_forward(value) or isinstance(value.get('result'), dict) and can_fail_forward(value['result'])
            if (value.get('running') is True or value.get('outcomeKnown') is False
                    or value.get('uncertain') is True and not known_preflight):
                return 'An installer may still be running or its outcome is uncertain; later conflicting operations were deferred.'
            if not known_preflight and not known_batch and not known_terminal and (value.get('failed_items') or value.get('status') in ('failed', 'uncertain')
                                        or value.get('stop_reason')):
                return str(value.get('stop_reason') or 'An update failed or remains uncertain; review installer state before another mutation.')[:4000]
            if 'items' in value and not isinstance(value['items'], list):
                return 'The update item outcomes are malformed; later conflicting operations were deferred.'
            # Discovery status is independent of actual installer receipts.
            pending.extend(item for key, item in value.items() if key not in ('driver_scan', 'app_scan', 'backup'))
        elif isinstance(value, list):
            if any(not isinstance(row, dict) for row in value) and value is result.get('items'):
                return 'An update item has an unknown outcome; later conflicting operations were deferred.'
            pending.extend(value)
    return None


def _uncertain(result, *, native=False):
    """Persist liveness ambiguity, preserving explicit known preflight outcomes."""
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            known_preflight = _known_preflight(value)
            if (value.get('running') is True or value.get('outcomeKnown') is False
                    or value.get('uncertain') is True and not known_preflight):
                return True
            if not known_preflight and not _known_outcome(value):
                if any(marker in (str(value.get('status', '')) + ' ' + str(value.get('stop_reason', ''))).casefold()
                       for marker in ('timeout', 'timed out', 'still running', 'uncertain', 'verify installer state')):
                    return True
                if (value.get('error') and value.get('status') == 'failed'
                        or native and value.get('status') == 'failed' and not value.get('skipped')):
                    return True
            # A source-search timeout does not make a known idle installer live.
            pending.extend(item for key, item in value.items() if key not in ('driver_scan', 'app_scan', 'backup'))
        elif isinstance(value, list):
            pending.extend(value)
    return False


def pending_updates_barrier(backend):
    """Read owned unresolved records for both serial and parallel workflows."""
    recovery = getattr(backend, 'recovery_mutation_barrier', None)
    if callable(recovery):
        reason = recovery()
        if reason:
            return str(reason)
    probe = ParallelAppUpdates(backend, '0' * 32, WINDOWS_SOFTWARE, None, None)
    return probe._pending()


def _journal_result(result):
    saved = {key: result[key] for key in ('failed_items', 'cancelled', 'restartNeeded', 'uncertain', 'policy', 'review_only',
             'stop_reason', 'reason', 'installer_started', 'outcomeKnown', 'running', 'blocked_by_prior_installer',
             'installation_blocked', 'installation_block_reason', 'navigation_cancelled', 'navigation_cancelled_items',
             'shared_barrier', 'failure_scope') if key in result}
    if isinstance(result.get('error'), str):
        saved['error'] = result['error'][:4000]
    saved['items'] = [{'id': str(row.get('id') or row.get('binding', {}).get('id', ''))[:280],
                      'status': row.get('status'), 'uncertain': row.get('uncertain', False),
                      'presence': str(row.get('presence', ''))[:40], 'source': str(row.get('source', ''))[:40],
                      'error': str(row.get('error', ''))[:2000],
                      'reason': str(row.get('reason', ''))[:2000],
                      'result': {key: (str(row['result'][key])[:2000] if key in ('note', 'failureKind', 'error') else row['result'][key])
                                 for key in ('exitCode', 'running', 'outcomeKnown',
                                 'restartNeeded', 'requiresReview', 'installer_started',
                                 'resultCode', 'hresult', 'aggregateResultCode', 'aggregateHresult',
                                 'navigation_cancelled', 'navigation_skipped', 'verified', 'shared_barrier',
                                 'failure_scope', 'note', 'failureKind', 'error') if key in row['result']}
                      if isinstance(row.get('result'), dict) else {}}
                     for row in result.get('items', []) if isinstance(row, dict)][:1000]
    if _restart_needed(result):
        saved['restartNeeded'] = True
    for key in ('driver_scan', 'app_scan'):
        evidence = result.get(key)
        if isinstance(evidence, dict):
            saved[key] = _bounded_scan_evidence(evidence)
    if type(result.get('held_count')) is int and 0 <= result['held_count'] <= 1000:
        saved['held_count'] = result['held_count']
    if isinstance(result.get('held_ids'), list):
        saved['held_ids'] = [value[:280] for value in result['held_ids'][:1000] if isinstance(value, str)]
    if isinstance(result.get('automatic_update_holds'), list):
        saved['automatic_update_holds'] = _bounded_update_holds(result)
    if isinstance(result.get('app_presence'), dict):
        presence = result['app_presence']
        saved['app_presence'] = _journal_result({key: value for key, value in presence.items() if key != 'app_presence'})
    return saved


def _bounded_update_holds(result):
    values = result.get('automatic_update_holds')
    if not isinstance(values, list):
        return []
    return [{key: row[key][:1000 if key == 'reason' else 280]
             for key in ('id', 'available_version', 'reason') if isinstance(row.get(key), str)}
            for row in values[:100] if isinstance(row, dict)]


def _bounded_scan_evidence(evidence):
    """Keep compact scan proof without persisting a device/package inventory."""
    fields = ('schema', 'complete', 'status', 'source', 'criteria', 'online',
              'started_utc', 'completed_utc', 'duration_seconds', 'offers_count',
              'eligible_count', 'result_code', 'invalid_offer_count', 'inventory_complete',
              'devices_checked', 'device_problems', 'recognized_offers_count',
              'installed_packages_checked', 'source_reported_offers_count', 'completed_count', 'verified_count',
              'failed_count', 'skipped_count', 'deferred_count', 'remaining_count',
              'no_more_offered', 'coverage_limit', 'summary')
    saved = {}
    for key in fields:
        value = evidence.get(key)
        if key in evidence and (value is None or isinstance(value, (bool, int, float, str))):
            saved[key] = value[:2000] if isinstance(value, str) else value
    errors = evidence.get('errors')
    if isinstance(errors, list):
        saved['errors'] = [{key: str(row[key])[:1000] for key in ('source', 'error') if key in row}
                           for row in errors[:20] if isinstance(row, dict)]
    return saved


def _driver_completion_counts(result, evidence):
    """Count installed drivers only from complete successful native receipts.

    Search counts and a row labelled ``applied`` alone cannot establish that an
    installation succeeded. Missing/contradictory COM or lifecycle fields stay
    unverified; a restart remains a separate installer barrier.
    """
    offers = evidence.get('offers_count')
    if (result.get('policy') not in ('install_approved', 'install_matching')
            or result.get('review_only') is True or evidence.get('complete') is not True
            or type(offers) is not int or offers < 0):
        return None
    counts = dict(installed=0, failed=0, unverified=0, skipped=0, deferred=0, not_processed=0)
    items = result.get('items') if isinstance(result.get('items'), list) else []
    prerequisite_checks = 0
    for row in items[:1000]:
        if not isinstance(row, dict):
            counts['unverified'] += 1
            continue
        status = row.get('status')
        binding = row.get('binding') if isinstance(row.get('binding'), dict) else {}
        prerequisite_checks += int(bool(binding.get('stage')) and not binding.get('id'))
        outcome = row.get('result') if isinstance(row.get('result'), dict) else {}
        success = (status == 'applied' and row.get('uncertain') is not True
            and outcome.get('installer_started') is True and outcome.get('outcomeKnown') is True
            and outcome.get('running') is False and outcome.get('requiresReview') is not True
            and all(type(outcome.get(key)) is int and outcome[key] == expected
                    for key, expected in (('resultCode', 2), ('hresult', 0),
                                          ('aggregateResultCode', 2), ('aggregateHresult', 0))))
        if success:
            counts['installed'] += 1
        elif status == 'skipped' or status == 'cancelled' and local_skip(outcome):
            counts['skipped'] += 1
        elif status == 'deferred':
            counts['deferred'] += 1
        elif status == 'failed' and row.get('uncertain') is not True and (
                _known_preflight(row) or outcome.get('outcomeKnown') is True and outcome.get('running') is False):
            counts['failed'] += 1
        else:
            counts['unverified'] += 1
    deferred = result.get('deferred')
    if isinstance(deferred, list):
        counts['deferred'] += len(deferred)
    if type(result.get('failed_items')) is int and result['failed_items'] > 0:
        # A count without item receipts cannot be promoted to a known failure
        # or successful installation; preserve it as an unverified outcome.
        unexplained = result['failed_items'] - counts['failed'] - counts['unverified']
        counts['unverified'] += max(0, unexplained)
    counts['not_processed'] = max(0, offers - sum(counts.values()) + prerequisite_checks)
    return counts


def _driver_completion_notes(result):
    """Expose a concrete quiet-mode/backup failure rather than a scan summary."""
    notes = []
    items = result.get('items') if isinstance(result.get('items'), list) else []
    for row in items[:1000]:
        if not isinstance(row, dict) or row.get('status') == 'applied':
            continue
        outcome = row.get('result') if isinstance(row.get('result'), dict) else {}
        note = next((value for value in (row.get('error'), row.get('reason'), outcome.get('note'),
                                        outcome.get('reason'), outcome.get('error'))
                     if isinstance(value, str) and value.strip()), '')
        if not note and type(outcome.get('hresult')) is int:
            note = 'Driver result ' + str(outcome.get('resultCode')) + ', HRESULT 0x' + format(outcome['hresult'] & 0xffffffff, '08X')
            if type(outcome.get('aggregateHresult')) is int:
                note += '; aggregate HRESULT 0x' + format(outcome['aggregateHresult'] & 0xffffffff, '08X')
        if note and note[:500] not in notes:
            notes.append(note[:500])
        if len(notes) == 2:
            break
    return notes


def update_completion_state(result):
    """Describe observed availability separately from installer item counts."""
    result = result if isinstance(result, dict) else {}
    key = next((key for key in ('driver_scan', 'app_scan') if isinstance(result.get(key), dict)), None)
    evidence = result[key] if key else {}
    reason = str(result.get('stop_reason') or result.get('reason') or '')
    summary = str(evidence.get('summary') or '')
    description = reason or summary or 'Update checks completed; item results are ready.'
    if reason and summary and summary not in reason:
        description += ' ' + summary
    driver_counts = _driver_completion_counts(result, evidence) if key == 'driver_scan' else None
    app_presentation = _app_completion_presentation(result, evidence) if key == 'app_scan' else None
    if driver_counts is not None:
        reason = reason or installer_barrier(result) or ''
        counts_text = 'Driver results: ' + '; '.join(str(driver_counts[name]) + ' ' + name.replace('_', ' ')
            for name in ('installed', 'failed', 'unverified', 'skipped', 'deferred', 'not_processed')) + '.'
        details = ' '.join(_driver_completion_notes(result))
        description = ' '.join(value for value in (reason, counts_text, details, summary) if value)
    presence_counts = _presence_completion_counts(result)
    if presence_counts is not None:
        description = ('Application rules: ' + '; '.join(str(presence_counts[name]) + ' ' + name.replace('_', ' ')
            for name in ('installed', 'removed', 'already_satisfied', 'skipped', 'needs_review', 'unverified'))
            + '. Update installers completed: ' + str(presence_counts['updates_completed']) + '. ' + description)
    if key != 'driver_scan':
        notes = []
        presence = result.get('app_presence')
        groups = [result.get('items'), presence.get('items') if isinstance(presence, dict) else None]
        for rows in groups:
            for row in rows[:1000] if isinstance(rows, list) else []:
                if not isinstance(row, dict) or row.get('status') not in ('failed', 'needs_review', 'skipped', 'cancelled'):
                    continue
                outcome = row.get('result') if isinstance(row.get('result'), dict) else {}
                note = row.get('error') or outcome.get('note') or outcome.get('error')
                if isinstance(note, str) and note.strip() and note not in description:
                    notes.append(str(row.get('id') or 'Application') + ': ' + ' '.join(note.split())[:600])
                if len(notes) >= 2: break
            if len(notes) >= 2: break
        if notes: description += ' Review: ' + '; '.join(notes)
    errors = evidence.get('errors')
    if isinstance(errors, list):
        notes = [str(row.get('error') or '')[:500] for row in errors[:3] if isinstance(row, dict)]
        notes = [note for note in notes if note and note not in description]
        if notes:
            description += ' Details: ' + '; '.join(notes)
    holds = _bounded_update_holds(result)
    if holds:
        count = result.get('held_count')
        count = count if type(count) is int and count >= len(holds) else len(holds)
        notes = [str(row.get('id') or 'Application') + ': ' + str(row.get('reason') or 'Saved automatic-update hold.')[:500]
                 for row in holds[:2]]
        description = (str(count) + ' app update' + ('' if count == 1 else 's') + ' held for review. '
                       + '; '.join(notes) + ' ' + description)
    if app_presentation is not None:
        counts = app_presentation['app_counts']
        description = ('App results: ' + '; '.join(str(counts[name]) + ' ' + name for name in ('updated', 'skipped', 'failed', 'unverified'))
                       + '. ' + ('Queue finished; held or failed packages do not block other workers. ' if app_presentation.get('presentation_status') else '')
                       + (str(counts['source_unverified']) + ' additional source offers unverified. '
                          if counts.get('source_unverified') and evidence.get('complete') is not True else '')
                       + description)
    label = ''
    offers = evidence.get('offers_count')
    complete = evidence.get('complete') is True
    if evidence:
        if complete and type(offers) is int and offers >= 0:
            label = str(offers) + (' driver' if key == 'driver_scan' else ' app') + (' update found' if offers == 1 else ' updates found')
        elif evidence.get('status') == 'not_started':
            label = 'Scan not started'
        elif (key == 'app_scan' and type(evidence.get('recognized_offers_count')) is int
              and evidence['recognized_offers_count'] > 0):
            label = str(evidence['recognized_offers_count']) + ' recognized; incomplete'
        else:
            label = 'Availability not confirmed'
    if driver_counts is not None and result.get('installation_blocked') is not True and result.get('blocked_by_prior_installer') is not True:
        label = str(driver_counts['installed']) + ' installed / ' + str(offers) + ' found'
    if presence_counts is not None:
        label = (str(presence_counts['installed']) + ' installed / ' + str(presence_counts['removed'])
                 + ' removed / ' + str(presence_counts['updates_completed']) + ' updates completed')
    items = result.get('items')
    state = {'description': description[:2000], 'current': len(items) if isinstance(items, list) else 0,
             'id': None,
             'scan_label': label}
    # Existing per-installer totals remain distinct from the number discovered.
    if not isinstance(items, list) or not items:
        state['total'] = 0
    if app_presentation is not None:
        state.update(app_presentation)
        deferred = result.get('deferred_ids')
        state['total'] = len(items) + (len(deferred) if isinstance(deferred, list) else 0)
    if driver_counts is not None:
        state.update(driver_counts=driver_counts, total=0)
    if presence_counts is not None:
        state['presence_counts'] = presence_counts
    if evidence:
        state.update(scan_complete=complete, scan_status=str(evidence.get('status') or 'unknown'))
        if type(offers) is int and offers >= 0:
            state['offers_count'] = offers
    if result.get('installation_blocked') is True or result.get('blocked_by_prior_installer') is True:
        block_reason = str(result.get('installation_block_reason') or reason)
        state.update(installation_blocked=True,
                     blocked_by_prior_installer=result.get('blocked_by_prior_installer') is True,
                     installation_block_reason=block_reason[:4000],
                     scan_complete=complete,
                     scan_status=str(evidence.get('status') or 'not_started'))
        state['wait_reason'] = (('Search completed; ' if complete else 'Search needs review; ' if evidence else 'Search not started; ')
                                + 'installation blocked until the previous installer is reviewed. ' + block_reason)[:4000]
    return state


def _app_completion_presentation(result, evidence):
    """Presentation only: audit status/holds and mutation gates stay unchanged."""
    from .update_holds import is_known_update_hold
    rows = result.get('items')
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        return None
    counts = dict(updated=0, skipped=0, failed=0, unverified=0, held=0)
    seen = set()
    for row in rows:
        value = row.get('result') if isinstance(row, dict) and isinstance(row.get('result'), dict) else {}
        binding = row.get('binding') if isinstance(row, dict) and isinstance(row.get('binding'), dict) else {}
        identity = (row.get('id') or binding.get('id')) if isinstance(row, dict) else None
        if not isinstance(identity, str) or not identity or identity.casefold() in seen or not can_fail_forward(value):
            counts['unverified'] += 1
            continue
        seen.add(identity.casefold())
        if row.get('status') in ('completed', 'applied') and value.get('installer_started') is True and type(value.get('exitCode')) is int and value['exitCode'] == 0:
            counts['updated'] += 1
        elif row.get('status') == 'skipped':
            counts['skipped'] += 1
            counts['held'] += int(is_known_update_hold(value))
        elif row.get('status') in ('failed', 'needs_review', 'blocked'):
            counts['failed'] += 1
        else:
            counts['unverified'] += 1
    # A queue can finish processing its recognized items while source coverage
    # remains incomplete. Never promote that into a completed/zero-offer scan.
    finished = (bool(evidence) and evidence.get('status') not in ('failed', 'not_started')
        and not counts['unverified'] and installer_barrier(result) is None)
    presence = _presence_completion_counts(result)
    if presence is not None and (presence['unverified'] or presence['needs_review']):
        finished = False
    reported, recognized = evidence.get('source_reported_offers_count'), evidence.get('recognized_offers_count')
    if type(reported) is int and type(recognized) is int and 0 <= recognized <= reported <= 1000000:
        counts['source_unverified'] = reported - recognized
    pending = [result]
    while finished and pending:
        value = pending.pop()
        if isinstance(value, dict):
            if (value.get('cancelled') or value.get('navigation_cancelled') or value.get('navigation_cancelled_items')
                    or value.get('restartNeeded') or value.get('reboot_required') or value.get('uncertain')
                    or value.get('running') is True or value.get('outcomeKnown') is False
                    or value.get('deferred') or value.get('deferred_ids') or value.get('stop_reason')
                    or value.get('installation_blocked') or value.get('blocked_by_prior_installer')
                    or value.get('status') in ('deferred', 'cancelled', 'uncertain', 'running', 'installing', 'prepared')):
                finished = False
                break
            pending.extend(item for key, item in value.items() if key not in ('scan', 'app_scan', 'driver_scan', 'backup'))
        elif isinstance(value, list):
            pending.extend(value)
    state = {'app_counts': counts, 'worker_finished': finished, 'continuation_blocked': bool(installer_barrier(result))}
    if finished and (counts['skipped'] or counts['failed']):
        state['presentation_status'] = 'finished_with_skips'
    return state


def _presence_completion_counts(result):
    presence = result.get('app_presence')
    rows = presence.get('items') if isinstance(presence, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    counts = dict(installed=0, removed=0, already_satisfied=0, skipped=0,
                  needs_review=0, unverified=0, updates_completed=0)
    for row in rows[:100]:
        if not isinstance(row, dict):
            counts['unverified'] += 1
            continue
        value = row.get('result') if isinstance(row.get('result'), dict) else {}
        known = value.get('outcomeKnown') is True and value.get('running') is False
        if row.get('status') == 'completed':
            if known and value.get('installer_started') is True and value.get('verified') is True and row.get('presence') in ('keep_installed', 'keep_uninstalled'):
                counts['installed' if row['presence'] == 'keep_installed' else 'removed'] += 1
            else:
                counts['unverified'] += 1
        elif row.get('status') == 'already_satisfied' and known and value.get('installer_started') is False:
            counts['already_satisfied'] += 1
        elif row.get('status') in ('skipped', 'cancelled') and known and value.get('installer_started') is False:
            counts['skipped'] += 1
        else:
            counts['needs_review' if known else 'unverified'] += 1
    for row in result.get('items', [])[:1000] if isinstance(result.get('items'), list) else []:
        if not isinstance(row, dict):
            continue
        value = row.get('result') if isinstance(row.get('result'), dict) else {}
        if (row.get('status') == 'completed' and value.get('outcomeKnown') is True
                and value.get('running') is False and value.get('installer_started') is True
                and type(value.get('exitCode')) is int and value['exitCode'] in (0, 3010, 1641)):
            counts['updates_completed'] += 1
    return counts


def update_completion_status(result):
    if result.get('cancelled'):
        return 'cancelled'
    if result.get('navigation_cancelled') is True or result.get('navigation_cancelled_items'):
        return 'cancelled'
    evidence = next((result[key] for key in ('driver_scan', 'app_scan') if isinstance(result.get(key), dict)), {})
    driver_counts = (_driver_completion_counts(result, evidence)
                     if isinstance(result.get('driver_scan'), dict) else None)
    if installer_barrier(result) or result.get('failed_items') or evidence.get('status') in ('failed', 'incomplete'):
        return 'needs_review'
    if driver_counts is not None and (driver_counts['failed'] or driver_counts['unverified']):
        return 'needs_review'
    if evidence and evidence.get('complete') is not True:
        return 'deferred'
    if evidence and (result.get('skipped') is True or result.get('deferred') or result.get('deferred_ids')):
        return 'deferred'
    if driver_counts is not None and any(driver_counts[key] for key in ('skipped', 'deferred', 'not_processed')):
        return 'deferred'
    return 'completed'


def record_installer_outcome(backend, run_id, title, result):
    """Keep a serial driver's/security install ambiguity from disappearing next run."""
    if not isinstance(result, dict):
        result = {'failed_items': 1, 'uncertain': True, 'items': [],
                  'stop_reason': 'The installer stage failed without an established outcome.'}
    uncertain = _uncertain(result, native=getattr(backend, 'system', 'win32') != 'win32')
    if not uncertain and not _restart_needed(result):
        return
    evidence = ParallelAppUpdates(backend, run_id, title, None, None)
    evidence.record.update(status='uncertain' if uncertain else 'needs_review',
        installer_prepared=True, finished_at=time.time(), result=_journal_result(result), serial=True)
    evidence._write()


class ParallelAppUpdates:
    def __init__(self, backend, parent_run_id, title, operation, mux, *, cancel_event=None, isolate_bridge=False):
        if not re.fullmatch('[a-f0-9]{32}', str(parent_run_id)):
            raise ValueError('A current workflow identifier is required.')
        self.backend, self.title, self.operation = backend, title, operation
        self.job_id = ('driver_updates' if title in DRIVER_TITLES else
                       'windows_updates' if title == 'Check or install Windows security updates' else 'software_updates')
        from .navigation import cursor_for
        self.navigation = cursor_for(backend, self.job_id, title)
        self.mux = mux.for_job(self.job_id) if hasattr(mux, 'for_job') else mux
        self.isolate_bridge = isolate_bridge
        self.directory = Path(backend.data_dir) / 'parallel-updates'
        self.path = self.directory / (uuid.uuid4().hex + '.json')
        from .speed_enhancements import _current_owner
        self.owner = _current_owner()
        self.record = {'schema': 1, 'id': self.path.stem, 'parent_run_id': parent_run_id,
                       'owner': self.owner, 'status': 'prepared', 'started_at': time.time(),
                       'title': title, 'installer_prepared': False,
                       'note': 'No automatic retry is authorized by this record. An unresolved installer record requires manual review; preserve this evidence until installer state is established.'}
        self.result = None
        self.thread = None
        self.stage = {'title': self.title, 'name': self.title, 'status': 'running',
                      'started_at': self.record['started_at'], 'background': True,
                      'worker_log_path': str(self.path)}
        self.joined = False
        self.stop_event = threading.Event()
        self.concurrent_preparation = False
        self.prior_barrier = None
        class Cancellation:
            def is_set(inner):
                return self.stop_event.is_set() or cancel_event is not None and cancel_event.is_set()
        self.cancel_event = Cancellation()

    def cancel(self):
        self.stop_event.set()

    def _write(self):
        from .speed_enhancements import _private_json
        _ordinary_data_path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        _ordinary_data_path(self.path, file=True)
        _private_json(self.path, self.record)

    def _pending(self):
        _ordinary_data_path(self.directory)
        if not self.directory.exists():
            return None
        paths = list(self.directory.glob('*.json'))
        if len(paths) > 2000:
            return 'Application update history requires review before another background installer.'
        restart_checked, restart_clear = False, False
        for path in paths:
            info = _ordinary_data_path(path, file=True)
            if info is None or info.st_size > 8 * 1024 * 1024:
                return 'An application update journal is unreadable; verify installer state first.'
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return 'An application update journal is unreadable; verify installer state first.'
            if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('owner') != self.owner
                    or value.get('id') != path.stem):
                return 'Application update history ownership is unknown; verify installer state first.'
            if (value.get('status') in ('prepared', 'running', 'uncertain')
                    and value.get('installer_prepared', True) is not False):
                    return ('A previous update installer has an unresolved outcome; no automatic retry was started. '
                        'Review installer state and the saved record before continuing: ' + str(path))
            if value.get('status') == 'needs_review' and isinstance(value.get('result'), dict) and value['result'].get('restartNeeded'):
                if not restart_checked:
                    restart_checked = True
                    try:
                        security = self.backend.security_status()
                    except Exception:
                        security = None
                    restart_clear = isinstance(security, dict) and security.get('reboot_required') is False
                if not restart_clear:
                    return 'A previous update requested a restart; its cleared restart state has not been verified.'
        return None

    def start(self, *, inline=False, prior_barrier=None):
        reason = prior_barrier or self._pending()
        self.prior_barrier = reason
        self.stage = {'title': self.title, 'name': self.title, 'status': 'running',
                      'started_at': self.record['started_at'], 'background': True,
                      'worker_log_path': str(self.path)}
        self._write()  # Prepared evidence is durable before the thread can mutate.
        if self.navigation is not None:
            self.navigation.receipt_callback = self.item_receipt
        # Only coordinated operations expose a mandatory before-install gate.
        # They may search under an old installer barrier; unphased/serial calls
        # retain their original refusal before executing an arbitrary operation.
        if reason and not self.concurrent_preparation:
            self.defer(reason)
            return self.stage
        self.mux.background('pending', {'description': ('Driver updates queued in the background installer lane.'
                            if self.title in DRIVER_TITLES else 'Application updates queued in the background installer lane.')})
        if inline:
            return self.stage
        self.thread = threading.Thread(target=self._run, name='Optimus background updater', daemon=False)
        try:
            self.thread.start()
        except BaseException:
            self.thread = None
            self.record.update(status='deferred', finished_at=time.time())
            self._write()
            raise
        return self.stage

    def defer(self, reason, *, cancelled=False):
        self.result = {'items': [], 'failed_items': 0, 'review_only': True, 'skipped': True,
                       'requiresReview': not cancelled, 'cancelled': cancelled,
                       'installer_started': False, 'outcomeKnown': True,
                       'blocked_by_prior_installer': not cancelled, 'uncertain': not cancelled,
                       'installation_blocked': not cancelled, 'installation_block_reason': reason,
                       'stop_reason': reason, 'note': reason}
        # A guarded deferral did not launch an installer, but retain the
        # barrier itself in the result so the workflow cannot clear it.
        self.record.update(status='deferred', result=self.result, finished_at=time.time())
        self._write()
        self.mux.background('cancelled' if cancelled else 'deferred', update_completion_state(self.result))
        if self.navigation is not None:
            self.navigation.end('cancelled' if cancelled else 'deferred')

    def item_progress(self, state):
        state = state if isinstance(state, dict) else {'description': str(state)}
        if 'id' not in state and isinstance(state.get('binding'), dict):
            state = dict(state, id=state['binding'].get('id'))
        backing_up = self.title in DRIVER_TITLES and state.get('phase') == 'backup'
        if self.title in DRIVER_TITLES and not backing_up and type(state.get('current')) is int:
            state = dict(state, current=max(0, state['current'] - 1))
        self.record['current_item'] = {key: state[key] for key in ('id', 'description', 'current', 'total', 'item', 'phase') if key in state}
        if not backing_up:
            self.record['installer_prepared'] = True
        self._write()  # Existing sequential helpers emit this before each installer.
        self.mux.background('preparing' if backing_up else 'installing' if self.concurrent_preparation else 'running', state)

    def item_receipt(self, row):
        """Persist the actual terminal item before navigation advances its lane."""
        compact = _journal_result({'items': [row]})['items']
        if len(compact) != 1:
            raise ValueError('The terminal updater item receipt is invalid.')
        history = self.record.setdefault('terminal_items', [])
        if len(history) >= 1000:
            raise RuntimeError('The bounded updater receipt limit was reached; review before continuing.')
        history.append(compact[0])
        self._write()

    def _run(self):
        from .runtime_priority import lower_worker_priority
        restore_priority = lower_worker_priority()
        worker = None
        worker_kind = 'driver' if self.title in DRIVER_TITLES else 'application'
        try:
            self.record['status'] = 'running'
            self.record['execution_started_at'] = time.time()
            self._write()
            phase = 'scanning' if self.concurrent_preparation else 'running'
            self.mux.background(phase, {'description': 'Checking approved driver updates.'
                                if self.title in DRIVER_TITLES else 'Checking approved application updates.'})
            worker = isolated_backend(self.backend, isolate_bridge=self.isolate_bridge)
            worker._optimus_lane_cancel_event = self.cancel_event
            self.result = self.operation(worker, self.item_progress)
            if not isinstance(self.result, dict):
                raise ValueError('The ' + worker_kind + ' updater returned an unknown result.')
        except BaseException as error:
            self.result = {'items': [], 'failed_items': 1,
                           'uncertain': self.record['installer_prepared'],
                           'stop_reason': ('The ' + worker_kind + ' worker raised an error; verify installer state before continuing.'
                                           if self.record['installer_prepared'] else
                                           'The ' + worker_kind + ' worker failed before any installer was prepared; review the saved error.'),
                           'error': str(error)[:4000]}
            if not self.record['installer_prepared'] or (getattr(error, 'installer_started', None) is False
                                                        and getattr(error, 'outcomeKnown', None) is True):
                self.result.update(installer_started=False, outcomeKnown=True, running=False, uncertain=False)
            for key in ('driver_scan', 'app_scan'):
                evidence = getattr(error, key, None)
                if isinstance(evidence, dict):
                    self.result[key] = _bounded_scan_evidence(evidence)
            if isinstance(getattr(error, 'app_presence', None), dict):
                self.result['app_presence'] = _journal_result(error.app_presence)
        finally:
            if worker is not None and getattr(worker, 'bridge', None) is not getattr(self.backend, 'bridge', None):
                # Never close the shared backend (its sampler/controller belongs
                # to the foreground). A bridge with pending work stays alive.
                process = getattr(worker.bridge, '_process', None)
                if process is not None:
                    self.record['worker_pid'] = process.pid
                try:
                    worker.bridge.close()
                except Exception:
                    pass
            restore_priority()
        block_reason = self.prior_barrier or (getattr(self, 'gate_refusal', None)
                                             if not self.cancel_event.is_set() else None)
        if self.concurrent_preparation and block_reason:
            # Attach this before the first terminal event and durable receipt,
            # including zero-offer and review-only searches that never call the
            # install gate. Fresh scan evidence never resolves an old installer.
            self.result.update(blocked_by_prior_installer=True, installation_blocked=True,
                               installation_block_reason=block_reason, stop_reason=block_reason,
                               requiresReview=True)
            if not self.record['installer_prepared']:
                self.result.setdefault('installer_started', False)
                self.result.setdefault('outcomeKnown', True)
        native = getattr(self.backend, 'system', 'win32') != 'win32'
        try:
            journal_result = _journal_result(self.result)
            self.record.update(status='uncertain' if _uncertain(self.result, native=native) else
                               'needs_review' if update_completion_status(self.result) == 'needs_review' else 'completed',
                               finished_at=time.time(), result=journal_result)
            self._write()
        except BaseException as error:
            self.result = dict(self.result, failed_items=max(1, self.result.get('failed_items', 0)), uncertain=True,
                               stop_reason='The application outcome journal could not be saved; verify installer state before continuing.',
                               journal_error=str(error)[:2000])
        self.mux.background(update_completion_status(self.result), update_completion_state(self.result))
        if self.navigation is not None:
            if self.navigation.current is not None:
                self.navigation.finish(update_completion_status(self.result),
                    outcome_known=not _uncertain(self.result, native=native))
            self.navigation.end(update_completion_status(self.result))

    def join(self, *, next_stage=None):
        if self.joined:
            return self.stage
        if self.thread is not None:
            if self.thread.is_alive():
                self.mux.wait_for_updates(next_stage)
            while self.thread.is_alive():
                self.thread.join(timeout=1)
                if self.thread.is_alive():
                    self.mux.heartbeat()
            self.thread.join()  # Never release the workflow lease before this returns.
        result = self.result or {'items': [], 'failed_items': 1, 'uncertain': True,
                                 'stop_reason': 'The application worker did not establish an outcome.'}
        status = 'cancelled' if result.get('cancelled') or result.get('navigation_cancelled') or result.get('navigation_cancelled_items') else 'failed' if result.get('failed_items') else 'completed'
        self.stage.update(status=status,
                          started_at=self.record.get('execution_started_at', self.record['started_at']),
                          finished_at=self.record.get('finished_at', time.time()), result=result)
        self.joined = True
        if status == 'failed':
            self.stage['error'] = 'One or more application update checks failed; review the saved item results.'
        self.mux.background(update_completion_status(result), update_completion_state(result))
        return self.stage


class ParallelInstallerQueue:
    """Start discovery together; grant mutations after cleanup, one job at a time.

    Operations receive (backend, item_callback, prepared_callback, before_install).
    The installer slot remains owned until the final outcome is durable, including
    error handling. Merely preparing or waiting never records installer intent.
    """
    def __init__(self, backend, parent_run_id, operations, mux, *, cancel_event=None, prior_barrier=None):
        self.backend, self.mux = backend, mux
        self.joined = False
        self.prior_barrier = prior_barrier
        self.barrier = prior_barrier
        self.condition = threading.Condition()
        self.maintenance_ready = False
        self.active_job = None
        self.stop_event = threading.Event()
        queue = self
        class Cancellation:
            def is_set(self):
                return queue.stop_event.is_set() or cancel_event is not None and cancel_event.is_set()
        self.cancel_event = Cancellation()
        self.jobs = [ParallelAppUpdates(backend, parent_run_id, title, operation, mux,
                     cancel_event=self.cancel_event, isolate_bridge=True)
                     for title, operation in operations]
        if not self.jobs:
            raise ValueError('Choose at least one configured background update job.')
        for job in self.jobs:
            job.concurrent_preparation = True
            job.prepared = threading.Event()
            job.will_install = False
            job.preparation_bridge = None
            job.gate_refusal = None
            operation = job.operation
            def coordinated(worker, item_callback, *, job=job, operation=operation):
                job.preparation_bridge = getattr(worker, 'bridge', None)
                return operation(worker, item_callback,
                    lambda state: self._prepared(job, state),
                    lambda state: self._before_install(job, state))
            job.operation = coordinated

    def start(self, *, inline=False):
        stages = []
        # Every job is durably prepared before ANY worker starts discovery.
        try:
            for job in self.jobs:
                stages.append(job.start(inline=True, prior_barrier=self.barrier))
                # Discovering a pending journal here is as binding as a barrier
                # supplied by the workflow. Capture it before ANY thread starts.
                self.barrier = self.barrier or job.prior_barrier
            self.prior_barrier = self.barrier
            for job in self.jobs:
                # A journal can become visible while the remaining jobs are
                # being prepared. Even a zero-offer/review-only first search
                # must report the final shared installation barrier.
                job.prior_barrier = self.prior_barrier
            if inline and not self.barrier:
                raise ValueError('Inline discovery requires a closed installer gate.')
            for job in self.jobs:
                if job.result is not None:
                    job.prepared.set()
                    continue
                if inline:
                    self._run(job)
                    continue
                job.thread = threading.Thread(target=self._run, args=(job,),
                    name='Optimus ' + job.job_id, daemon=False)
                try:
                    job.thread.start()
                except BaseException:
                    job.thread = None
                    raise
        except BaseException:
            self.cancel()
            for job in self.jobs:
                if job.thread is None and job.result is None:
                    try:
                        job.defer('The background worker could not start; no installer was launched.', cancelled=True)
                    except BaseException:
                        # A failed preparation write cannot be repaired by
                        # writing again. The in-memory known no-launch result
                        # still permits joining safely and releasing the lease.
                        pass
                    finally:
                        job.prepared.set()
            self.join()
            raise
        return stages

    def _prepared(self, job, state):
        state = state if isinstance(state, dict) else {}
        with self.condition:
            if job.prepared.is_set():
                return
            job.will_install = bool(state.get('candidate_count')) and not state.get('review_only', True)
            job.prepared.set()
            self.condition.notify_all()
        if job.will_install and not self.barrier:
            job.mux.background('waiting_for_maintenance', {
                'description': 'Update discovery finished; waiting for initial resource checks and selected temporary-file cleanup.',
                'total': state.get('candidate_count', 0)})

    def _before_install(self, job, state):
        self._prepared(job, state)
        phase = None
        with self.condition:
            while True:
                if job.cancel_event.is_set():
                    job.gate_refusal = 'Cancelled before installation; no installer was launched.'
                    return False
                if job.navigation is not None and job.navigation.local_requested():
                    return False  # This exact local item does not close another lane's gate.
                if self.barrier:
                    job.gate_refusal = self.barrier
                    return False
                if self.active_job is job:
                    # Presence rules and later updates share one job-owned
                    # installer slot. Reentry must retain that ownership after
                    # fresh cancellation/barrier checks, rather than deadlock.
                    return True
                if not self.maintenance_ready:
                    current = 'waiting_for_maintenance'
                    description = 'Discovery finished; waiting for initial resource checks and selected temporary-file cleanup.'
                else:
                    # If both are ready at the checkpoint, retain app-first
                    # ordering. A ready driver need not await an app still scanning.
                    earlier_ready = any(other is not job and other.will_install and other.result is None
                                        for other in self.jobs[:self.jobs.index(job)])
                    if self.active_job is None and not earlier_ready:
                        self.active_job = job
                        job.mux.background('preparing', {'description': 'Installer slot acquired; rechecking approved scope and current readiness.'})
                        return True
                    current = 'waiting_for_installer'
                    description = 'Discovery finished; waiting for the other update installer and its verified outcome. Maintenance can continue.'
                if current != phase:
                    phase = current
                    job.mux.background(current, {'description': description})
                self.condition.wait(timeout=0.2)

    def _run(self, job):
        try:
            job._run()
        finally:
            with self.condition:
                # _run saves the final journal before relinquishing the slot.
                # A failed save produces an uncertain result and closes the gate.
                self.barrier = self.barrier or installer_barrier(job.result)
                job.will_install = False
                job.prepared.set()  # Failures/empty/review paths also unblock cleanup.
                if self.active_job is job:
                    self.active_job = None
                self.condition.notify_all()

    def wait_prepared(self):
        """Drain discovery before touching its shared TEMP/cache staging area."""
        if self.prior_barrier:
            # Cleanup is already forbidden by this durable installer barrier.
            # Return it now so independent maintenance need not await another
            # read-only search. The closed gate and final join still protect
            # installer staging files and retain ownership until workers drain.
            return self.prior_barrier
        if any(not job.prepared.is_set() for job in self.jobs):
            self.mux.wait_for_preparation()
        while any(not job.prepared.wait(timeout=0.2) for job in self.jobs):
            self.mux.heartbeat()
        # A bridge timeout does not cancel its native request. Do not delete
        # staging files or start another mutation while that discovery survives.
        for job in self.jobs:
            bridge = job.preparation_bridge
            from .bridge import PowerShellBridge
            if type(bridge) is PowerShellBridge:
                with bridge._pending_lock:
                    if bridge._pending:
                        return 'Update discovery is still running in its Windows engine after a timeout; temporary-file cleanup and installers were deferred.'
        # Completion of a read-only search cannot authorize cleanup of another
        # installer's staging files or clear an unresolved installer receipt.
        return self.prior_barrier

    def release_maintenance(self, barrier=None):
        with self.condition:
            self.barrier = self.barrier or barrier
            self.maintenance_ready = True
            self.condition.notify_all()

    def cancel(self):
        self.stop_event.set()
        for job in self.jobs:
            job.cancel()
        with self.condition:
            self.condition.notify_all()

    def join(self, *, next_stage=None):
        if not self.joined:
            if any(job.thread is not None and job.thread.is_alive() for job in self.jobs):
                self.mux.wait_for_updates(next_stage)
            for job in self.jobs:
                if job.thread is not None:
                    while job.thread.is_alive():
                        job.thread.join(timeout=0.5)
                        if job.thread.is_alive():
                            self.mux.heartbeat()
                    job.thread.join()
            self.joined = True
        return [job.join() for job in self.jobs]


def ready_maintenance_stages(stages, queue):
    """Use catalog wait time for already-approved independent stages.

    Reconsider readiness between foreground stages. Cleanup retains its native
    discovery checkpoint; installers still wait until cleanup has returned.
    """
    pending = list(stages)
    if pending:
        yield pending.pop(0)  # Preserve the initial before-inventory boundary.
    while pending:
        index = 0
        if (queue is not None and not queue.prior_barrier and pending[0][0] in BEFORE_UPDATES
                and any(not job.prepared.is_set() for job in queue.jobs)):
            index = next((i for i, stage in enumerate(pending) if independent_stage(stage[0])), 0)
        yield pending.pop(index)


def parallel_installer_stages(stages, titles):
    """Clean before installers; drain safe work before joining the updater."""
    operations = [row for title in titles for row in stages if row[0] == title]
    if not operations:
        return stages, [], None
    remaining = [row for row in stages[1:] if row[0] not in titles]
    cleanup = [row for row in remaining if row[0] in BEFORE_UPDATES]
    independent = [row for row in remaining if independent_stage(row[0])]
    queued = [row for row in remaining if row[0] not in BEFORE_UPDATES and not independent_stage(row[0])]
    launch_after = (cleanup[-1] if cleanup else stages[0])[0]
    return [stages[0], *cleanup, *independent, *queued], operations, launch_after


def parallel_stages(stages, software_title):
    planned, operations, launch_after = parallel_installer_stages(stages, (software_title,))
    return planned, operations[0] if operations else None, launch_after
