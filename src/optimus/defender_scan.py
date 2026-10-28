"""Bounded Defender scan display and detection metadata; no native actions."""
from __future__ import annotations

import math
import json
import queue
import re
import subprocess
import threading
import time


SCAN_KEY = 'security:defender:quick_scan'
SCAN_LABEL = 'Virus, malware and spyware scan'
MAX_DETECTIONS = 200
# MSFT_MpThreatDetection.ThreatStatusID, not MpClient MPTHREAT_STATUS.
# https://learn.microsoft.com/previous-versions/windows/desktop/defender/msft-mpthreatdetection
_STATUS_NAMES = {0: 'unknown', 1: 'detected', 2: 'cleaned', 3: 'quarantined',
                 4: 'removed', 5: 'allowed', 6: 'blocked', 102: 'quarantine_failed',
                 103: 'remove_failed', 104: 'allow_failed', 105: 'abandoned', 107: 'block_failed'}


class ScanProgress:
    def __init__(self, callback, *, clock=time.monotonic):
        self.callback, self.clock = callback, clock
        self.started = clock()
        self.closed = False

    def emit(self, status, *, percent=None, source='defender_powershell', verified=False):
        if self.closed:
            return
        if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
            percent = None
        if percent == 100 and not verified:
            percent = None
        item = {'key': SCAN_KEY, 'label': SCAN_LABEL, 'phase': 'scan', 'percent': percent}
        if source == 'defender_cim_progress' and percent is not None:
            item['progress_source'] = source
            if percent not in (0, 100):
                item['estimated'] = True  # The native ProgressRecord estimate, never elapsed time.
        elapsed = max(0.0, min(86400.0, self.clock() - self.started))
        event = {'item_progress': item, 'scan_status': status,
                 'elapsed_seconds': round(elapsed, 1), 'completion_verified': verified,
                 'progress_source': source, 'progress_only': True,
                 'description': SCAN_LABEL + (': completed' if verified else ': ' + status.replace('_', ' '))}
        if percent is None:
            event['description'] += ' — percentage unavailable'
        try:
            if callable(self.callback):
                self.callback(event)
        except Exception:
            pass  # Display failure cannot abandon a native scan or authorize removal.
        if verified or status in ('cancelled', 'blocked', 'incomplete', 'failed'):
            self.closed = True

    def elapsed(self):
        return round(max(0.0, min(86400.0, self.clock() - self.started)), 1)

    def native(self, state):
        if not isinstance(state, dict):
            return
        source = state.get('progress_source', 'defender_powershell')
        percent = None
        if source == 'defender_cim_progress':
            value = state.get('native_percent')
            percent = value if type(value) is int and 0 <= value < 100 else None
        self.emit('provider_scan_progress' if percent is not None else 'waiting_for_defender',
                  percent=percent, source=source)


def _identifier(value):
    return type(value) is int and 0 <= value <= 2 ** 63 - 1


def threat_snapshot(value):
    """Never turn historical quarantine or unavailable inventory into clean."""
    value = value if isinstance(value, dict) else {}
    count = value.get('active_count')
    complete = value.get('complete') is True and type(count) is int and 0 <= count <= 1000000
    items = []
    for raw in value.get('items', [])[:MAX_DETECTIONS] if isinstance(value.get('items'), list) else []:
        if not isinstance(raw, dict) or not _identifier(raw.get('id')) or type(raw.get('active')) is not bool:
            continue
        name = raw.get('name')
        name = ' '.join(name.split())[:256] if isinstance(name, str) else 'Defender detection'
        item = {'id': raw['id'], 'name': name, 'active': raw['active']}
        if type(raw.get('resource_count')) is int and 0 <= raw['resource_count'] <= 2000:
            item['resource_count'] = raw['resource_count']
        if isinstance(raw.get('resource_fingerprint'), str) and re.fullmatch('[0-9a-f]{64}', raw['resource_fingerprint']):
            item['resource_fingerprint'] = raw['resource_fingerprint']
        items.append(item)
    active_ids = sorted({item['id'] for item in items if item['active']})
    # An exact all-active removal scope needs every displayed active identity.
    identities_complete = complete and not value.get('truncated') and len(active_ids) == count
    detections = []
    rows = value.get('detections')
    for raw in rows[:MAX_DETECTIONS] if isinstance(rows, list) else []:
        if not isinstance(raw, dict) or not _identifier(raw.get('threat_id')):
            continue
        status = raw.get('status_id')
        status = status if type(status) is int and 0 <= status <= 255 else None
        row = {'threat_id': raw['threat_id'], 'status_id': status,
               'status': _STATUS_NAMES.get(status, 'unknown'),
               'action_success': raw.get('action_success') if type(raw.get('action_success')) is bool else None}
        detections.append(row)
    fingerprint = value.get('active_fingerprint')
    fingerprint = fingerprint if isinstance(fingerprint, str) and re.fullmatch('[0-9a-f]{64}', fingerprint) else None
    return {'complete': complete, 'active_count': count if complete else None,
            'active_ids': active_ids, 'active_ids_complete': identities_complete,
            'active_fingerprint': fingerprint,
            'active_fingerprint_complete': value.get('active_fingerprint_complete') is True and identities_complete and fingerprint is not None,
            'items': items, 'detections': detections,
            'detections_complete': value.get('detections_complete') is True and isinstance(rows, list) and len(rows) <= MAX_DETECTIONS,
            'truncated': value.get('truncated') is True,
            'scope': 'Defender current threats and bounded retained detection history; records may predate this scan.',
            'review_action': 'review_security', 'removal_requires_confirmation': True}


