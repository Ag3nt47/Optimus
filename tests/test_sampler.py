"""Mock-only sampler tests: no real telemetry, process mutation, or system changes."""
import collections
import importlib.util
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch


class MockPsutilError(Exception):
    pass


mock_psutil = types.ModuleType('psutil')
mock_psutil.Error = MockPsutilError
_previous_psutil = sys.modules.get('psutil')
sys.modules['psutil'] = mock_psutil
module_path = Path(__file__).parents[1] / 'src' / 'optimus' / 'sampler.py'
sys.path.insert(0, str(module_path.parents[1]))
spec = importlib.util.spec_from_file_location('optimus._sampler_mock_test', module_path)
sampler_module = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(sampler_module)
finally:
    if _previous_psutil is None:
        sys.modules.pop('psutil', None)
    else:
        sys.modules['psutil'] = _previous_psutil


class MockProcess:
    def __init__(self, process_id=200, created=1234.5, rss=1048576):
        self.pid = process_id
        self.info = {'pid': process_id, 'name': 'Fixture.exe', 'memory_info': types.SimpleNamespace(rss=rss, private=2048), 'create_time': created, 'nice': 32}
        self.cpu_user, self.cpu_system = 1.0, 1.0
        self.read_bytes, self.write_bytes = 1000, 2000
    def cpu_times(self): return types.SimpleNamespace(user=self.cpu_user, system=self.cpu_system)
    def io_counters(self): return types.SimpleNamespace(read_bytes=self.read_bytes, write_bytes=self.write_bytes)
    def cpu_percent(self, *args, **kwargs): raise AssertionError('Sampler must own CPU-time baselines independently of Process.cpu_percent')


