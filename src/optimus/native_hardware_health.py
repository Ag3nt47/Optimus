"""Bounded, read-only Linux/macOS battery and physical-disk observations.

No repair, self-test, package installation, sudo, charge-limit change or serial
number export. Missing device counters stay unknown. Capacity is never inferred
from the current charge percentage or filesystem free space.

Native contracts: https://docs.kernel.org/power/power_supply_class.html
https://github.com/torvalds/linux/blob/master/Documentation/ABI/testing/sysfs-class-power
https://man7.org/linux/man-pages/man8/lsblk.8.html (upstream util-linux manual)
https://github.com/smartmontools/smartmontools/blob/main/src/smartctl.8.in
https://support.apple.com/en-us/102589
https://support.apple.com/en-ie/guide/mac-help/mchlp2548/27/mac/27
Apple utility plist/JSON keys are version-dependent and used only when present.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import threading
import time
from xml.parsers.expat import ExpatError


_TOOLS = {
    'Linux': {'lsblk': ('/usr/bin/lsblk', '/bin/lsblk'),
              'smartctl': ('/usr/sbin/smartctl', '/usr/bin/smartctl', '/sbin/smartctl')},
    'Darwin': {'diskutil': ('/usr/sbin/diskutil',), 'system_profiler': ('/usr/sbin/system_profiler',)},
}
_LSBLK_FIELDS = 'NAME,KNAME,TYPE,SIZE,MODEL,ROTA,RM,TRAN'
_LINUX_DISK = re.compile(r'(?:sd[a-z]{1,4}|hd[a-z]{1,4}|vd[a-z]{1,4}|xvd[a-z]{1,4}|nvme[0-9]{1,4}n[0-9]{1,4}|mmcblk[0-9]{1,4})\Z')
_MAC_DISK = re.compile(r'disk[0-9]{1,5}\Z')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')
_MAX_STDOUT, _MAX_STDERR, _MAX_TOTAL = 512 * 1024, 16 * 1024, 2 * 1024 * 1024
_ATTRIBUTE_GATE = threading.Semaphore(1)
_PLACEHOLDERS = {'unknown', 'none', 'n/a', 'not available', 'not specified', 'default string',
                 'to be filled by o.e.m.', 'to be filled by oem', 'serialnumber', '123456789', '0123456789'}


def _text(value, maximum=120):
    if not isinstance(value, str):
        return None
    value = ' '.join(''.join(c if c.isprintable() else ' ' for c in value).split())
    return value[:maximum] if value else None


def _number(value, minimum=0, maximum=10 ** 15, *, integer=False):
    if isinstance(value, str) and re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value.strip()):
        try:
            value = float(value) if '.' in value else int(value)
        except (ValueError, OverflowError):
            return None
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    if integer:
        return value if type(value) is int else None
    return value


def _mapping(value):
    return value if isinstance(value, dict) else {}


class _Budget:
    def __init__(self, seconds, cancel, clock):
        self.clock, self.cancel, self.deadline, self.bytes = clock, cancel, clock() + seconds, 0

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError('Hardware health collection was cancelled.')
        if self.clock() >= self.deadline:
            raise TimeoutError('Hardware health collection reached its time budget.')
        if self.bytes > _MAX_TOTAL:
            raise ValueError('Hardware health collection reached its output budget.')

    def remaining(self, maximum):
        self.check()
        return max(.01, min(maximum, self.deadline - self.clock()))


def _allowed_argv(argv):
    if not isinstance(argv, list) or not argv or any(type(v) is not str for v in argv):
        return False
    executable = argv[0]
    if executable in _TOOLS['Linux']['lsblk']:
        return argv[1:] == ['--json', '--bytes', '--nodeps', '--output', _LSBLK_FIELDS]
    if executable in _TOOLS['Darwin']['system_profiler']:
        return argv[1:] == ['SPPowerDataType', '-json', '-timeout', '4']
    if executable in _TOOLS['Darwin']['diskutil']:
        return (argv[1:] == ['list', '-plist', 'physical'] or len(argv) == 4
                and argv[1:3] == ['info', '-plist'] and _MAC_DISK.fullmatch(argv[3]) is not None)
    if executable in _TOOLS['Linux']['smartctl']:
        return (len(argv) == 12 and argv[1:10] == ['-j', '-H', '-A', '-q', 'noserial', '-n', 'standby,3', '-d', argv[9]]
                and argv[9] in ('ata', 'nvme') and argv[10] == '--'
                and argv[11].startswith('/dev/') and _LINUX_DISK.fullmatch(argv[11][5:]) is not None)
    return False


def _trusted_tool(platform_name, name):
    """Ignore PATH and user-writable replacements, including unsafe ancestors."""
    for supplied in _TOOLS[platform_name][name]:
        try:
            path = Path(supplied).resolve(strict=True)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
                continue
            if any(p.stat().st_uid != 0 or p.stat().st_mode & 0o022 for p in (path, *path.parents)):
                continue
            # argv uses the fixed OS path; resolving is solely a trust check.
            return supplied
        except (OSError, RuntimeError, AttributeError):
            continue
    return None


def _bounded_command(argv, *, timeout, cancel_event=None):
    if not _allowed_argv(argv):
        raise ValueError('Hardware health commands must match the read-only allowlist.')
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('Hardware health command was cancelled before launch.')
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('LD_PRELOAD', 'LD_LIBRARY_PATH') and not key.startswith('DYLD_')}
    environment.update(LC_ALL='C', LANG='C', PATH='/usr/bin:/usr/sbin:/bin:/sbin')
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=False, env=environment,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    buffers, overflow, lock = [bytearray(), bytearray()], threading.Event(), threading.Lock()

    def read_pipe(stream, index, maximum):
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                with lock:
                    room = maximum + 1 - len(buffers[index])
                    if room > 0:
                        buffers[index].extend(chunk[:room])
                    if len(buffers[index]) > maximum:
                        overflow.set()
                        break
        finally:
            stream.close()

    threads = [threading.Thread(target=read_pipe, args=(stream, index, maximum), daemon=True)
               for index, (stream, maximum) in enumerate(((process.stdout, _MAX_STDOUT), (process.stderr, _MAX_STDERR)))]
    for thread in threads:
        thread.start()
    deadline, failure = time.monotonic() + timeout, None
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                failure = InterruptedError('Hardware health command was cancelled.')
            elif overflow.is_set():
                failure = ValueError('Hardware health command exceeded its output limit.')
            elif time.monotonic() >= deadline:
                failure = TimeoutError('Hardware health command exceeded its time limit.')
            if failure is not None or process.poll() is not None:
                break
            time.sleep(.025)
        if failure is not None:
            raise failure
        for thread in threads:
            thread.join(timeout=min(.2, max(.01, deadline - time.monotonic())))
        if any(thread.is_alive() for thread in threads) or overflow.is_set():
            raise ValueError('Hardware health command output was incomplete or oversized.')
        return {'exit_code': process.returncode, 'stdout': bytes(buffers[0]), 'stderr': bytes(buffers[1])}
    finally:
        # Only this exact owned read-only utility is retired; no services or
        # system-wide process-name searches, installers or descendants are used.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    pass


def _invoke(tool, args, budget, command, seconds=3):
    argv = [tool, *args]
    if not _allowed_argv(argv):
        raise ValueError('Hardware health command was not allowlisted.')
    result = command(argv, timeout=budget.remaining(seconds), cancel_event=budget.cancel)
    budget.check()
    if (not isinstance(result, dict) or type(result.get('exit_code')) is not int
            or type(result.get('stdout')) is not bytes or type(result.get('stderr', b'')) is not bytes):
        raise ValueError('Hardware health command returned invalid metadata.')
    output, errors = result['stdout'], result.get('stderr', b'')
    if len(output) > _MAX_STDOUT or len(errors) > _MAX_STDERR:
        raise ValueError('Hardware health command returned oversized output.')
    budget.bytes += len(output) + len(errors)
    budget.check()
    return result


def _read_attribute(path):
    try:
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        elif stat.S_ISLNK(path.lstat().st_mode):  # Portable filesystem fixtures.
            return None
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            raw = stream.read(513)
        return raw
    except (OSError, UnicodeError):
        return None


def _attr(path, budget):
    budget.check()
    if not _ATTRIBUTE_GATE.acquire(blocking=False):
        return None
    value = {}
    def read():
        try:
            value['raw'] = _read_attribute(path)
        finally:
            _ATTRIBUTE_GATE.release()
    thread = threading.Thread(target=read, name='Optimus bounded sysfs attribute read', daemon=True)
    try:
        thread.start()
    except Exception:
        _ATTRIBUTE_GATE.release()
        raise
    # An unhealthy kernel/firmware driver can block even a small sysfs read.
    # Keep at most one such own read outstanding; never let it hold maintenance
    # or spawn a growing pool of readers on later scans. No daemon is installed.
    while thread.is_alive():
        budget.check()
        thread.join(timeout=budget.remaining(.025))
    raw = value.get('raw')
    if raw is None:
        return None
    budget.bytes += len(raw)
    budget.check()
    try:
        return raw.decode('utf-8', 'strict').strip() if len(raw) <= 512 else None
    except UnicodeError:
        return None


def _strong_identity(kind, values):
    if any(type(value) is not str or len(value) > 512 or any(not c.isprintable() for c in value) for value in values):
        return None
    clean = [_text(value, 512) for value in values]
    if any(not value or value.casefold() in _PLACEHOLDERS or re.fullmatch(r'[0\s.-]+', value) for value in clean):
        return None
    # Device serials can be case-sensitive. Normalizing their case would join
    # distinct hardware across scans; a conservative split is preferable.
    return hashlib.sha256(('optimus-hardware-v1\0' + kind + '\0' + '\0'.join(clean)).encode()).hexdigest()


def _identity(row, value=None, source=None):
    row.update(identity_hash=value, identity_source=source if value else None,
               identity_quality='stable' if value else 'unavailable')


def _sysfs_device(path, root):
    resolved = path.resolve(strict=True)
    boundary = Path('/sys') if root == Path('/sys/class/power_supply') or root == Path('/sys/class/block') else root.resolve(strict=True)
    if not resolved.is_relative_to(boundary):
        raise ValueError('A native device link left its read-only sysfs scope.')
    return resolved


def _linux_batteries(result, root, budget):
    if not root.is_dir():
        result['capabilities']['battery_health'] = 'unavailable'
        return
    incomplete = False
    with os.scandir(root) as entries:
        paths = []
        for entry in entries:
            budget.check()
            if len(paths) >= 32:
                incomplete = True
                result['errors'].append('Battery inventory exceeded the 32-device read bound.')
                break
            if _NAME.fullmatch(entry.name):
                paths.append(root / entry.name)
    for path in sorted(paths):
        budget.check()
        try:
            device = _sysfs_device(path, root)
            supply_type = _attr(device / 'type', budget)
            if supply_type is None:
                incomplete = True
                result['errors'].append('A power-supply type was unreadable; absence of a battery was not established.')
                continue
            if supply_type != 'Battery' or _attr(device / 'present', budget) == '0':
                continue
            row = {'name': path.name, 'source': 'Linux power_supply sysfs', 'temp_c': None,
                   'cycle_count': None, 'full_charge_capacity': None, 'design_capacity': None,
                   'capacity_unit': None, 'condition': _text(_attr(device / 'health', budget)),
                   'charge_status': _text(_attr(device / 'status', budget))}
            for key in ('energy_full', 'energy_full_design', 'charge_full', 'charge_full_design'):
                row[key] = _number(_attr(device / key, budget), integer=True)
            for full, design, unit in (('energy_full', 'energy_full_design', 'uWh'), ('charge_full', 'charge_full_design', 'uAh')):
                if row[full] is not None and row[design] is not None and row[full] > 0 and row[design] > 0:
                    row.update(full_charge_capacity=row[full], design_capacity=row[design], capacity_unit=unit)
                    break
            # Linux ABI defines cycle_count=0 as unavailable, not zero wear.
            row['cycle_count'] = _number(_attr(device / 'cycle_count', budget), minimum=1, maximum=10 ** 7, integer=True)
            temperature = _number(_attr(device / 'temp', budget), minimum=-400, maximum=1200)
            row['temp_c'] = temperature / 10 if temperature is not None else None
            manufacturer, model = _attr(device / 'manufacturer', budget), _attr(device / 'model_name', budget)
            row.update(manufacturer=_text(manufacturer), model=_text(model))
            serial = _attr(device / 'serial_number', budget)
            _identity(row, _strong_identity('Linux/battery', [manufacturer, model, serial]), 'Linux power_supply manufacturer/model/serial')
            result['batteries'].append(row)
        except (OSError, ValueError):
            incomplete = True
            result['errors'].append('A battery device could not be read within its native scope.')
    result['capabilities']['battery_health'] = ('unavailable' if incomplete and not result['batteries'] else 'not_present' if not result['batteries'] else 'partial' if incomplete else 'available'
        if any(row['capacity_unit'] or row['cycle_count'] is not None or row['temp_c'] is not None
               or row['condition'] not in (None, 'Unknown') for row in result['batteries']) else 'not_exposed')


def _linux_storage(result, tools, block_root, budget, command):
    if not tools.get('lsblk'):
        result['capabilities']['storage_health'] = 'unavailable'
        return
    response = _invoke(tools['lsblk'], ['--json', '--bytes', '--nodeps', '--output', _LSBLK_FIELDS], budget, command)
    if response['exit_code'] != 0:
        raise ValueError('Native block-device inventory was unavailable.')
    payload = json.loads(response['stdout'], parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite device metadata.')))
    rows = payload.get('blockdevices') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError('Native block-device inventory was incomplete or oversized.')
    seen, invalid_disk = set(), False
    for source in rows:
        budget.check()
        if not isinstance(source, dict) or not isinstance(source.get('type'), str):
            raise ValueError('Native block-device inventory contained an invalid row.')
        if source.get('type') != 'disk':
            continue
        name = source.get('kname')
        if not isinstance(name, str) or not _LINUX_DISK.fullmatch(name):
            invalid_disk = True
            result['errors'].append('A native whole-disk identifier was unsupported; no device path was guessed.')
            continue
        if name in seen:
            raise ValueError('Native block-device identity was ambiguous.')
        seen.add(name)
        if len(result['storage']) >= 16:
            result['errors'].append('Physical-disk inventory exceeded the 16-device read bound.')
            break
        row = {'name': _text(source.get('model')) or name, 'device_id': name, 'model': _text(source.get('model')),
               'size_bytes': _number(source.get('size'), maximum=10 ** 18, integer=True),
               'bus_type': _text(source.get('tran')), 'source': 'lsblk physical block-device inventory',
               'health_status': None, 'smart_passed': None, 'operational_status': None,
               'wear_percent_used': None, 'temp_c': None, 'power_on_hours': None,
               'uncorrected_read_errors': None, 'uncorrected_write_errors': None}
        _identity(row)
        try:
            device = _sysfs_device(block_root / name, block_root)
            row['operational_status'] = _text(_attr(device / 'device' / 'state', budget))
            wwid = _attr(device / 'wwid', budget) or _attr(device / 'device' / 'wwid', budget)
            if wwid and re.fullmatch(r'(?:naa\.[0-9a-fA-F]{16,32}|eui\.[0-9a-fA-F]{16,32}|nvme\.[A-Za-z0-9_.:-]{12,500}|t10\.[A-Za-z0-9_.: -]{12,500})', wwid):
                suffix = wwid.partition('.')[2]
                if not re.fullmatch(r'[0 .:-]+|[fF]+', suffix):
                    if wwid.startswith(('naa.', 'eui.')):
                        wwid = wwid.lower()
                    _identity(row, _strong_identity('Linux/storage/wwid', [wwid]), 'Linux sysfs WWID')
        except (OSError, ValueError):
            pass
        result['storage'].append(row)
        kind = 'nvme' if re.fullmatch(r'nvme[0-9]+n[0-9]+', name) and source.get('tran') == 'nvme' else 'ata' if source.get('tran') in ('ata', 'sata') and re.fullmatch(r'[sh]d[a-z]+', name) else None
        if not tools.get('smartctl') or kind is None:
            row['health_unavailable_reason'] = 'Native inventory has no SMART health reading; no tool was installed and no unsupported bus was probed.'
            continue
        try:
            value = _invoke(tools['smartctl'], ['-j', '-H', '-A', '-q', 'noserial', '-n', 'standby,3', '-d', kind, '--', '/dev/' + name], budget, command, seconds=2)
            smart = json.loads(value['stdout'], parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite SMART metadata.')))
            metadata = _mapping(_mapping(smart).get('smartctl'))
            native_code = metadata.get('exit_status')
            if (type(native_code) is not int or native_code != value['exit_code'] or not 0 <= native_code <= 255
                    or native_code & 7 or _mapping(smart.get('device')).get('name') != '/dev/' + name):
                raise ValueError('SMART device, power state or read completion was unavailable.')
            passed = _mapping(smart.get('smart_status')).get('passed')
            if type(passed) is not bool or bool(native_code & 8) != (not passed):
                raise ValueError('SMART health status was contradictory or unavailable.')
            row.update(smart_passed=passed, health_status='Passed' if passed else 'Failed', health_source='Existing trusted smartctl read-only health')
            row['temp_c'] = _number(_mapping(smart.get('temperature')).get('current'), minimum=-40, maximum=150)
            row['power_on_hours'] = _number(_mapping(smart.get('power_on_time')).get('hours'), maximum=10 ** 9, integer=True)
            nvme = smart.get('nvme_smart_health_information_log', {})
            if isinstance(nvme, dict):
                row['wear_percent_used'] = _number(nvme.get('percentage_used'), maximum=255, integer=True)
        except (OSError, ValueError, json.JSONDecodeError):
            row['health_unavailable_reason'] = 'SMART health could not be read without extra permissions, waking a sleeping device or changing device state.'
    result['capabilities']['storage_health'] = ('unavailable' if invalid_disk and not result['storage'] else 'not_present' if not result['storage'] else 'available'
        if all(row['smart_passed'] is not None for row in result['storage']) else 'partial' if any(row['smart_passed'] is not None for row in result['storage']) else 'not_exposed')


def _mac_batteries(result, tools, budget, command):
    if not tools.get('system_profiler'):
        result['capabilities']['battery_health'] = 'unavailable'
        return
    value = _invoke(tools['system_profiler'], ['SPPowerDataType', '-json', '-timeout', '4'], budget, command, seconds=4)
    if value['exit_code'] != 0:
        raise ValueError('Native power inventory was unavailable.')
    payload = json.loads(value['stdout'], parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite battery metadata.')))
    sources = payload.get('SPPowerDataType') if isinstance(payload, dict) else None
    if not isinstance(sources, list) or len(sources) > 32:
        raise ValueError('Native power inventory was invalid or oversized.')
    for source in sources:
        budget.check()
        if not isinstance(source, dict):
            continue
        health = _mapping(source.get('sppower_battery_health_info'))
        if not health and not any(isinstance(key, str) and key.startswith('sppower_battery_') for key in source):
            continue
        model = source.get('sppower_battery_model_info', {})
        model = model if isinstance(model, dict) else {}
        maximum = health.get('sppower_battery_maximum_capacity')
        maximum = maximum[:-1].strip() if isinstance(maximum, str) and maximum.endswith('%') else maximum
        row = {'name': 'Built-in battery', 'source': 'system_profiler SPPowerDataType',
               'cycle_count': _number(health.get('sppower_battery_cycle_count'), maximum=10 ** 7, integer=True),
               'condition': _text(health.get('sppower_battery_health')),
               'reported_health_capacity_percent': _number(maximum, maximum=100),
               'full_charge_capacity': None, 'design_capacity': None, 'capacity_unit': None,
               'temp_c': None, 'manufacturer': _text(model.get('sppower_battery_manufacturer')),
               'model': _text(model.get('sppower_battery_device_name'))}
        _identity(row, _strong_identity('Darwin/battery', [model.get('sppower_battery_manufacturer'),
            model.get('sppower_battery_device_name'), model.get('sppower_battery_serial_number')]), 'Apple power profiler manufacturer/model/serial')
        result['batteries'].append(row)
    result['capabilities']['battery_health'] = ('not_present' if not result['batteries'] else 'available'
        if any(row['reported_health_capacity_percent'] is not None or row['cycle_count'] is not None
               or row['condition'] not in (None, 'Unknown') for row in result['batteries']) else 'not_exposed')


def _mac_storage(result, tools, budget, command):
    if not tools.get('diskutil'):
        result['capabilities']['storage_health'] = 'unavailable'
        return
    value = _invoke(tools['diskutil'], ['list', '-plist', 'physical'], budget, command)
    if value['exit_code'] != 0:
        raise ValueError('Native physical-disk inventory was unavailable.')
    listing = plistlib.loads(value['stdout'])
    sources = listing.get('AllDisksAndPartitions') if isinstance(listing, dict) else None
    if not isinstance(sources, list) or len(sources) > 128:
        raise ValueError('Native physical-disk inventory was invalid or oversized.')
    seen, invalid_disk = set(), False
    for source in sources:
        budget.check()
        name = source.get('DeviceIdentifier') if isinstance(source, dict) else None
        if not isinstance(name, str) or not _MAC_DISK.fullmatch(name):
            invalid_disk = True
            result['errors'].append('A native physical-disk identifier was unavailable; no device path was guessed.')
            continue
        if name in seen:
            raise ValueError('Native disk identity was ambiguous.')
        seen.add(name)
        if len(result['storage']) >= 16:
            result['errors'].append('Physical-disk inventory exceeded the 16-device read bound.')
            break
        row = {'name': name, 'device_id': name, 'source': 'diskutil list physical / info plist',
               'model': None, 'size_bytes': None, 'bus_type': None, 'health_status': None,
               'smart_passed': None, 'temp_c': None, 'wear_percent_used': None, 'power_on_hours': None,
               'uncorrected_read_errors': None, 'uncorrected_write_errors': None}
        _identity(row)  # diskN and filesystem UUIDs cannot identify physical hardware across scans.
        result['storage'].append(row)
        try:
            info = _invoke(tools['diskutil'], ['info', '-plist', name], budget, command, seconds=2)
            if info['exit_code'] != 0:
                raise ValueError('Disk information was unavailable.')
            data = plistlib.loads(info['stdout'])
            if (not isinstance(data, dict) or data.get('DeviceIdentifier') != name or data.get('Whole') is not True
                    or data.get('VirtualOrPhysical') not in (None, 'Physical')):
                raise ValueError('The reported device is not this whole physical disk.')
            row.update(name=_text(data.get('MediaName')) or name, model=_text(data.get('MediaName')),
                       size_bytes=_number(data.get('TotalSize'), maximum=10 ** 18, integer=True),
                       bus_type=_text(data.get('BusProtocol')))
            status = data.get('SMARTStatus')
            row['smart_passed'] = True if status == 'Verified' else False if status == 'Failing' else None
            row['health_status'] = status if status in ('Verified', 'Failing') else None
            if row['smart_passed'] is None:
                row['health_unavailable_reason'] = 'This native disk interface does not expose a verified SMART health status.'
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError):
            row['health_unavailable_reason'] = 'Native information for this whole physical disk could not be verified.'
    result['capabilities']['storage_health'] = ('unavailable' if invalid_disk and not result['storage'] else 'not_present' if not result['storage'] else 'available'
        if all(row['smart_passed'] is not None for row in result['storage']) else 'partial' if any(row['smart_passed'] is not None for row in result['storage']) else 'not_exposed')


def collect_native_hardware_health(platform_name, cancel_event=None, *, command=None,
                                  battery_root=Path('/sys/class/power_supply'),
                                  block_root=Path('/sys/class/block'), max_seconds=12, clock=time.monotonic):
    """Collect one native snapshot; injected commands are fixture-only byte results.

    Command contract: ``(argv, timeout=..., cancel_event=...)`` returns an object
    with integer ``exit_code`` and bounded byte ``stdout``/``stderr``. Actual
    commands always use fixed root-trusted executables and exact read-only args.
    """
    if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or not 1 <= max_seconds <= 30:
        raise ValueError('Native hardware health collection requires a 1 to 30 second budget.')
    result = {'schema': 1, 'platform': platform_name, 'captured_at': time.time(), 'read_only': True,
              'status': 'completed', 'storage': [], 'batteries': [], 'errors': [],
              'capabilities': {'storage_health': 'unavailable', 'battery_health': 'unavailable',
                               'volume_mapping': 'not_exposed'}}
    if platform_name not in _TOOLS:
        return dict(result, status='unavailable')
    budget = _Budget(max_seconds, cancel_event, clock)
    injected = command is not None
    command = command or _bounded_command
    tools = {name: paths[0] if injected else _trusted_tool(platform_name, name)
             for name, paths in _TOOLS[platform_name].items()}
    operations = [('Battery health', lambda: _linux_batteries(result, Path(battery_root), budget)),
                  ('Physical-disk health', lambda: _linux_storage(result, tools, Path(block_root), budget, command))] if platform_name == 'Linux' else [
                  ('Battery health', lambda: _mac_batteries(result, tools, budget, command)),
                  ('Physical-disk health', lambda: _mac_storage(result, tools, budget, command))]
    for label, operation in operations:
        try:
            budget.check()
            operation()
        except InterruptedError:
            result['status'] = 'cancelled'
            break
        except TimeoutError:
            result['errors'].append(label + ' reached the collection time budget; remaining counters are unknown.')
            result['status'] = 'partial'
            if budget.clock() >= budget.deadline:
                break
        except (OSError, ValueError, TypeError, KeyError, RecursionError, ExpatError):
            result['errors'].append(label + ' could not be read from its bounded native source.')
            result['status'] = 'partial'
    for collection in ('batteries', 'storage'):
        identities = [row.get('identity_hash') for row in result[collection] if row.get('identity_hash')]
        repeated = {identity for identity in identities if identities.count(identity) > 1}
        for row in result[collection]:
            if row.get('identity_hash') in repeated:
                _identity(row)
                row['identity_unavailable_reason'] = 'The native hardware identity was duplicated; scan history cannot be joined safely.'
    if result['status'] == 'completed' and (result['errors'] or any(value in ('unavailable', 'partial', 'not_exposed')
            for key, value in result['capabilities'].items() if key != 'volume_mapping')):
        result['status'] = 'partial'
    if cancel_event is not None and cancel_event.is_set():
        result['status'] = 'cancelled'
    result['collection_bytes'] = budget.bytes
    return result
