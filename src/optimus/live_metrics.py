"""Cached, read-only progress metrics; passive traffic is not a speed test.

Construction and status() never query the machine. The owner starts the sampler
only while progress is visible and stops it on hide, completion or shutdown.
Worker CPU is an identified Optimus native thread's fraction of total machine
CPU, excluding PowerShell, Windows Update and vendor installer processes.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


SOURCES = (
    'https://psutil.readthedocs.io/stable/',
    'https://learn.microsoft.com/en-us/powershell/module/netadapter/get-netadapter',
    'https://learn.microsoft.com/en-us/windows/win32/fwp/wmi/netadaptercimprov/msft-netadapter',
)
_SCOPE = 'Optimus thread only; percent of total computer CPU. External installers are excluded.'


def _number(value, low=0, high=None):
    return (type(value) in (int, float) and math.isfinite(value) and value >= low
            and (high is None or value <= high))


def _safe_name(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and all(ord(c) >= 32 and ord(c) != 127 for c in value))


def _unavailable(reason='Metrics have not started.'):
    return {'running': False, 'sampled_at': None, 'computer': {'cpu_percent': None,
        'cpu_frequency_mhz': None, 'memory_percent': None, 'pressure': 'unknown',
        'high_cpu': False, 'high_memory': False, 'reason': reason},
        'network': {'kind': 'unknown', 'adapter': None, 'receive_mbps': None,
            'send_mbps': None, 'link_mbps': None, 'is_up': None,
            'internet_state': 'unverified', 'reason': reason}, 'workers': {}}


def identify_wifi(*, system=None, runner=subprocess.run, linux_root=Path('/sys/class/net')):
    """Bounded passive interface identification. Never infer Wi-Fi from a name.

    Windows uses physical media codes, Linux the wireless sysfs directory and
    macOS its hardware-port mapping. Failure leaves ordinary Network labels.
    The Windows connectivity value is a cached OS report, not an Internet test.
    """
    system = system or sys.platform
    report = {'wifi_adapters': [], 'internet_by_adapter': {},
              'reason': 'Wi-Fi identity could not be verified; displaying one network adapter.'}
    try:
        if system == 'win32':
            executable = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            command = ("$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
                "$wifi=@(Get-NetAdapter -Physical | Where-Object {[int]$_.NdisPhysicalMedium -in @(1,9)} | ForEach-Object {$_.Name});"
                "$profiles=@(Get-NetConnectionProfile -ErrorAction SilentlyContinue | ForEach-Object {"
                "@{adapter=$_.InterfaceAlias;internet=([string]$_.IPv4Connectivity -eq 'Internet' -or [string]$_.IPv6Connectivity -eq 'Internet')}});"
                "@{wifi_adapters=$wifi;profiles=$profiles}|ConvertTo-Json -Depth 4 -Compress")
            environment = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
            environment['PSModulePath'] = str(executable.parent / 'Modules')
            result = runner([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command],
                timeout=3, capture_output=True, text=True, encoding='utf-8', errors='replace',
                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=environment)
            if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > 65536:
                return report
            value = json.loads(result.stdout.lstrip('\ufeff'))
            names = value.get('wifi_adapters') if isinstance(value, dict) else None
            if not isinstance(names, list) or len(names) > 64 or any(not _safe_name(name) for name in names):
                return report
            report['wifi_adapters'] = list(dict.fromkeys(names))
            profiles = value.get('profiles', [])
            if isinstance(profiles, list) and len(profiles) <= 128:
                for profile in profiles:
                    if isinstance(profile, dict) and _safe_name(profile.get('adapter')) and type(profile.get('internet')) is bool:
                        report['internet_by_adapter'][profile['adapter']] = ('os_reported_internet' if profile['internet']
                                                                           else 'os_reported_no_internet')
        elif system.startswith('linux'):
            report['wifi_adapters'] = [path.name for path in list(Path(linux_root).iterdir())[:128]
                                      if _safe_name(path.name) and (path / 'wireless').is_dir()]
        elif system == 'darwin':
            result = runner(['/usr/sbin/networksetup', '-listallhardwareports'], timeout=3,
                capture_output=True, text=True, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
            if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > 65536:
                return report
            wifi = False
            for line in result.stdout.splitlines():
                if line.startswith('Hardware Port: '):
                    wifi = line[len('Hardware Port: '):] in ('Wi-Fi', 'AirPort')
                elif wifi and line.startswith('Device: ') and _safe_name(line[len('Device: '):]):
                    report['wifi_adapters'].append(line[len('Device: '):])
        report['reason'] = ('Wi-Fi adapter identified by the operating system.' if report['wifi_adapters'] else
                            'No Wi-Fi adapter was identified; displaying one network adapter.')
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        pass
    return report


def _read_system():
    import psutil
    value = {'errors': []}
    def read(name, operation):
        try:
            value[name] = operation()
        except (OSError, ValueError, RuntimeError, psutil.Error):
            value[name] = None
            value['errors'].append(name)
    def cpu():
        fields = psutil.cpu_times()._asdict()
        total = sum(v for key, v in fields.items() if key not in ('guest', 'guest_nice'))
        return {'total': total, 'idle': fields.get('idle', 0) + fields.get('iowait', 0)}
    read('cpu_times', cpu)
    read('cpu_count', lambda: psutil.cpu_count(logical=True))
    read('cpu_frequency_mhz', lambda: getattr(psutil.cpu_freq(), 'current', None))
    read('memory_percent', lambda: psutil.virtual_memory().percent)
    read('threads', lambda: {row.id: row.user_time + row.system_time for row in psutil.Process().threads()})
    read('network_counters', lambda: {name: {'received': row.bytes_recv, 'sent': row.bytes_sent}
         for name, row in psutil.net_io_counters(pernic=True, nowrap=False).items()})
    read('network_stats', lambda: {name: {'is_up': row.isup, 'link_mbps': row.speed}
         for name, row in psutil.net_if_stats().items()})
    return value


class LiveMetrics:
    def __init__(self, backend, interval=2, *, reader=None, wifi_provider=None, clock=time.monotonic,
                 thread_factory=threading.Thread):
        if not _number(interval, .5, 30):
            raise ValueError('Use a metrics interval between 0.5 and 30 seconds.')
        self.backend, self.interval = backend, float(interval)
        self._reader, self._wifi_provider = reader or _read_system, wifi_provider or identify_wifi
        self._clock, self._thread_factory = clock, thread_factory
        self._lock, self._wake = threading.RLock(), threading.Event()
        self._thread, self._desired, self._generation = None, False, 0
        self._status = _unavailable()
        self._reset()

    def _reset(self):
        self._previous = None
        self._worker_previous = {}
        self._adapter = None
        self._wifi = None
        self._wifi_at = None

    def status(self):
        with self._lock:
            return copy.deepcopy(self._status)

    def start(self):
        with self._lock:
            if self._desired:
                return False
            self._desired = True
            self._generation += 1
            self._reset()
            self._status = _unavailable('Waiting for two observed samples; no activity percentage is assumed.')
            self._status['running'] = True
            self._wake.set()
            if self._thread is None or not self._thread.is_alive():
                self._thread = self._thread_factory(target=self._run, name='Optimus live metrics', daemon=True)
                try:
                    self._thread.start()
                except BaseException:
                    self._desired, self._thread = False, None
                    self._status = _unavailable('The metrics sampler could not start.')
                    raise
            return True

    def stop(self, wait=False):
        with self._lock:
            self._desired = False
            self._generation += 1
            self._status['running'] = False
            self._wake.set()
            thread = self._thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        return thread is None or not thread.is_alive()

    def _navigation(self):
        try:
            method = getattr(self.backend, 'workflow_navigation_status', None)
            value = method() if callable(method) else {}
            return value if isinstance(value, dict) else {}
        except (AttributeError, OSError, RuntimeError, ValueError, TypeError):
            return {}

    def _run(self):
        while True:
            with self._lock:
                if not self._desired:
                    self._thread = None
                    return
                generation = self._generation
                self._wake.clear()
            self._sample(generation)
            self._wake.wait(self.interval)

    def _sample(self, generation=None):
        """One worker-thread sample; exposed privately for injected fixture tests."""
        before = self._navigation()
        try:
            raw = self._reader()
            now = self._clock()
            with self._lock:
                if generation is not None and (generation != self._generation or not self._desired):
                    return
                needs_wifi = self._wifi is None or self._wifi_at is None or now - self._wifi_at > 300
            wifi = self._wifi_provider() if needs_wifi else None
            after = self._navigation()
            with self._lock:
                if generation is not None and (generation != self._generation or not self._desired):
                    return
                if needs_wifi:
                    self._wifi = wifi if isinstance(wifi, dict) else {}
                    self._wifi_at = now
                self._status = self._derive(raw, before, after, now)
                self._status['running'] = self._desired
        except Exception:
            with self._lock:
                if generation is None or generation == self._generation and self._desired:
                    self._previous, self._worker_previous = None, {}
                    self._status = _unavailable('The read-only metrics sample is unavailable; no values were invented.')
                    self._status['running'] = self._desired

    def _derive(self, raw, before, after, now):
        if not isinstance(raw, dict) or not _number(now):
            raise ValueError('Invalid metrics sample.')
        result = _unavailable('Waiting for an observed interval.')
        result['sampled_at'] = now
        previous = self._previous
        dt = now - previous['at'] if previous is not None else None
        valid_dt = _number(dt, .05, max(10, self.interval * 3))
        computer = result['computer']
        computer['memory_percent'] = raw.get('memory_percent') if _number(raw.get('memory_percent'), 0, 100) else None
        computer['cpu_frequency_mhz'] = raw.get('cpu_frequency_mhz') if _number(raw.get('cpu_frequency_mhz'), .01, 100000) else None
        cpu, old_cpu = raw.get('cpu_times'), previous.get('cpu_times') if previous else None
        if valid_dt and isinstance(cpu, dict) and isinstance(old_cpu, dict):
            if all(_number(value) for row in (cpu, old_cpu) for value in (row.get('total'), row.get('idle'))):
                total, idle = cpu['total'] - old_cpu['total'], cpu['idle'] - old_cpu['idle']
                if total > 0 and 0 <= idle <= total:
                    computer['cpu_percent'] = round((total - idle) * 100 / total, 1)
        computer['high_cpu'] = computer['cpu_percent'] is not None and computer['cpu_percent'] >= 90
        computer['high_memory'] = computer['memory_percent'] is not None and computer['memory_percent'] >= 90
        computer['pressure'] = ('high' if computer['high_cpu'] or computer['high_memory'] else
                                'normal' if computer['cpu_percent'] is not None and computer['memory_percent'] is not None else 'unknown')
        computer['reason'] = ('Observed high CPU or memory use can limit available resources; this does not diagnose a fault.'
            if computer['pressure'] == 'high' else 'OS activity readings do not prove maximum performance. CPU frequency may be nominal rather than instantaneous.')
        result['network'] = self._network(raw, previous, dt if valid_dt else None)
        result['workers'] = self._workers(raw, before, after, now, valid_dt)
        self._previous = {'at': now, 'cpu_times': copy.deepcopy(cpu),
                          'network_counters': copy.deepcopy(raw.get('network_counters', {}))}
        return result

    def _network(self, raw, previous, dt):
        network = _unavailable()['network']
        counters, stats = raw.get('network_counters'), raw.get('network_stats')
        if not isinstance(counters, dict) or not isinstance(stats, dict) or len(counters) > 256 or len(stats) > 256:
            network['reason'] = 'Network interface counters are unavailable.'
            return network
        available = [name for name in counters if _safe_name(name) and isinstance(stats.get(name), dict)
                     and stats[name].get('is_up') is True and name.casefold() not in ('lo', 'lo0', 'loopback pseudo-interface 1')]
        wifi = self._wifi or {}
        known_wifi = wifi.get('wifi_adapters', [])
        known_wifi = known_wifi if isinstance(known_wifi, list) and len(known_wifi) <= 64 else []
        preferred = [name for name in available if name in known_wifi]
        choices = preferred or available
        if not choices:
            self._adapter = None
            network.update(is_up=False if stats else None,
                           reason='No active adapter was reported; Internet availability is unverified.')
            return network
        adapter = self._adapter if self._adapter in choices else sorted(choices)[0]
        self._adapter = adapter
        network.update(kind='wifi' if adapter in preferred else 'network', adapter=adapter, is_up=True,
                       link_mbps=stats[adapter].get('link_mbps') if _number(stats[adapter].get('link_mbps'), .01, 1000000) else None)
        reports = wifi.get('internet_by_adapter', {})
        state = reports.get(adapter) if isinstance(reports, dict) else None
        network['internet_state'] = state if state in ('os_reported_internet', 'os_reported_no_internet') else 'unverified'
        network['internet_report_age_seconds'] = (max(0, self._clock() - self._wifi_at) if self._wifi_at is not None else None)
        current = counters[adapter]
        old = previous.get('network_counters', {}).get(adapter) if previous else None
        if dt is not None and isinstance(current, dict) and isinstance(old, dict):
            for field, output in (('received', 'receive_mbps'), ('sent', 'send_mbps')):
                if (type(current.get(field)) is int and type(old.get(field)) is int
                        and current[field] >= old[field] >= 0):
                    network[output] = round((current[field] - old[field]) * 8 / dt / 1_000_000, 3)
        network['reason'] = ('Passive traffic on the OS-identified Wi-Fi adapter. ' if preferred else
            'Wi-Fi could not be verified; passive traffic on one selected network adapter. ') + (
            'RX/TX are current traffic, not Internet capacity or a speed test. Link speed and connectivity are OS reports; Internet access was not independently tested.')
        return network

    @staticmethod
    def _bindings(snapshot):
        if snapshot.get('active') is not True or not isinstance(snapshot.get('jobs'), list):
            return {}
        result = {}
        for job in snapshot['jobs'][:12]:
            if not isinstance(job, dict) or not _safe_name(job.get('id')):
                continue
            current = job.get('current')
            if job.get('active') is not True or not isinstance(current, dict):
                result[job['id']] = None
                continue
            tid = current.get('thread_id')
            result[job['id']] = (snapshot.get('session_id'), current.get('key'), tid) if type(tid) is int and tid > 0 else None
        return result

    def _workers(self, raw, before, after, now, valid_dt):
        first, last = self._bindings(before), self._bindings(after)
        threads, count = raw.get('threads'), raw.get('cpu_count')
        threads = threads if isinstance(threads, dict) and len(threads) <= 4096 else {}
        valid_count = type(count) is int and 0 < count <= 4096
        owners = {}
        for identity, binding in last.items():
            if binding is not None:
                owners.setdefault(binding[2], []).append(identity)
        result, upcoming = {}, {}
        for identity, binding in last.items():
            row = {'cpu_percent': None, 'scope': _SCOPE,
                   'reason': 'No stable current native worker thread is available.'}
            result[identity] = row
            if binding is None or first.get(identity) != binding or not valid_count:
                continue
            duplicates = owners[binding[2]]
            if len(duplicates) > 1 and (identity == 'maintenance' or len([job for job in duplicates if job != 'maintenance']) > 1):
                row['reason'] = 'This native thread is shared or ambiguous; CPU is not counted twice.'
                continue
            cpu = threads.get(binding[2])
            if not _number(cpu):
                row['reason'] = 'The current worker thread has exited or its CPU counters are unavailable.'
                continue
            previous = self._worker_previous.get(identity)
            upcoming[identity] = {'binding': binding, 'at': now, 'cpu': cpu, 'count': count}
            row['reason'] = 'Waiting for a second sample of this exact worker thread. External installer CPU is excluded.'
            if valid_dt and previous and previous['binding'] == binding and previous['count'] == count:
                elapsed, used = now - previous['at'], cpu - previous['cpu']
                if elapsed > 0 and 0 <= used <= elapsed * 1.2:
                    row.update(cpu_percent=round(min(100, used * 100 / elapsed / count), 2),
                               reason='Observed Optimus thread CPU only. Waiting on PowerShell, downloads or vendor installers can correctly show 0%.')
        self._worker_previous = upcoming
        return result
