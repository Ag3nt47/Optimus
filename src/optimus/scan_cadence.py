"""Conservative reuse of freshly observed native Defender scan evidence.

No cache, scan, protection change or clean-system claim is made here. Native
timestamps are insufficient alone: an exact completed lifecycle and complete
current protection/threat observations are required. Uncertainty means scan.

https://learn.microsoft.com/powershell/module/defender/get-mpcomputerstatus
https://learn.microsoft.com/previous-versions/windows/desktop/defender/msft-mpcomputerstatus
https://learn.microsoft.com/defender-endpoint/troubleshoot-microsoft-defender-antivirus
https://learn.microsoft.com/windows/win32/lwef/mpthreat-status
https://learn.microsoft.com/previous-versions/windows/desktop/defender/msft-mpthreatdetection
"""
from __future__ import annotations

from datetime import datetime
import math
import re
import time
import uuid

_VERSION = re.compile(r'[0-9]+(?:\.[0-9]+){3}\Z')
_PROVIDER = re.compile(r'(?:Microsoft Defender|Windows Defender|Windows Security)(?: Antivirus)?\Z', re.I)
_MAX_EVENTS = 256


def validate_reuse_hours(value):
    if type(value) is not int or not 0 <= value <= 24:
        raise ValueError('Defender scan reuse must be disabled (0) or an integer from 1 to 24 hours.')
    return value