class SamplerTests(unittest.TestCase):
    def setUp(self):
        self.process = MockProcess()
        self.processes = [self.process]
        self.sampler = sampler_module.ResourceSampler()
        counters = collections.namedtuple('Counters', ['read_bytes', 'write_bytes'])
        mock_psutil.virtual_memory = lambda: types.SimpleNamespace(total=16000, available=4000, percent=75)
        mock_psutil.disk_io_counters = lambda: counters(100, 200)
        mock_psutil.net_io_counters = lambda: counters(300, 400)
        mock_psutil.cpu_count = lambda *args, **kwargs: 4
        mock_psutil.cpu_percent = lambda *args, **kwargs: 25.0
        mock_psutil.process_iter = lambda *args, **kwargs: list(self.processes)
        self.addCleanup(self.sampler.stop)

    def test_read_only_sample_baseline(self):
        with patch.object(sampler_module.time, 'time', return_value=10.0):
            snapshot = self.sampler.sample()
        self.assertEqual(snapshot['processes'][0]['cpu_percent'], 0)
        self.assertNotIn('read_bytes_per_second', snapshot['processes'][0])
        self.assertEqual(snapshot['memory']['percent'], 75)
        self.assertEqual(snapshot['network']['read_bytes'], 300)
        self.assertEqual(snapshot['process_count'], 1)

    def test_cpu_and_io_rates_use_owned_baselines(self):
        with patch.object(sampler_module.time, 'time', return_value=10.0): self.sampler.sample()
        self.process.cpu_user += 2.0
        self.process.read_bytes += 1000
        self.process.write_bytes += 2000
        with patch.object(sampler_module.time, 'time', return_value=12.0): snapshot = self.sampler.sample()
        process = snapshot['processes'][0]
        self.assertAlmostEqual(process['cpu_percent'], 25.0)
        self.assertEqual(process['read_bytes_per_second'], 500)
        self.assertEqual(process['write_bytes_per_second'], 1000)

    def test_pid_reuse_starts_new_baseline(self):
        with patch.object(sampler_module.time, 'time', return_value=10.0): self.sampler.sample()
        self.processes = [MockProcess(process_id=200, created=2000.0)]
        with patch.object(sampler_module.time, 'time', return_value=12.0): snapshot = self.sampler.sample()
        self.assertEqual(snapshot['processes'][0]['cpu_percent'], 0)
        self.assertNotIn('read_bytes_per_second', snapshot['processes'][0])
        self.assertNotIn((200, 1234.5), self.sampler._previous_io)
        self.assertNotIn((200, 1234.5), self.sampler._previous_cpu)

    def test_counter_reset_never_produces_negative_rates(self):
        with patch.object(sampler_module.time, 'time', return_value=10.0): self.sampler.sample()
        self.process.cpu_user = 0
        self.process.cpu_system = 0
        self.process.read_bytes = 0
        self.process.write_bytes = 0
        with patch.object(sampler_module.time, 'time', return_value=12.0): snapshot = self.sampler.sample()
        process = snapshot['processes'][0]
        self.assertEqual(process['cpu_percent'], 0)
        self.assertEqual(process['read_bytes_per_second'], 0)
        self.assertEqual(process['write_bytes_per_second'], 0)

    def test_status_is_a_defensive_copy(self):
        self.sampler.latest = {'processes': [{'pid': 200}]}
        snapshot = self.sampler.status()
        snapshot['latest']['processes'][0]['pid'] = 999
        self.assertEqual(self.sampler.status()['latest']['processes'][0]['pid'], 200)

    def test_invalid_interval_does_not_start_worker(self):
        for interval in (0, 1, 61):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError): self.sampler.start(interval)
        self.assertFalse(self.sampler.status()['enabled'])

    def test_start_stop_is_explicit_and_stops_sampling(self):
        sampled = threading.Event()
        def sample():
            sampled.set()
            return {'fixture': True}
        with patch.object(self.sampler, 'sample', side_effect=sample):
            self.assertFalse(self.sampler.status()['enabled'])
            self.sampler.start(2)
            self.assertTrue(sampled.wait(1))
            self.assertTrue(self.sampler.status()['enabled'])
            result = self.sampler.stop()
            self.assertFalse(result['enabled'])
            self.assertFalse(result.get('stopping', False))

    def test_blocked_stop_prevents_replacement_worker(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow_sample():
            calls.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return {'fixture': True}
        with patch.object(self.sampler, 'sample', side_effect=slow_sample):
            self.sampler.start(2)
            self.assertTrue(entered.wait(1))
            old_thread = self.sampler._thread
            original_join = old_thread.join
            # A nonblocking simulated join timeout reproduces an OS call still in flight.
            old_thread.join = lambda timeout=None: None
            try:
                result = self.sampler.stop()
                self.assertFalse(result['enabled'])
                self.assertTrue(result.get('stopping'))
                with self.assertRaises(RuntimeError): self.sampler.start(2)
                self.assertEqual(len(calls), 1)
                release.set()
                original_join(timeout=1)
                self.assertFalse(old_thread.is_alive())
            finally:
                release.set()
                old_thread.join = original_join
                original_join(timeout=1)
            self.sampler.start(2)
            self.sampler.stop()

    def test_concurrent_start_requests_never_overlap_workers(self):
        original_thread = threading.Thread
        factory_gate = threading.Barrier(2)
        activity_lock = threading.Lock()
        activity = {'active': 0, 'peak': 0}
        errors = []
        def sample():
            with activity_lock:
                activity['active'] += 1
                activity['peak'] = max(activity['peak'], activity['active'])
            time.sleep(0.02)
            with activity_lock:
                activity['active'] -= 1
            return {'fixture': True}
        def factory(*args, **kwargs):
            try: factory_gate.wait(timeout=0.1)
            except threading.BrokenBarrierError: pass
            return original_thread(*args, **kwargs)
        def start():
            try: self.sampler.start(2)
            except Exception as exc: errors.append(str(exc))
        starters = [original_thread(target=start) for _ in range(2)]
        with patch.object(self.sampler, 'sample', side_effect=sample), patch.object(sampler_module.threading, 'Thread', side_effect=factory):
            for item in starters: item.start()
            for item in starters: item.join(timeout=1)
            self.assertTrue(all(not item.is_alive() for item in starters))
            self.sampler.stop()
        self.assertEqual(errors, [])
        self.assertEqual(activity['peak'], 1)

    def test_worker_exception_is_visible_without_process_actions(self):
        called = threading.Event()
        def broken_sample():
            called.set()
            raise RuntimeError('Synthetic sampler failure')
        with patch.object(self.sampler, 'sample', side_effect=broken_sample):
            self.sampler.start(2)
            self.assertTrue(called.wait(1))
            self.sampler.stop()
        self.assertIn('Synthetic sampler failure', self.sampler.status()['latest']['errors'][0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