def validate_remediation_consent(consent, snapshot):
    if (not isinstance(consent, dict) or set(consent) != {'confirmed', 'scope', 'threat_ids', 'resource_fingerprint'}
            or consent['confirmed'] is not True or consent['scope'] != 'all_current_active_defender_threats'
            or not isinstance(consent['threat_ids'], list) or not 0 < len(consent['threat_ids']) <= MAX_DETECTIONS
            or any(not _identifier(value) for value in consent['threat_ids'])
            or len(consent['threat_ids']) != len(set(consent['threat_ids']))
            or snapshot.get('active_ids_complete') is not True
            or snapshot.get('active_fingerprint_complete') is not True
            or not isinstance(consent['resource_fingerprint'], str)
            or not re.fullmatch('[0-9a-f]{64}', consent['resource_fingerprint'])
            or consent['resource_fingerprint'] != snapshot.get('active_fingerprint')
            or sorted(consent['threat_ids']) != snapshot.get('active_ids')):
        return None
    return {'confirmed': True, 'scope': consent['scope'], 'threat_ids': sorted(consent['threat_ids']),
            'resource_fingerprint': consent['resource_fingerprint']}


def read_scan_process(process, *, timeout, cancel_event=None, progress_callback=None, clock=time.monotonic):
    """Read only this owned child's bounded JSONL; never stop Defender itself."""
    incoming = queue.Queue(maxsize=64)
    finished, invalid = threading.Event(), threading.Event()

    def read():
        total = 0
        try:
            while True:
                line = process.stdout.readline(1024 * 1024 + 1)
                if not line:
                    break
                total += len(line.encode('utf-8'))
                if len(line) > 1024 * 1024 or total > 1024 * 1024:
                    invalid.set(); break
                incoming.put_nowait(line)
        except (OSError, ValueError, queue.Full):
            invalid.set()
        finally:
            finished.set()

    reader = threading.Thread(target=read, name='DefenderScanOutput', daemon=True)
    reader.start()
    deadline, next_tick = clock() + timeout, clock()
    terminal = None
    job_id, last_percent, numeric_disabled = None, -1, False
    latest = {'progress_source': 'defender_powershell'}

    def retire():
        if process.poll() is None:
            process.terminate()  # The owned PowerShell client only.
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=3)
        reader.join(timeout=1)

    try:
        while True:
            cancelled = cancel_event is not None and cancel_event.is_set()
            expired = clock() >= deadline
            if cancelled or expired or invalid.is_set():
                retire()
                return {'ok': False, 'cancelled': cancelled, 'timed_out': expired,
                        'may_continue': True, 'error': 'Stopped waiting for Defender; Windows may continue its scan.'}
            try:
                line = incoming.get(timeout=0.1)
            except queue.Empty:
                line = None
            if line is not None:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('Invalid Defender scan frame.')
                if value.get('kind') == 'defender_scan_progress':
                    identity = value.get('scan_job_id')
                    if (not isinstance(identity, str) or identity == '00000000-0000-0000-0000-000000000000'
                            or re.fullmatch('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', identity) is None
                            or job_id is not None and identity != job_id or terminal is not None):
                        raise ValueError('Changed Defender job identity.')
                    job_id = identity
                    percent = value.get('percent')
                    if type(percent) is int and 0 <= percent < 100 and not numeric_disabled:
                        if percent < last_percent:
                            numeric_disabled = True; percent = None
                        else:
                            last_percent = percent
                    else:
                        percent = None
                    latest = {'progress_source': 'defender_cim_progress', 'native_percent': percent}
                    next_tick = 0
                else:
                    if terminal is not None or type(value.get('ok')) is not bool:
                        raise ValueError('Invalid terminal Defender result.')
                    terminal = value
                    if job_id is not None and terminal.get('scan_job_id') != job_id:
                        raise ValueError('Defender completion belongs to a different job.')
            if clock() >= next_tick and callable(progress_callback) and terminal is None:
                try:
                    progress_callback(dict(latest))
                except Exception:
                    pass
                next_tick = clock() + 1
            if process.poll() is not None and finished.is_set() and incoming.empty():
                break
        reader.join(timeout=1)
        if terminal is None or process.returncode != 0 and terminal.get('ok') is not False:
            return {'ok': False, 'may_continue': True, 'error': 'Defender returned no verified terminal result.'}
        terminal['numeric_progress_observed'] = last_percent >= 0
        return terminal
    except (ValueError, TypeError, json.JSONDecodeError):
        retire()
        return {'ok': False, 'may_continue': True, 'error': 'Defender scan output was incomplete or changed; review Windows Security.'}
    finally:
        if finished.is_set():
            process.stdout.close()
