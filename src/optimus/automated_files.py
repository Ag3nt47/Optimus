"""Saved-scope duplicate policies; recycling always uses fresh backend approvals."""
from __future__ import annotations
import hashlib
import math
import ntpath
from pathlib import Path
import re
import stat
import time
from .automation import CURRENT_SETUP_VERSION

_POLICIES = frozenset(('review', 'keep_oldest', 'keep_newest'))
_TICKS_UNIX = 621355968000000000
_CLOUD_BYTES = 256 * 1024 * 1024


def _result(scope):
    return {'items': [], 'skips': [], 'failed_items': 0, 'recycled_count': 0, 'approved_scope': scope,
            'cancelled': False, 'review_only': True}


def _cancelled(event):
    return event is not None and event.is_set()


def _policy(config, prefix):
    policy = config.get(prefix + '_policy', 'review')
    if policy not in _POLICIES:
        raise ValueError('Unknown saved duplicate policy.')
    maximum = config.get(prefix + '_max_recycle', 25)
    age = config.get(prefix + '_min_age_days', 7)
    if type(maximum) is not int or not 1 <= maximum <= 500 or type(age) is not int or not 1 <= age <= 3650:
        raise ValueError('Invalid saved recycle count or minimum age.')
    approved = config.get(prefix + '_changes_approved', False)
    if type(approved) is not bool:
        raise ValueError('Saved duplicate approval must be an explicit boolean.')
    return policy, maximum, age, approved


def _normal(path):
    return ntpath.normcase(ntpath.normpath(str(path)))


def _within(path, root):
    return _normal(path).startswith(_normal(root).rstrip('\\') + '\\')


def _field(record, name, default=None):
    if not isinstance(record, dict):
        return default
    return next((value for key, value in record.items() if str(key).casefold() == name.casefold()), default)


