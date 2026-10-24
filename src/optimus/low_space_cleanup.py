"""Map existing approved cleanup roots to measured low-space local volumes.

This module never deletes, discovers a new cleanup scope or edits Cleaner plans.
Each category is scanned separately; a whole unchanged plan must belong to its
verified root/device before the ordinary Cleaner apply operation can be entered.
"""
from __future__ import annotations

import os
import math
from pathlib import Path
import stat

from .cleaner import OFFLINE, READONLY, RECALL, REPARSE, SYSTEM
from .volume_guard import check_drive_buffers

MAX_CATEGORIES = 16
MAX_FILES = 2000
MAX_SCAN_SECONDS = 5


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 32767
            or any(ord(character) < 32 for character in value)
            or value.startswith(('\\\\', '//'))):
        raise ValueError('A verified ordinary local cleanup path is required.')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('A cleanup path must be absolute without traversal.')
    return Path(os.path.abspath(path))


def _inside(path, root, *, allow_root=False):
    try:
        same = os.path.normcase(str(path)) == os.path.normcase(str(root))
        return (allow_root or not same) and os.path.normcase(os.path.commonpath((path, root))) == os.path.normcase(str(root))
    except (ValueError, OSError):
        return False


def _directory(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & (REPARSE | OFFLINE | RECALL):
            raise ValueError('Indirect, unavailable or cloud-backed cleanup roots are excluded.')
    return path.lstat()


class LowSpacePlanner:
    """Read-only authority checks around one existing category at a time."""
    def __init__(self, settings, *, volume_reader=None):
        self.settings = settings
        self.volume_reader = volume_reader or check_drive_buffers

    def observe(self):
        report = self.volume_reader(self.settings)
        target = self.settings.get('drive_space_guard_target_gib', 2.0)
        expected = math.ceil(target * 1024**3) if type(target) in (int, float) and math.isfinite(target) and 1 <= target <= 2 else None
        if (not isinstance(report, dict) or report.get('enabled') is not True
                or report.get('review_only') is not True or type(report.get('schema')) is not int or report['schema'] != 1
                or type(report.get('target_bytes')) is not int or report['target_bytes'] != expected
                or not isinstance(report.get('volumes'), list) or len(report['volumes']) > 4096):
            raise ValueError('The enabled local drive-space check is unavailable.')
        return report

    @staticmethod
    def summary(report):
        return [{key: row.get(key) for key in ('mountpoint', 'free_bytes', 'target_bytes', 'shortfall_bytes', 'status')}
                for row in report['volumes'] if isinstance(row, dict)]

    def target(self, category, report):
        if (not isinstance(category, dict) or category.get('available') is not True
                or category.get('process_names') or not isinstance(category.get('id'), str)
                or ':' in category['id']):
            return None
        root = _path(category.get('path'))
        if root == Path(root.anchor):
            raise ValueError('A complete drive is never a cleanup category.')
        matches = []
        for row in report['volumes']:
            if not isinstance(row, dict):
                raise ValueError('Local volume observations are malformed.')
            mount = _path(row.get('mountpoint'))
            if _inside(root, mount, allow_root=True):
                matches.append((len(mount.parts), mount, row))
        if not matches:
            return None
        maximum = max(item[0] for item in matches)
        nearest = [item for item in matches if item[0] == maximum]
        if len(nearest) != 1:
            raise ValueError('The cleanup volume is ambiguous.')
        _, mount, row = nearest[0]
        free, target = row.get('free_bytes'), row.get('target_bytes')
        if (row.get('status') != 'below_target' or row.get('below_target') is not True
                or type(free) is not int or type(target) is not int
                or target != report['target_bytes'] or not 0 <= free < target
                or row.get('shortfall_bytes') != target - free):
            return None
        root_info = _directory(root)
        mount_info = _directory(mount)
        if root_info.st_dev != mount_info.st_dev:
            raise ValueError('A nested filesystem differs from the observed local volume.')
        return {'category_id': category['id'], 'root': str(root), 'mountpoint': str(mount),
                'device': root_info.st_dev, 'root_inode': root_info.st_ino,
                'mount_inode': mount_info.st_ino, 'reported_device': row.get('device')}

    def revalidate(self, category, original, plan):
        """Read current headroom and require the entire untouched plan's scope."""
        report = self.observe()
        current = self.target(category, report)
        if current is None or current != original:
            return None, report
        if (not isinstance(plan, dict) or plan.get('complete') is not True
                or plan.get('errors') or plan.get('skipped')
                or not isinstance(plan.get('files'), list) or len(plan['files']) > MAX_FILES
                or type(plan.get('total_bytes')) is not int or plan['total_bytes'] < 0):
            raise ValueError('An incomplete or oversized low-space preview cannot be applied.')
        root = Path(current['root'])
        total = 0
        for record in plan['files']:
            if (not isinstance(record, dict) or record.get('category_id') != current['category_id']
                    or record.get('root') != current['root'] or record.get('device') != current['device']
                    or type(record.get('size')) is not int or record['size'] < 0):
                raise ValueError('The complete cleanup preview must stay on its verified category volume.')
            path = _path(record.get('path'))
            if not _inside(path, root):
                raise ValueError('A preview file left its exact selected category.')
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or getattr(info, 'st_file_attributes', 0) & (REPARSE | OFFLINE | RECALL | SYSTEM | READONLY)
                    or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) !=
                    (record['device'], record.get('inode'), record['size'], record.get('modified_ns'))):
                raise ValueError('A low-space candidate changed after preview.')
            total += record['size']
        if total != plan['total_bytes']:
            raise ValueError('Low-space preview byte totals are inconsistent.')
        return current, report

    def recovered(self, category, original, report):
        """A healthy label alone cannot establish that this volume recovered."""
        rows = [row for row in report['volumes'] if isinstance(row, dict)
                and row.get('mountpoint') == original['mountpoint']]
        if len(rows) != 1:
            return None
        row = rows[0]
        root, mount = _path(category.get('path')), _path(original['mountpoint'])
        root_info, mount_info = _directory(root), _directory(mount)
        if ((root_info.st_dev, root_info.st_ino, mount_info.st_dev, mount_info.st_ino, row.get('device')) !=
                (original['device'], original['root_inode'], original['device'], original['mount_inode'], original['reported_device'])):
            return None
        free = row.get('free_bytes')
        if (row.get('status') == 'ok' and row.get('below_target') is False
                and type(free) is int and free >= report['target_bytes']
                and row.get('target_bytes') == report['target_bytes'] and row.get('shortfall_bytes') == 0):
            return True
        return False if self.target(category, report) == original else None
