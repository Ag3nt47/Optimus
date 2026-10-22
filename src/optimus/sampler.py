"""Opt-in background resource sampling. It never stops or tunes applications."""
from __future__ import annotations
import copy
import sys
import threading
import time
import psutil

class ResourceSampler:
    def __init__(self):
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.interval = 5
        self.latest = None
        self._previous_io = {}
        self._previous_cpu = {}
        self._previous_time = None

    def sample(self):
        now = time.time()
        memory = psutil.virtual_memory()
        errors = []
        try:
            io = psutil.disk_io_counters()
        except (psutil.Error, OSError, AttributeError, NotImplementedError):
            io = None
            errors.append('System disk I/O counters are unavailable.')
        try:
            network = psutil.net_io_counters()
        except (psutil.Error, OSError, AttributeError, NotImplementedError):
            network = None
            errors.append('System network counters are unavailable.')
        processes = []
        for process in psutil.process_iter(['pid', 'name', 'memory_info', 'create_time', 'nice']):
            try:
                item = process.info
                record = {'pid': item['pid'], 'name': item['name'], 'rss': item['memory_info'].rss,
                          'private': getattr(item['memory_info'], 'private', 0 if sys.platform == 'win32' else None), 'cpu_percent': 0.0,
                          'priority': str(item['nice']), 'create_time': item['create_time']}
                key = (item['pid'], item['create_time'])
                try:
                    cpu = process.cpu_times()
                    cpu_total = cpu.user + cpu.system
                    previous_cpu = self._previous_cpu.get(key)
                    if previous_cpu:
                        record['cpu_percent'] = max(0.0, min(100.0, (cpu_total - previous_cpu[1]) / max(0.01, now - previous_cpu[0]) / max(1, psutil.cpu_count()) * 100))
                    self._previous_cpu[key] = (now, cpu_total)
                except (psutil.Error, OSError, AttributeError, NotImplementedError):
                    pass
                try:
                    counters = process.io_counters()
                    record.update(read_bytes=counters.read_bytes, write_bytes=counters.write_bytes)
                    key = (item['pid'], item['create_time'])
                    previous = self._previous_io.get(key)
                    if previous:
                        elapsed = max(0.01, now - previous[0])
                        record['read_bytes_per_second'] = max(0, counters.read_bytes - previous[1]) / elapsed
                        record['write_bytes_per_second'] = max(0, counters.write_bytes - previous[2]) / elapsed
                    self._previous_io[key] = (now, counters.read_bytes, counters.write_bytes)
                except (psutil.Error, OSError, AttributeError, NotImplementedError):
                    pass
                processes.append(record)
            except (psutil.Error, OSError, AttributeError):
                continue
        alive = {(item['pid'], item['create_time']) for item in processes}
        self._previous_io = {key: value for key, value in self._previous_io.items() if key in alive}
        self._previous_cpu = {key: value for key, value in self._previous_cpu.items() if key in alive}
        result = {'timestamp': now, 'cpu_percent': psutil.cpu_percent(None),
                  'memory': {'total': memory.total, 'available': memory.available, 'percent': memory.percent},
                  'disk_io': dict(io._asdict()) if io else {}, 'network': dict(network._asdict()) if network else {},
                  'processes': sorted(processes, key=lambda item: item['rss'], reverse=True)[:60],
                  'process_count': len(processes), 'errors': errors,
                  'note': 'The first CPU/IO sample establishes a baseline. Samples include the current diagnostic activity.'}
        return result

    def _run(self, stop_event):
        while not stop_event.is_set():
            try:
                result = self.sample()
                with self._lock:
                    self.latest = result
            except Exception as error:
                with self._lock:
                    self.latest = {'timestamp': time.time(), 'errors': [str(error)]}
            stop_event.wait(self.interval)

    def start(self, interval=5):
        with self._lifecycle_lock:
            return self._start_locked(interval)

    def _start_locked(self, interval=5):
        interval = int(interval)
        if not 2 <= interval <= 60:
            raise ValueError('Background interval must be 2 to 60 seconds.')
        self.stop()
        if self._thread and self._thread.is_alive():
            raise RuntimeError('The previous analysis sample is still stopping. Try again shortly.')
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name='Optimus resource sampler', daemon=True)
        self._thread.start()
        return self.status()

    def stop(self):
        with self._lifecycle_lock:
            return self._stop_locked()

    def _stop_locked(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=4)
        if not self._thread or not self._thread.is_alive():
            self._thread = None
        return self.status()

    def status(self):
        with self._lock:
            return {'enabled': bool(self._thread and self._thread.is_alive() and not self._stop.is_set()), 'stopping': bool(self._thread and self._thread.is_alive() and self._stop.is_set()), 'interval': self.interval, 'latest': copy.deepcopy(self.latest)}