def _keeper_valid(record, cancel_event=None):
    """Revalidate the chosen keeper without following placeholders or indirect paths."""
    path = Path(record['FilePath'])
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('The selected keeper or an ancestor became indirect.')
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & (0x1000 | 0x40000 | 0x400000):
        raise ValueError('The chosen keeper is no longer an ordinary local file.')
    if before.st_size != record['Length'] or before.st_mtime_ns // 100 + _TICKS_UNIX != record['LastWriteTimeUtcTicks']:
        raise ValueError('The chosen keeper identity changed.')
    digest = hashlib.sha256()
    deadline = time.monotonic() + 120
    received = 0
    with path.open('rb') as stream:
        while True:
            if _cancelled(cancel_event):
                raise InterruptedError('Keeper verification cancelled.')
            if time.monotonic() >= deadline:
                raise ValueError('Keeper verification time limit reached.')
            chunk = stream.read(min(1024 * 1024, record['Length'] - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > record['Length']:
                raise ValueError('The chosen keeper size changed during verification.')
            digest.update(chunk)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or digest.hexdigest() != record['Fingerprint'].lower():
        raise ValueError('The chosen keeper content or identity changed.')
    return True


def run_local(backend, config, cancel_event=None, item_callback=None):
    policy, maximum, age, approved = _policy(config, 'duplicate')
    roots = config.get('duplicate_roots', [])
    if not isinstance(roots, list) or len(roots) > 20 or any(not isinstance(root, str) or not root for root in roots):
        raise ValueError('Choose a bounded list of saved local folders.')
    result = _result(list(roots))
    result['note'] = 'Only fresh SHA256 duplicate groups in saved folders are considered. One deterministic keeper remains; Windows recycle validation runs again for every selected file. No permanent-delete fallback is used.'
    if config.get('duplicates_enabled') is not True:
        result['skips'].append({'reason': 'Local duplicate maintenance is disabled.'}); return result
    automatic = policy != 'review' and approved and type(config.get('setup_version')) is int and config['setup_version'] == CURRENT_SETUP_VERSION
    result['review_only'] = not automatic
    if policy != 'review' and not automatic:
        result['needs_approval'] = True
    cutoff = time.time_ns() // 100 + _TICKS_UNIX - age * 86400 * 10000000
    attempted, seen_paths, protected_keepers = 0, set(), set()
    for root in roots:
        if _cancelled(cancel_event) or attempted >= maximum:
            break
        try:
            scan = backend.duplicate_scan(root)
            if _normal(_field(scan, 'Root', '')) != _normal(root):
                raise ValueError('The fresh scan does not match the saved approved folder.')
            if not automatic:
                result['items'].append({'root': root, 'status': 'review', 'scan': scan}); continue
            if _field(scan, 'Complete') is not True:
                result['skips'].append({'root': root, 'reason': 'The scan was incomplete; automatic recycling is skipped.'}); continue
            groups = _field(scan, 'Groups', [])
            if not isinstance(groups, list):
                raise ValueError('The duplicate scan returned invalid groups.')
            for group in groups:
                if _cancelled(cancel_event) or attempted >= maximum:
                    break
                records = _field(group, 'Candidates', [])
                if not isinstance(records, list) or len(records) < 2:
                    continue
                fingerprint = _field(group, 'SHA256', '')
                if not isinstance(fingerprint, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', fingerprint):
                    raise ValueError('A duplicate group has no validated SHA256 fingerprint.')
                members = []
                for record in records:
                    if not isinstance(record, dict) or any(key not in record for key in ('FilePath', 'Root', 'ScanId', 'Fingerprint', 'Length', 'LastWriteTimeUtcTicks')):
                        raise ValueError('A duplicate scan record has no complete identity.')
                    if _normal(record['Root']) != _normal(root) or not _within(record['FilePath'], root) or record['ScanId'] != _field(scan, 'ScanId') or str(record['Fingerprint']).lower() != fingerprint.lower() or type(record['LastWriteTimeUtcTicks']) is not int or type(record['Length']) is not int or record['Length'] <= 0:
                        raise ValueError('A duplicate record escaped its approved group or scope.')
                    members.append(record)
                if len({_normal(item['FilePath']) for item in members}) != len(members):
                    raise ValueError('A duplicate group contains repeated paths.')
                members.sort(key=lambda item: ((item['LastWriteTimeUtcTicks'] if policy == 'keep_oldest' else -item['LastWriteTimeUtcTicks']), _normal(item['FilePath'])))
                keeper = members[0]
                protected_keepers.add(_normal(keeper['FilePath']))
                for record in members[1:]:
                    if _cancelled(cancel_event) or attempted >= maximum:
                        break
                    if record['LastWriteTimeUtcTicks'] > cutoff or _normal(record['FilePath']) in seen_paths or _normal(record['FilePath']) in protected_keepers:
                        result['skips'].append({'path': record['FilePath'], 'reason': 'Too recently modified or already considered in an overlapping saved folder.'}); continue
                    try:
                        _keeper_valid(keeper, cancel_event)
                    except (OSError, ValueError) as error:
                        result['skips'].append({'keeper': keeper['FilePath'], 'reason': str(error)}); break
                    attempted += 1
                    seen_paths.add(_normal(record['FilePath']))
                    if item_callback:
                        item_callback({'index': attempted, 'current': attempted, 'total': maximum, 'description': 'Recycling selected duplicate: ' + record['FilePath'], 'maximum': maximum, 'item': record['FilePath'], 'keeper': keeper['FilePath'], 'scope': root, 'action': 'recycle'})
                    if _cancelled(cancel_event):
                        break
                    try:
                        outcome = backend.duplicate_recycle_approved(record)
                        success = _field(outcome, 'Recycled') is True
                        result['items'].append({'path': record['FilePath'], 'keeper': keeper['FilePath'], 'status': 'recycled' if success else 'failed', 'result': outcome})
                        result['recycled_count'] += int(success)
                        result['failed_items'] += int(not success)
                        if not success:
                            result['stopped_after_error'] = True
                            result['cancelled'] = _cancelled(cancel_event)
                            return result
                    except Exception as error:
                        result['items'].append({'path': record['FilePath'], 'status': 'failed', 'error': str(error)})
                        result['failed_items'] += 1
                        # A failed recycle can have an uncertain shell outcome; never replay it.
                        result['stopped_after_error'] = True
                        result['cancelled'] = _cancelled(cancel_event)
                        return result
        except Exception as error:
            result['failed_items'] += 1
            result['items'].append({'root': root, 'status': 'failed', 'error': str(error)})
    result['cancelled'] = _cancelled(cancel_event)
    result['attempted_count'] = attempted
    return result


def run_cloud(backend, config, cancel_event=None, item_callback=None):
    policy, maximum, age, approved = _policy(config, 'cloud')
    drive_id, folders = config.get('cloud_drive_id', ''), config.get('cloud_folder_ids', [])
    result = _result({'drive_id': drive_id, 'folder_ids': list(folders) if isinstance(folders, list) else []})
    result['note'] = 'Only the saved personal drive and concrete approved folder IDs are scanned. Automatic recycling requires bounded streamed SHA256 verification and unchanged eTags; no content is cached locally.'
    if config.get('cloud_review_enabled') is not True:
        result['skips'].append({'reason': 'Cloud maintenance is disabled.'}); return result
    if not isinstance(drive_id, str) or not drive_id or not isinstance(folders, list) or not folders or len(folders) > 20 or any(not isinstance(folder, str) or not folder or folder == 'root' for folder in folders):
        result.update({'needs_approval': True}); result['skips'].append({'reason': 'Verify and save concrete personal-drive folder approvals first.'}); return result
    status = backend.cloud_status()
    if not isinstance(status, dict) or status.get('connected') is not True:
        result.update({'needs_approval': True}); result['skips'].append({'reason': 'The saved account is not connected; no sign-in was started.'}); return result
    automatic = policy != 'review' and approved and type(config.get('setup_version')) is int and config['setup_version'] == CURRENT_SETUP_VERSION
    result['review_only'] = not automatic
    if policy != 'review' and not automatic:
        result['needs_approval'] = True
    remaining_bytes, attempted = _CLOUD_BYTES, 0
    for folder in folders:
        if _cancelled(cancel_event) or attempted >= maximum or remaining_bytes <= 0:
            break
        try:
            # Validate the account/folder binding before even scanning its contents.
            scope = backend.cloud_approval_scope(folder)
            if scope.get('drive_id') != drive_id or scope.get('folder_id') != folder:
                result['needs_approval'] = True
                result['skips'].append({'folder_id': folder, 'reason': 'The connected drive or approved folder changed.'}); break
            plan = backend.cloud_scan(folder)
            if plan.get('drive', {}).get('id') != drive_id or plan.get('folder', {}).get('id') != folder:
                result['needs_approval'] = True
                result['skips'].append({'folder_id': folder, 'reason': 'The fresh scan no longer matches the saved scope.'}); break
            if not automatic:
                result['items'].append({'folder_id': folder, 'status': 'review', 'scan': plan}); continue
            if plan.get('complete') is not True:
                result['skips'].append({'folder_id': folder, 'reason': 'Incomplete cloud scan; no automatic recycle.'}); continue
            effective = dict(config, cloud_max_recycle=maximum - attempted, _verification_bytes_remaining=remaining_bytes)
            outcome = backend.cloud_recycle_approved(plan, effective, cancel_event=cancel_event, item_callback=item_callback)
            result['items'].extend(outcome.get('items', [])); result['skips'].extend(outcome.get('skips', []))
            result['failed_items'] += outcome.get('failed_items', 0)
            result['recycled_count'] += outcome.get('recycled_count', 0)
            attempted += outcome.get('attempted_count', 0)
            remaining_bytes -= outcome.get('verified_bytes', 0)
            if outcome.get('needs_approval'):
                result['needs_approval'] = True; break
            if outcome.get('stopped_after_error'):
                result['stopped_after_error'] = True; break
        except Exception:
            result['needs_approval'] = True
            result['failed_items'] += 1
            # Provider errors must never persist transient download URLs or tokens.
            result['items'].append({'folder_id': folder, 'status': 'failed', 'error': 'Cloud verification failed; no automatic retry was made.'})
            break
    result['cancelled'] = _cancelled(cancel_event)
    result['attempted_count'] = attempted
    result['verified_bytes'] = _CLOUD_BYTES - remaining_bytes
    return result
