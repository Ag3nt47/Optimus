"""Append local scan measurements without connecting unidentified hardware.

The first observation is retained. Reaching a storage bound stops recording;
it never silently deletes earlier scans or changes maintenance behavior.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading

from .filesystem import _ordinary_data_path
from .hardware_health import _DIGEST, _METRICS, _number, _text, _stamp

_LOCK = threading.RLock()
_MAX_BYTES = 32 * 1024 * 1024
_MAX_SCANS = 20000
_MAX_DEVICES = 256
_SCAN = re.compile(r'[a-f0-9]{32}\Z')
_MEDIA = frozenset(('HDD', 'SSD', 'SCM', 'Unspecified', 'Unknown'))
_WEAR_EVIDENCE = frozenset(('not_applicable', 'unavailable', 'unverified_zero', 'provider_estimate', 'nvme_estimate'))


def _metric(kind, key, value):
    if key not in _METRICS[kind]:
        raise ValueError('Unknown hardware-history metric.')
    if value is None:
        return None
    if key == 'provider_condition_code':
        valid = value if type(value) is int and value in (0, 1, 2) else None
    elif key == 'temperature_c':
        valid = _number(value, -50, 150)
    elif key == 'capacity_retained_percent':
        valid = _number(value, 0, 200)
    elif key == 'wear_percent_used':
        valid = _number(value, 0, 255)
    else:
        valid = value if type(value) is int and 0 <= value <= 2**63 - 1 else None
    if valid is None:
        raise ValueError('Invalid hardware-history measurement.')
    return valid


def _validated(value):
    if (type(value) is not dict or set(value) != {'schema', 'devices', 'scans'}
            or type(value.get('schema')) is not int or value['schema'] != 1
            or type(value.get('devices')) is not dict or len(value['devices']) > _MAX_DEVICES
            or type(value.get('scans')) is not list or len(value['scans']) > _MAX_SCANS):
        raise ValueError('The local hardware history is invalid or exceeds its bound.')
    for identity, device in value['devices'].items():
        if (not isinstance(identity, str) or not _DIGEST.fullmatch(identity)
                or type(device) is not dict or set(device) not in (
                    {'label', 'kind', 'identity_quality'}, {'label', 'kind', 'identity_quality', 'media_type'})
                or device.get('kind') not in _METRICS
                or device.get('identity_quality') not in ('stable', 'weak')
                or not isinstance(device.get('label'), str) or not 0 < len(device['label']) <= 300):
            raise ValueError('Invalid historical device identity.')
        if 'media_type' in device and (device['kind'] != 'drive' or type(device['media_type']) is not str
                or device['media_type'] not in _MEDIA):
            raise ValueError('Invalid historical media type.')
    seen = set()
    for scan in value['scans']:
        if (type(scan) is not dict or set(scan) not in (
                    {'id', 'captured_at', 'measurements'}, {'id', 'captured_at', 'measurements', 'evidence'})
                or not isinstance(scan.get('id'), str) or not _SCAN.fullmatch(scan['id'])
                or scan['id'] in seen or _stamp(scan.get('captured_at')) is None
                or type(scan.get('measurements')) is not dict or len(scan['measurements']) > 96):
            raise ValueError('Invalid or repeated historical scan.')
        seen.add(scan['id'])
        for identity, measurements in scan['measurements'].items():
            if identity not in value['devices'] or type(measurements) is not dict or len(measurements) > 8:
                raise ValueError('Historical measurements have no valid device identity.')
            kind = value['devices'][identity]['kind']
            for key, reading in measurements.items():
                _metric(kind, key, reading)
        if 'evidence' in scan:
            if type(scan['evidence']) is not dict or len(scan['evidence']) > 64:
                raise ValueError('Invalid historical evidence scope.')
            for identity, evidence in scan['evidence'].items():
                if (identity not in scan['measurements'] or value['devices'][identity]['kind'] != 'drive'
                        or type(evidence) is not dict or set(evidence) != {'media_type', 'wear_evidence'}
                        or type(evidence.get('media_type')) is not str or evidence['media_type'] not in _MEDIA
                        or type(evidence.get('wear_evidence')) is not str or evidence['wear_evidence'] not in _WEAR_EVIDENCE):
                    raise ValueError('Invalid historical endurance evidence.')
    return value


def _wear_value(scan, identity, metric, known_hdd):
    value = scan['measurements'].get(identity, {}).get(metric)
    if metric != 'wear_percent_used':
        return value
    evidence = scan.get('evidence', {}).get(identity, {})
    kind = evidence.get('wear_evidence')
    if (known_hdd or evidence.get('media_type') != 'SSD'
            or kind not in ('provider_estimate', 'nvme_estimate')):
        return None
    # Generic zero may be a default/unsupported counter. The NVMe-specific
    # estimate explicitly supports genuine zero and its >254 saturation value.
    if kind != 'nvme_estimate' and value in (0, 255):
        return None
    return value


class HardwareHealthHistory:
    def __init__(self, data_dir):
        self.directory = Path(data_dir) / 'hardware-health'
        self.path = self.directory / 'history.json'

    def _read(self):
        before = _ordinary_data_path(self.path, file=True)
        if before is None:
            return {'schema': 1, 'devices': {}, 'scans': []}
        if before.st_size > _MAX_BYTES:
            raise ValueError('Hardware history reached its file-size bound.')
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_nlink)
        with self.path.open('rb') as stream:
            if identity(os.fstat(stream.fileno())) != identity(before):
                raise ValueError('Hardware history changed while opening.')
            raw = stream.read(_MAX_BYTES + 1)
            during = os.fstat(stream.fileno())
        after = _ordinary_data_path(self.path, file=True)
        if after is None or identity(before) != identity(during) or identity(before) != identity(after) or len(raw) > _MAX_BYTES:
            raise ValueError('Hardware history changed while reading.')
        def ordinary_object(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError('Duplicate history fields are unsupported.')
                result[key] = item
            return result
        return _validated(json.loads(raw.decode('utf-8'), object_pairs_hook=ordinary_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid history number.'))))

    def record(self, report):
        with _LOCK:
            lease = None
            try:
                if (not isinstance(report, dict) or report.get('kind') != 'hardware_health'
                        or report.get('read_only') is not True or report.get('status') not in ('completed', 'partial', 'unavailable')
                        or not isinstance(report.get('scan_id'), str) or not _SCAN.fullmatch(report['scan_id'])
                        or _stamp(report.get('captured_at')) is None):
                    raise ValueError('Only a completed health observation can enter history.')
                _ordinary_data_path(self.directory)
                self.directory.mkdir(parents=True, exist_ok=True)
                _ordinary_data_path(self.directory)
                from .automation import RunLease
                lease = RunLease(self.directory).acquire()
                value = self._read()
                if any(scan['id'] == report['scan_id'] for scan in value['scans']):
                    return {'status': 'already_recorded', 'saved': False, 'scan_count': len(value['scans'])}
                if len(value['scans']) >= _MAX_SCANS:
                    return {'status': 'limit_reached', 'saved': False, 'scan_count': len(value['scans']),
                            'note': 'Existing scans, including the baseline, were retained. New readings are displayed but history is full.'}
                measurements, evidence = {}, {}
                for section, kind in (('batteries', 'battery'), ('drives', 'drive')):
                    rows = report.get(section)
                    if not isinstance(rows, list) or len(rows) > (32 if kind == 'battery' else 64):
                        raise ValueError('Invalid health observation scope.')
                    for row in rows:
                        if not isinstance(row, dict):
                            raise ValueError('Invalid health observation device.')
                        digest = row.get('identity_hash')
                        stable = (row.get('identity_quality') == 'stable' and isinstance(digest, str) and _DIGEST.fullmatch(digest)
                                  and bool(_text(row.get('identity_source'), 100)))
                        name = _text(row.get('name'), 120) or kind.title()
                        # Weak groups are explicitly unverified locations/names;
                        # their observations are never connected by trend lines.
                        material = kind + '\0' + (digest if stable else name + '\0' + _text(row.get('device_id'), 120))
                        identity = hashlib.sha256(material.encode('utf-8')).hexdigest()
                        if identity in measurements:
                            raise ValueError('Duplicate device identities in one observation.')
                        if identity not in value['devices'] and len(value['devices']) >= _MAX_DEVICES:
                            raise ValueError('The retained historical device limit was reached; prior history was preserved.')
                        label = name + (' · device ' + _text(row.get('device_id'), 80) if row.get('device_id') else '')
                        previous = value['devices'].get(identity, {})
                        value['devices'][identity] = {'label': label[:300], 'kind': kind,
                            'identity_quality': 'stable' if stable else 'weak'}
                        if kind == 'drive':
                            media = row.get('media_type')
                            media = media if type(media) is str and media in _MEDIA else 'Unknown'
                            wear = row.get('wear_evidence')
                            wear = wear if type(wear) is str and wear in _WEAR_EVIDENCE else 'unavailable'
                            # Once explicitly known as rotational, preserve that
                            # caution even if later provider metadata conflicts.
                            prior_media = previous.get('media_type')
                            value['devices'][identity]['media_type'] = ('HDD' if prior_media == 'HDD'
                                else media if media in ('HDD', 'SSD', 'SCM')
                                else prior_media if prior_media in _MEDIA else media)
                            evidence[identity] = {'media_type': media, 'wear_evidence': wear}
                        measurements[identity] = {metric: _metric(kind, metric, row.get(metric)) for metric in _METRICS[kind]}
                value['scans'].append({'id': report['scan_id'], 'captured_at': report['captured_at'],
                    'measurements': measurements, 'evidence': evidence})
                _validated(value)
                # Check the encoded bound before replacing the existing history.
                raw = json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False).encode('ascii')
                if len(raw) > _MAX_BYTES:
                    return {'status': 'limit_reached', 'saved': False,
                            'note': 'The local history-size limit was reached; existing baseline and scans were retained.'}
                _ordinary_data_path(self.directory)
                from .workflow import atomic_json
                atomic_json(self.path, value)
                return {'status': 'saved', 'saved': True, 'scan_count': len(value['scans']),
                        'note': 'Measurements are stored locally; missing readings remain gaps and unidentified devices are not line-joined.'}
            except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, RuntimeError) as error:
                return {'status': 'unavailable', 'saved': False,
                        'note': 'The local history could not be safely recorded; prior history was preserved (' + type(error).__name__ + ').'}
            finally:
                if lease is not None:
                    lease.release()

    def series(self, *, max_points=500):
        """Prepare bounded plotting data; every original scan stays on disk."""
        if type(max_points) is not int or not 2 <= max_points <= 500:
            raise ValueError('The graph supports 2 to 500 visible points per series.')
        try:
            with _LOCK:
                value = self._read()
            scans, result = value['scans'], []
            for identity, device in value['devices'].items():
                known_hdd = device.get('media_type') == 'HDD' or any(
                    scan.get('evidence', {}).get(identity, {}).get('media_type') == 'HDD' for scan in scans)
                first = next((index for index, scan in enumerate(scans) if identity in scan['measurements']), None)
                if first is None:
                    continue
                indexes = list(range(first, len(scans)))
                if len(indexes) > max_points:
                    # Retain the baseline and recent scans; do not bridge the
                    # omitted interval with an invented continuous trend.
                    indexes = [first, *range(len(scans) - max_points + 1, len(scans))]
                index_set = set(indexes)
                available_metrics = [metric for metric in _METRICS[device['kind']] if any(
                    scans[index]['measurements'].get(identity, {}).get(metric) is not None for index in indexes)]
                # A device with no exposed metric remains an explicit empty
                # graph instead of disappearing or receiving a zero reading.
                selected = available_metrics or [next(iter(_METRICS[device['kind']]))]
                for metric, unit in _METRICS[device['kind']].items():
                    if metric not in selected:
                        continue
                    points = []
                    for index in indexes:
                        scan = scans[index]
                        points.append({'scan_id': scan['id'], 'scan_index': index + 1,
                            'captured_at': scan['captured_at'], 'value': _wear_value(scan, identity, metric, known_hdd),
                            'baseline': index == first,
                            'gap_before': index > first and index - 1 not in index_set})
                    result.append({'id': identity, 'label': device['label'], 'kind': device['kind'],
                        'identity_quality': device['identity_quality'], 'metric': metric, 'unit': unit, 'points': points})
            return {'schema': 1, 'status': 'available', 'scan_count': len(scans), 'series': result,
                'note': 'First observation and subsequent scans are retained locally. Missing readings are gaps. '
                        'Unverified identities show isolated observations. HDD wear and legacy endurance values without per-scan SSD evidence are shown as gaps; original measurements are retained. '
                        'Large graphs show the baseline and recent readings without joining the omitted interval.'}
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return {'schema': 1, 'status': 'unavailable', 'series': [],
                    'note': 'Local health history is unavailable; existing files were preserved.'}