def _epoch(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def recent_quick_scan(evidence, reuse_hours, *, now=None):
    """Return reusable metadata or an explicit reason to run a fresh scan."""
    validate_reuse_hours(reuse_hours)
    def fresh(reason):
        return {'reuse': False, 'reason': reason}
    if not reuse_hours:
        return fresh('Recent-scan reuse is disabled; request a fresh scan.')
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        return fresh('The current time cannot be verified.')
    if (not isinstance(evidence, dict) or evidence.get('ok') is not True
            or type(evidence.get('schema')) is not int or evidence['schema'] != 1):
        return fresh('Native recent-scan observations are unavailable.')
    observed = _epoch(evidence.get('observed_at'))
    if observed is None or not 0 <= now - observed <= 120:
        return fresh('The native recent-scan observations are stale or future-dated.')
    defender = evidence.get('defender')
    if (not isinstance(defender, dict)
            or any(defender.get(key) is not True for key in ('available', 'service_enabled',
                'antivirus_enabled', 'antispyware_enabled', 'realtime_enabled',
                'on_access_enabled', 'behavior_enabled', 'ioav_enabled'))
            or defender.get('running_mode') != 'Normal'
            or defender.get('quick_scan_overdue') is not False
            or defender.get('signatures_out_of_date') is not False):
        return fresh('Active real-time protection, current definitions or non-overdue scan state is unverified.')
    providers = evidence.get('antivirus_providers')
    if (not isinstance(providers, dict) or providers.get('complete') is not True
            or type(providers.get('items')) is not list or not 1 <= len(providers['items']) <= 8
            or any(not isinstance(value, str) or not _PROVIDER.fullmatch(value) for value in providers['items'])):
        return fresh('The active antivirus-provider inventory needs a fresh scan or review.')
    start, end = _epoch(defender.get('quick_scan_start')), _epoch(defender.get('quick_scan_end'))
    if (start is None or end is None or not 0 < start <= end <= observed
            or observed - start > reuse_hours * 3600):
        return fresh('The last quick-scan timestamps are unknown, future, in progress or outside the reuse window.')
    if defender.get('full_scan_metadata_complete') is not True:
        return fresh('Current full-scan state is unavailable.')
    full_start, full_end = defender.get('full_scan_start'), defender.get('full_scan_end')
    if (full_start, full_end) != ('', ''):
        full_start, full_end = _epoch(full_start), _epoch(full_end)
        if (full_start is None or full_end is None or not 0 < full_start <= full_end <= observed):
            return fresh('A full scan may be active or its completion state is unknown.')
    definitions = []
    for prefix in ('antivirus', 'antispyware'):
        version = defender.get(prefix + '_signature_version')
        updated = _epoch(defender.get(prefix + '_signature_updated'))
        if (not isinstance(version, str) or len(version) > 80 or not _VERSION.fullmatch(version)
                or updated is None or not 0 < updated <= start
                or observed - updated > 86400):
            return fresh('Definitions are unknown, stale or newer than the completed scan.')
        definitions.append(version)
    threats = evidence.get('threats')
    if (not isinstance(threats, dict) or threats.get('complete') is not True
            or type(threats.get('active_count')) is not int or threats['active_count'] != 0
            or threats.get('items') != [] or type(threats.get('items')) is not list
            or threats.get('truncated') is not False
            or threats.get('active_fingerprint_complete') is not True
            or not isinstance(threats.get('active_fingerprint'), str)
            or not re.fullmatch('[0-9a-f]{64}', threats['active_fingerprint'])):
        return fresh('A complete zero-active-threat inventory could not be established.')
    if (threats.get('detections_complete') is not True
            or type(threats.get('detections')) is not list or len(threats['detections']) > 200):
        return fresh('Detection-history coverage or resolved outcomes could not be verified.')
    for row in threats['detections']:
        if (not isinstance(row, dict) or type(row.get('threat_id')) is not int
                or not 0 <= row['threat_id'] <= 2**63-1
                or type(row.get('status_id')) is not int or row['status_id'] not in (2, 3, 4, 5, 6, 106)
                or row.get('action_success') is not True):
            return fresh('Detection history includes an unknown or unresolved outcome.')
        if row['status_id'] == 106:
            # ABANDONED is not a newly remediated or proven safe threat. An old,
            # inactive retained history record may coexist with the later scan;
            # its native initial/status-change dates must both precede that scan.
            initial = _epoch(row.get('initial_detection_time'))
            changed = _epoch(row.get('last_status_change_time'))
            if initial is None or changed is None or not 0 < initial <= changed < start:
                return fresh('Abandoned detection history is newer than the scan or its native timing is unverified.')
    history = evidence.get('lifecycle')
    if not isinstance(history, dict) or history.get('complete') is not True:
        return fresh('Native scan lifecycle history is incomplete.')
    boot, oldest = _epoch(history.get('boot_time')), _epoch(history.get('log_oldest'))
    events = history.get('events')
    if (boot is None or oldest is None or not 0 < oldest <= boot <= start
            or type(events) is not list or not 2 <= len(events) <= _MAX_EVENTS):
        return fresh('The bounded lifecycle history does not cover this boot and scan.')
    parsed, identities = [], set()
    for row in events:
        if not isinstance(row, dict):
            return fresh('A native scan lifecycle record is malformed.')
        record, kind, stamp = row.get('record_id'), row.get('id'), _epoch(row.get('time'))
        try:
            identity = str(uuid.UUID(row.get('scan_id')))
        except (ValueError, TypeError, AttributeError):
            return fresh('A native scan lifecycle identity is unavailable.')
        if (type(record) is not int or not 0 < record <= 2**63-1 or record in identities
                or type(kind) is not int or kind not in (1000, 1001, 1002)
                or identity == '00000000-0000-0000-0000-000000000000'
                or stamp is None or not boot <= stamp <= observed):
            return fresh('A native scan lifecycle record is ambiguous or future-dated.')
        identities.add(record)
        parsed.append((record, kind, identity, stamp))
    latest, fence = history.get('latest_record_id'), history.get('fence_record_id')
    if (type(latest) is not int or type(fence) is not int
            or latest != max(identities) or fence != latest):
        return fresh('Native scan activity changed while the status was read.')
    pending, finished, candidates, previous_time = {}, set(), [], boot
    for record, kind, identity, stamp in sorted(parsed):
        if stamp < previous_time:
            return fresh('Native scan event timestamps are inconsistent.')
        previous_time = stamp
        if kind == 1000:
            if identity in pending or identity in finished:
                return fresh('A native scan lifecycle identity is duplicated.')
            pending[identity] = (record, stamp)
        else:
            if identity not in pending:
                return fresh('A scan outcome has no matching start in the complete history.')
            opened, began = pending.pop(identity)
            finished.add(identity)
            if kind == 1001 and abs(began - start) <= 5 and abs(stamp - end) <= 5:
                candidates.append((identity, opened, record))
    if pending or len(candidates) != 1:
        return fresh('A scan is active, unresolved, cancelled or lacks a unique completed quick-scan lifecycle.')
    identity, opened, completed = candidates[0]
    return {'reuse': True, 'reason': 'A recent completed quick scan remains eligible under fresh native observations.',
        'scan_started_at': defender['quick_scan_start'], 'scan_completed_at': defender['quick_scan_end'],
        'age_seconds': round(now - end, 1), 'max_age_hours': reuse_hours,
        'observed_at': evidence['observed_at'], 'native_scan_id': identity,
        'start_event_record_id': opened, 'completion_event_record_id': completed,
        'definition_versions': {'antivirus': definitions[0], 'antispyware': definitions[1]},
        'active_threat_count_observed': 0,
        'note': 'This reuses native completion evidence, not a new scan or a clean-system guarantee. Real-time protection remains unchanged.'}
