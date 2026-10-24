"""Bounded local metadata snapshots and honest complete-to-complete growth trends."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

from .application_caches import _ordinary_root, _BAD_FLAGS, _audit, discover_recipes
from .cleaner import Cleaner
from .filesystem import _ordinary_data_path


DEFAULTS = {'enabled': False, 'roots': [], 'max_entries': 20000, 'max_seconds': 15,
            'max_depth': 16, 'max_bytes': 16 * 1024**4}


def local_root(value):
    """Reject drive roots, remote mounts and cloud/reparse ancestry before scanning."""
    return _ordinary_root(Path(value))


def validate_configuration(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError('Use the supported storage-growth settings.')
    result = copy.deepcopy(DEFAULTS)
    result.update(copy.deepcopy(value))
    if type(result['enabled']) is not bool:
        raise ValueError('Storage-growth scanning must be explicitly enabled or disabled.')
    roots = result['roots']
    if not isinstance(roots, list) or len(roots) > 16:
        raise ValueError('Select at most sixteen exact local directories.')
    for value in roots:
        if (not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value
                or not Path(value).is_absolute() or Path(value) == Path(value).parent
                or '..' in Path(value).parts or value.startswith(('\\\\', '//'))):
            raise ValueError('Storage-growth roots must be exact ordinary local directories below a drive root.')
    if len({os.path.normcase(os.path.abspath(root)) for root in roots}) != len(roots):
        raise ValueError('Storage-growth roots must be unique.')
    for key, low, high in (('max_entries', 1, 100000), ('max_seconds', 1, 60), ('max_depth', 1, 32),
                            ('max_bytes', 1, 256 * 1024**4)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Storage-growth ' + key + ' is outside its safe bound.')
    return result


class StorageGrowthTracker:
    def __init__(self, data_dir, *, clock=time.time, root_validator=local_root, recipe_provider=discover_recipes):
        self.data_dir, self.clock, self.root_validator = Path(data_dir), clock, root_validator
        self.recipe_provider = recipe_provider

    def _previous(self, path, root):
        info = _ordinary_data_path(path, file=True)
        if info is None:
            return None
        if info.st_size > 1024**2:
            raise ValueError('The local storage-growth baseline exceeds its bound.')
        raw = path.read_bytes()
        after = _ordinary_data_path(path, file=True)
        if after is None or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('The local storage-growth baseline changed while reading.')
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('root') != str(root)
                or value.get('complete') is not True or type(value.get('bytes')) is not int
                or value['bytes'] < 0 or type(value.get('file_count')) is not int
                or type(value.get('observed_at')) not in (int, float) or not math.isfinite(value['observed_at'])
                or not isinstance(value.get('buckets'), dict) or len(value['buckets']) > 1000):
            raise ValueError('The local storage-growth baseline is unsupported.')
        return value

    def scan(self, config, *, progress_callback=None, cancel_event=None):
        config = validate_configuration(config)
        report = {'schema': 1, 'id': uuid.uuid4().hex, 'read_only': True, 'roots': [], 'complete': True,
                  'examined': 0, 'scanned_bytes': 0, 'note': 'Logical file sizes, not reclaimed or physically allocated bytes. Only complete comparable scans produce a growth claim; no file contents are read.'}
        if not config['enabled']:
            return dict(report, disabled=True, skipped=True)
        deadline = time.monotonic() + config['max_seconds']
        try:
            recipes = self.recipe_provider()
        except Exception:
            recipes = []
        for selected in config['roots']:
            if cancel_event is not None and cancel_event.is_set():
                report.update(cancelled=True, complete=False)
                break
            item = {'root': selected, 'bytes': 0, 'file_count': 0, 'skipped_count': 0,
                    'complete': True, 'buckets': {}, 'owner': None, 'owner_evidence': None}
            report['roots'].append(item)
            try:
                root = self.root_validator(selected)
                for recipe in recipes:
                    if root == recipe.root or recipe.root in root.parents:
                        item.update(owner=recipe.app, owner_evidence='Exact fixed application web-cache recipe: ' + recipe.id)
                        break
                stack, root_device = [(root, 0)], root.lstat().st_dev
                while stack:
                    if report['examined'] >= config['max_entries'] or time.monotonic() >= deadline:
                        item['complete'] = False
                        break
                    directory, depth = stack.pop()
                    if directory == self.data_dir or self.data_dir in directory.parents:
                        item['skipped_count'] += 1
                        item['complete'] = False
                        continue
                    if directory != root and not Cleaner._safe_path(directory, root):
                        item['skipped_count'] += 1
                        item['complete'] = False
                        continue
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            report['examined'] += 1
                            if report['examined'] > config['max_entries'] or time.monotonic() >= deadline:
                                item['complete'] = False
                                break
                            if cancel_event is not None and cancel_event.is_set():
                                item.update(complete=False, cancelled=True)
                                break
                            path = Path(entry.path)
                            try:
                                # DirEntry.stat reports zero device/inode on Windows.
                                info = path.lstat()
                                if (entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & _BAD_FLAGS
                                        or info.st_dev != root_device):
                                    item['skipped_count'] += 1
                                    item['complete'] = False
                                    continue
                                if stat.S_ISDIR(info.st_mode):
                                    if depth < config['max_depth']:
                                        stack.append((path, depth + 1))
                                    else:
                                        item['skipped_count'] += 1
                                        item['complete'] = False
                                elif stat.S_ISREG(info.st_mode):
                                    info = path.lstat()
                                    if info.st_nlink != 1 or not Cleaner._safe_path(path, root):
                                        item['skipped_count'] += 1
                                        item['complete'] = False
                                        continue
                                    if report['scanned_bytes'] + item['bytes'] + info.st_size > config['max_bytes']:
                                        item['skipped_count'] += 1
                                        item['complete'] = False
                                        continue
                                    bucket = path.relative_to(root).parts[0] if path.parent != root else '[files in selected folder]'
                                    if bucket not in item['buckets'] and len(item['buckets']) >= 1000:
                                        bucket = '[other folders]'
                                    group = item['buckets'].setdefault(bucket, {'bytes': 0, 'file_count': 0})
                                    group['bytes'] += info.st_size
                                    group['file_count'] += 1
                                    item['bytes'] += info.st_size
                                    item['file_count'] += 1
                            except (OSError, ValueError):
                                item['skipped_count'] += 1
                                item['complete'] = False
                    if item.get('cancelled'):
                        break
                root_info = root.lstat()
                item.update(schema=1, observed_at=self.clock(), scan_max_depth=config['max_depth'],
                            root_device=root_info.st_dev, root_inode=root_info.st_ino)
                if type(item['observed_at']) not in (int, float) or not math.isfinite(item['observed_at']):
                    raise ValueError('The local observation clock is unavailable.')
                identity = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
                history = self.data_dir / 'storage-growth' / (identity + '.json')
                previous = self._previous(history, root)
                if (item['complete'] and previous is not None and previous.get('scan_max_depth') == config['max_depth']
                        and (item['root_device'], item['root_inode']) == (previous.get('root_device'), previous.get('root_inode'))
                        and item['observed_at'] > previous['observed_at']):
                    item.update(growth_bytes=item['bytes'] - previous['bytes'], previous_bytes=previous['bytes'],
                                interval_seconds=item['observed_at'] - previous['observed_at'])
                    item['growth_bytes_per_day'] = item['growth_bytes'] * 86400 / item['interval_seconds']
                else:
                    item['growth_bytes'] = None
                    item['trend_reason'] = 'A previous complete comparable scan and a complete current scan are required.'
                if item['complete']:
                    old_history = previous.get('history', []) if previous is not None else []
                    if not isinstance(old_history, list) or len(old_history) > 30:
                        raise ValueError('The local storage-growth history is unsupported.')
                    item['history'] = old_history[-29:] + [{'observed_at': item['observed_at'], 'bytes': item['bytes'], 'file_count': item['file_count']}]
                    _audit(history, item)
            except (OSError, ValueError, TypeError) as error:
                item.update(complete=False, error=str(error)[:500], growth_bytes=None)
            report['scanned_bytes'] += item['bytes']
            report['complete'] = report['complete'] and item['complete']
            if callable(progress_callback):
                try:
                    progress_callback({'progress_only': True, 'item_progress': {'key': 'storage-growth:' + report['id'],
                        'label': 'Storage growth', 'phase': 'scan', 'percent': None},
                        'description': f'{report["examined"]} metadata entries checked; {report["scanned_bytes"]} logical bytes observed.'})
                except Exception:
                    pass
        return report

    run = scan
