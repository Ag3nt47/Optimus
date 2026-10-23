"""Per-run display accounting from observed bytes and terminal item receipts.

These totals never authorize work. Logical lengths of successfully deleted files
are distinct from free space gained; downloaded bytes are distinct from an
installed application's disk footprint. Preview, catalog and recycle sizes do
not enter this ledger.
"""
from __future__ import annotations

import hashlib

MAX_BYTES = 1 << 50
MAX_ITEMS = 1000000
_SOURCES = frozenset({'winget_native', 'windows_update'})


def _number(value, maximum=MAX_BYTES):
    return type(value) is int and 0 <= value <= maximum


def _identity(value):
    return (isinstance(value, str) and 0 < len(value) <= 512
            and value == value.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def download_observation(value, *, key=None):
    """Extract only a producer's explicit download-byte observation."""
    if not isinstance(value, dict):
        return None
    identity = value.get('key', key)
    done, total = value.get('downloaded_bytes'), value.get('download_total_bytes')
    if (not _identity(identity) or value.get('phase') not in ('download', 'downloading')
            or not _number(done) or not isinstance(value.get('bytes_source'), str)
            or value['bytes_source'] not in _SOURCES
            or type(value.get('bytes_estimated')) is not bool
            or total is not None and (not _number(total) or total < done)):
        return None
    return {'key': identity, 'phase': 'download', 'downloaded_bytes': done,
            'download_total_bytes': total, 'bytes_estimated': value['bytes_estimated'],
            'bytes_source': value['bytes_source']}


def normalize_resource_totals(value):
    """Bound a presentation snapshot without promoting unknown facts."""
    if not isinstance(value, dict):
        return None
    numbers = ('removed_bytes', 'removed_files', 'downloaded_bytes', 'installed_items')
    flags = ('removed_known', 'downloaded_known', 'downloaded_estimated',
             'downloaded_partial', 'installed_known', 'installed_reported')
    if (any(not _number(value.get(key), MAX_BYTES if key.endswith('_bytes') else MAX_ITEMS) for key in numbers)
            or any(type(value.get(key)) is not bool for key in flags)):
        return None
    return {key: value[key] for key in (*numbers, *flags)}


def _receipt_key(row, kind):
    binding = row.get('binding') if isinstance(row.get('binding'), dict) else {}
    identity = row.get('id') or binding.get('id')
    if not _identity(identity):
        return None
    if kind == 'driver_updates':
        revision = binding.get('revision')
        return 'driver:update:' + identity + ':' + str(revision) if _number(revision, 2147483647) else None
    presence = row.get('presence')
    if presence in ('keep_installed', 'keep_uninstalled'):
        return 'software:' + ('install:' if presence == 'keep_installed' else 'uninstall:') + identity
    return 'software:update:' + identity


def _known(outcome):
    return (isinstance(outcome, dict) and outcome.get('outcomeKnown') is True
            and outcome.get('running') is False and type(outcome.get('installer_started')) is bool)


def _installed(row, outcome, kind):
    if (row.get('status') not in ('completed', 'applied') or row.get('uncertain') is True
            or not _known(outcome) or outcome['installer_started'] is not True
            or outcome.get('requiresReview') is True or outcome.get('cancelled') is True
            or outcome.get('skipped') is True or outcome.get('uncertain') is True
            or outcome.get('success') is False or outcome.get('verified') is False):
        return False
    if kind == 'driver_updates':
        return row.get('status') == 'applied' and all(
            type(outcome.get(key)) is int and outcome[key] == expected
            for key, expected in (('resultCode', 2), ('hresult', 0),
                                  ('aggregateResultCode', 2), ('aggregateHresult', 0)))
    if row.get('presence') == 'keep_uninstalled':
        return False
    if outcome.get('verified') is not True and outcome.get('installation_reported') is not True:
        return False
    return type(outcome.get('exitCode')) is int and outcome['exitCode'] == 0


class WorkerResourceTotals:
    """Monotonic facts for one worker, owned by its progress mux lock.

    Absolute counters are merged by identity, so repeated ticks, receipts and
    the final batch never add the same work twice. A missing byte observation
    remains unknown even when an installer reports success.
    """
    def __init__(self, kind='maintenance'):
        self.kind = kind
        self._removed = {}
        self._downloads = {}
        self._receipts = {}
        self._installed_known = False
        self._removed_bytes = self._removed_files = self._downloaded_bytes = 0

    def observe(self, state):
        if not isinstance(state, dict):
            return
        item = state.get('item_progress')
        if isinstance(item, dict):
            if self.kind != 'maintenance':
                self._download(item)
            if item.get('phase') == 'cleanup':
                self.removal_receipt(item.get('key'), state)

    def _download(self, observation, *, key=None):
        row = download_observation(observation, key=key)
        if row is None or len(self._downloads) >= 10000 and row['key'] not in self._downloads:
            return
        previous = self._downloads.get(row['key'])
        delta = row['downloaded_bytes'] - (previous['downloaded_bytes'] if previous else 0)
        if self._downloaded_bytes + delta > MAX_BYTES:
            return
        if previous is None:
            self._downloads[row['key']] = row
            self._downloaded_bytes += row['downloaded_bytes']
        elif row['downloaded_bytes'] >= previous['downloaded_bytes']:
            # A source change cannot upgrade a rounded observation to exact.
            row['bytes_estimated'] = row['bytes_estimated'] or previous['bytes_estimated']
            self._downloads[row['key']] = row
            self._downloaded_bytes += delta

    def removal_receipt(self, key, result):
        if (not _identity(key) or not key.startswith('cleanup:') or not isinstance(result, dict)
                or not _number(result.get('deleted_bytes')) or not _number(result.get('deleted_count'), MAX_ITEMS)
                or len(self._removed) >= 10000 and key not in self._removed):
            return
        previous = self._removed.get(key, (0, 0))
        current = (max(previous[0], result['deleted_bytes']), max(previous[1], result['deleted_count']))
        byte_delta, file_delta = current[0] - previous[0], current[1] - previous[1]
        if self._removed_bytes + byte_delta > MAX_BYTES or self._removed_files + file_delta > MAX_ITEMS:
            return
        self._removed[key] = current
        self._removed_bytes += byte_delta
        self._removed_files += file_delta

    def receipt(self, row):
        if not isinstance(row, dict):
            return
        key = _receipt_key(row, self.kind)
        if key is None or len(self._receipts) >= 10000 and key not in self._receipts:
            return
        outcome = row.get('result') if isinstance(row.get('result'), dict) else {}
        observed = outcome.get('download_progress')
        if isinstance(observed, dict):
            # A receipt's caller-bound identity must match its current item.
            if observed.get('key', key) == key:
                self._download(observed, key=key)
        elif 'downloaded_bytes' in outcome:
            self._download(dict(outcome, phase='download'), key=key)
        known = _known(outcome)
        count_known = known and (row.get('status') not in ('completed', 'applied')
                                  or outcome.get('installer_started') is False
                                  or outcome.get('verified') is True or outcome.get('installation_reported') is True
                                  or self.kind == 'driver_updates')
        self._installed_known = self._installed_known or count_known
        previous = self._receipts.get(key, {})
        installed = _installed(row, outcome, self.kind)
        self._receipts[key] = {'known': previous.get('known', False) or known,
                              'transfer_unknown': outcome.get('installer_started') is not False,
                              'installed_reported': previous.get('installed_reported', False)
                                  or installed and self.kind != 'driver_updates' and outcome.get('verified') is not True,
                              'installed': previous.get('installed', False) or installed}

    def result(self, result):
        if not isinstance(result, dict):
            return
        if _known(result) and result['installer_started'] is False:
            self._installed_known = True
        rows = result.get('items')
        if isinstance(rows, list):
            for row in rows[:1000]:
                self.receipt(row)
        presence = result.get('app_presence')
        if isinstance(presence, dict):
            rows = presence.get('items')
            if isinstance(rows, list):
                for row in rows[:100]:
                    self.receipt(row)

    def snapshot(self):
        downloaded = list(self._downloads.values())
        partial = any(row['download_total_bytes'] is None
                      or row['downloaded_bytes'] < row['download_total_bytes'] for row in downloaded)
        # Counted installs without a byte receipt are not zero-byte downloads.
        partial = partial or any(key not in self._downloads for key, row in self._receipts.items()
                                 if row['transfer_unknown'])
        return {'removed_bytes': self._removed_bytes,
                'removed_files': self._removed_files,
                'removed_known': bool(self._removed),
                'downloaded_bytes': self._downloaded_bytes,
                'downloaded_known': bool(downloaded),
                'downloaded_estimated': any(row['bytes_estimated'] for row in downloaded),
                'downloaded_partial': partial,
                'installed_items': sum(row['installed'] for row in self._receipts.values()),
                'installed_known': self._installed_known,
                'installed_reported': any(row['installed_reported'] for row in self._receipts.values())}


def resource_totals_from_smart_actions(snapshot):
    """Summarize only actual actions in one Smart Maintenance status snapshot.

    This reads no history and runs no cleanup. The monitor's last check is a
    separate scope from a one-click run; duplicate action receipts and database
    row counts cannot inflate its logical-file removal total.
    """
    ledger = WorkerResourceTotals()
    if not isinstance(snapshot, dict) or snapshot.get('preview') is True:
        return ledger.snapshot()
    actions = snapshot.get('actions')
    if not isinstance(actions, list):
        return ledger.snapshot()
    for action in actions[:1000]:
        if (not isinstance(action, dict) or action.get('status') not in
                ('completed', 'completed_with_errors', 'cancelled')):
            continue
        receipt = action.get('receipt')
        outcome = action.get('result')
        if (not isinstance(receipt, str) or not 0 < len(receipt) <= 2048
                or any(ord(c) < 32 or ord(c) == 127 for c in receipt) or not isinstance(outcome, dict)
                or outcome.get('uncertain') is True or outcome.get('preview') is True):
            continue
        key = 'cleanup:smart:' + hashlib.sha256(receipt.encode('utf-8')).hexdigest()
        if _number(outcome.get('deleted_bytes')) and _number(outcome.get('deleted_count'), MAX_ITEMS):
            ledger.removal_receipt(key, outcome)
            continue
        items = outcome.get('items')
        for index, item in enumerate(items[:20] if isinstance(items, list) else []):
            if (isinstance(item, dict) and isinstance(item.get('cache'), dict)
                    and item['cache'].get('uncertain') is not True and item['cache'].get('preview') is not True):
                ledger.removal_receipt(key + ':cache:' + str(index), item['cache'])
    return ledger.snapshot()
