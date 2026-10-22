"""Mock-only process tests. No real application is changed by this test suite."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch


class FakePsutilError(Exception):
    pass


fake_psutil = types.ModuleType('psutil')
fake_psutil.Error = FakePsutilError
fake_psutil.STATUS_STOPPED = 'stopped'
fake_psutil.NORMAL_PRIORITY_CLASS = 0x20
fake_psutil.ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
fake_psutil.HIGH_PRIORITY_CLASS = 0x80
_previous_psutil = sys.modules.get('psutil')
sys.modules['psutil'] = fake_psutil
module_path = Path(__file__).parents[1] / 'src' / 'optimus' / 'performance.py'
sys.path.insert(0, str(module_path.parents[1]))
spec = importlib.util.spec_from_file_location('optimus._performance_mock_test', module_path)
perf = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(perf)
finally:
    if _previous_psutil is None:
        sys.modules.pop('psutil', None)
    else:
        sys.modules['psutil'] = _previous_psutil


class FakeProcess:
    def __init__(self, process_id, name='app.exe', exe=r'C:\Apps\Example\app.exe', parents=None):
        self.pid = process_id
        self.image_name, self.image_path = name, exe
        self.created, self.owner, self.session = 1234.5, 'MACHINE\\User', 1
        self.parent_list = parents or []
        self.priority, self.current_status = 0x20, 'running'
        self.priority_calls, self.suspend_calls, self.resume_calls = [], 0, 0
        self.fail_suspend = False
        self.before_suspend = None
        self.affinity = [0, 1, 2, 3]
        self.affinity_calls = []
        self.before_affinity = None

    def name(self): return self.image_name
    def exe(self): return self.image_path
    def username(self): return self.owner
    def create_time(self): return self.created
    def ppid(self): return self.parent_list[0].pid if self.parent_list else 1
    def parents(self): return list(self.parent_list)
    def memory_info(self): return types.SimpleNamespace(rss=1048576)
    def oneshot(self): return contextlib.nullcontext()
    def status(self): return self.current_status
    def nice(self, value=None):
        if value is not None:
            self.priority = int(value)
            self.priority_calls.append(int(value))
        return self.priority
    def cpu_affinity(self, values=None):
        if values is not None:
            if self.before_affinity:
                self.before_affinity()
            self.affinity = list(values)
            self.affinity_calls.append(list(values))
        return list(self.affinity)
    def cpu_percent(self, interval=None): return 80.0
    def io_counters(self): return types.SimpleNamespace(read_bytes=1024, write_bytes=2048, read_count=3, write_count=4)
    def num_threads(self): return 5
    def num_handles(self): return 12
    def cpu_times(self): return types.SimpleNamespace(_asdict=lambda: {'user': 1.0, 'system': 2.0})
    def parent(self): return self.parent_list[0] if self.parent_list else None
    def children(self, recursive=False): return []
    def open_files(self): return []
    def net_connections(self, kind='all'): return []
    def cmdline(self): raise AssertionError('Process details must not request command lines')
    def suspend(self):
        if self.before_suspend:
            self.before_suspend()
        if self.fail_suspend:
            raise FakePsutilError('Simulated access failure')
        self.suspend_calls += 1
        self.current_status = 'stopped'
    def resume(self):
        self.resume_calls += 1
        self.current_status = 'running'


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.processes = {}
        self.services = set()
        self.ancestors = {100}
        self.temp = tempfile.TemporaryDirectory(prefix='optimus-performance-')
        self.state_path = Path(self.temp.name) / 'sleep.json'
        self.controller = perf.PerformanceController(self.state_path)
        fake_psutil.Process = self.lookup
        fake_psutil.process_iter = lambda: list(self.processes.values())
        fake_psutil.cpu_count = lambda logical=True: 4
        fake_psutil.win_service_iter = lambda: []
        self.patches = [
            patch.object(perf.os, 'getpid', return_value=100),
            patch.object(perf, '_controller', return_value=self.controller),
            patch.object(perf, '_current_user', return_value='machine\\user'),
            patch.object(perf, '_ancestor_ids', side_effect=lambda: set(self.ancestors)),
            patch.object(perf, '_session_id', side_effect=lambda process_id: self.processes[process_id].session),
            patch.object(perf, '_service_ids', side_effect=lambda **kwargs: set(self.services)),
            patch.dict(os.environ, {'LOCALAPPDATA': r'C:\Users\User\AppData\Local', 'ProgramFiles': r'C:\Program Files', 'ProgramW6432': r'C:\Program Files', 'SystemRoot': r'C:\Windows'}),
        ]
        for item in self.patches: item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.addCleanup(self.temp.cleanup)

    def lookup(self, process_id):
        try: return self.processes[int(process_id)]
        except KeyError: raise FakePsutilError('Process ended')

    def add(self, process_id=200, **kwargs):
        process = FakeProcess(process_id, **kwargs)
        self.processes[process_id] = process
        return process

    def codex(self, process_id=300):
        return self.add(process_id, name='codex.exe', exe=r'C:\Users\User\AppData\Local\OpenAI\Codex\bin\896a0166b774037a\codex.exe')

    def test_inventory_has_no_mutations(self):
        process = self.add()
        rows = perf.list_performance_candidates()
        self.assertEqual(rows[0]['memory_rss'], 1048576)
        self.assertFalse(rows[0]['protected'])
        self.assertEqual((process.priority_calls, process.suspend_calls, process.resume_calls), ([], 0, 0))
        self.assertFalse(self.state_path.exists())

    def test_inventory_collects_shared_guards_once_and_live_changes_recheck(self):
        first = self.add(200)
        second = self.add(201)
        with patch.object(perf, '_current_user', return_value='machine\\user') as owner, \
                patch.object(perf, '_ancestor_ids', return_value={100}) as ancestors, \
                patch.object(perf, '_service_ids', return_value=set()) as services, \
                patch.object(perf, '_session_id', return_value=1) as sessions, \
                patch.object(fake_psutil, 'cpu_count', return_value=4) as cpus:
            rows = perf.list_performance_candidates()
        self.assertEqual(len(rows), 2)
        for shared in (owner, ancestors, services, cpus):
            self.assertEqual(shared.call_count, 1)
        self.assertEqual(sessions.call_count, 2)
        self.assertTrue(all(row['can_sleep'] for row in rows))
        # Census snapshots are display evidence only. A newly service-owned PID
        # must be refused by the existing fresh mutation guard.
        self.services.add(first.pid)
        record = next(row for row in rows if row['pid'] == first.pid)
        with self.assertRaises(perf.PerformanceError):
            self.controller.sleep_process(record)
        self.assertEqual(first.suspend_calls + second.suspend_calls, 0)
        self.assertFalse(self.state_path.exists())

    def test_inventory_shared_guard_failures_stay_protected(self):
        self.add()
        cases = (
            ('_current_user', {'side_effect': perf.PerformanceError('Unavailable owner')}),
            ('_service_ids', {'return_value': None}),
            ('_ancestor_ids', {'return_value': {-1, 100}}),
        )
        for name, options in cases:
            with self.subTest(name=name), patch.object(perf, name, **options):
                row = perf.list_performance_candidates()[0]
                self.assertTrue(row['protected'])
                self.assertFalse(row['can_sleep'])
                self.assertFalse(row['can_set_priority'])

    def test_unavailable_cpu_count_retains_read_only_inventory(self):
        self.add()
        with patch.object(fake_psutil, 'cpu_count', return_value=None), \
                patch.object(perf.os, 'cpu_count', return_value=4):
            row = perf.list_performance_candidates()[0]
        self.assertEqual(row['cpu_percent'], 20.0)
        self.assertTrue(row['can_sleep'])

    def test_stale_pid_and_changed_path_rejected(self):
        process = self.add()
        record = perf._record(process)
        process.created += 1
        with self.assertRaises(perf.PerformanceError): perf.set_process_priority(record, 'High')
        process.created = record['create_time']
        process.image_path = r'C:\Apps\Other\app.exe'
        with self.assertRaises(perf.PerformanceError): self.controller.sleep_process(record)
        self.assertEqual(process.priority_calls, [])
        self.assertEqual(process.suspend_calls, 0)

    def test_other_owner_session_zero_service_and_windows_protected(self):
        for kind in ('owner', 'session', 'service', 'windows', 'security', 'ancestor'):
            with self.subTest(kind=kind):
                process = self.add()
                if kind == 'owner': process.owner = 'MACHINE\\Other'
                elif kind == 'session': process.session = 0
                elif kind == 'service': self.services.add(process.pid)
                elif kind == 'windows': process.image_path = r'C:\Windows\System32\ordinary.exe'
                elif kind == 'security': process.image_name = 'MsMpEng.exe'
                elif kind == 'ancestor': self.ancestors.add(process.pid)
                record = perf._record(process)
                self.assertTrue(record['protected'])
                with self.assertRaises(perf.PerformanceError): self.controller.sleep_process(record)
                with self.assertRaises(perf.PerformanceError): perf.set_process_priority(record, 'High')
                self.services.clear(); self.ancestors = {100}

    def test_realtime_cannot_be_requested(self):
        record = perf._record(self.add())
        with self.assertRaises(perf.PerformanceError): perf.set_process_priority(record, 'RealTime')
        with self.assertRaises(perf.PerformanceError): perf.performance_profile('RealTime')

    def test_bing_wallpaper_name_and_installation_helpers_are_preserved(self):
        variants = (
            ('BingWallpaper.exe', r'C:\Apps\Wallpaper\BingWallpaper.exe'),
            ('wallpaper.exe', r'C:\Users\User\Bing Wallpaper\wallpaper.exe'),
            ('update.exe', r'C:\Apps\Microsoft\BingWallpaper\update.exe'),
        )
        for name, executable in variants:
            with self.subTest(name=name, executable=executable):
                process = self.add(name=name, exe=executable)
                record = perf._record(process)
                self.assertTrue(record['protected'])
                self.assertFalse(record['can_sleep'])
                self.assertFalse(record['can_set_priority'])
                self.assertIn('Bing Wallpaper', record['reason'])
                # A caller cannot bypass the fresh native-process protection by
                # changing the inventory flags supplied with an action.
                record.update(protected=False, can_sleep=True, can_set_priority=True)
                with self.assertRaises(perf.PerformanceError): self.controller.sleep_process(record)
                with self.assertRaises(perf.PerformanceError): self.controller.set_process_priority(record, 'High')
                with self.assertRaises(perf.PerformanceError): self.controller.set_affinity(record, [0])
                self.assertEqual((process.suspend_calls, process.priority_calls, process.affinity_calls), (0, [], []))
                self.assertFalse(self.state_path.exists())

    def test_explicit_verified_codex_ancestor_priority_allowed_sleep_blocked(self):
        process = self.codex()
        process.priority = 0x100
        self.ancestors.add(process.pid)
        record = perf._record(process)
        self.assertTrue(record['protected'])
        self.assertTrue(record['can_set_priority'])
        result = perf.set_process_priority(record, 'High')
        self.assertEqual(result['old_priority'], 'RealTime')
        self.assertEqual(process.priority_calls, [0x80])
        with self.assertRaises(perf.PerformanceError): self.controller.sleep_process(record)
        self.assertEqual(process.suspend_calls, 0)

    def test_profile_install_path_and_optional_helpers(self):
        real = self.codex()
        fake = self.add(301, name='codex.exe', exe=r'C:\Untrusted\codex.exe')
        helper = self.add(302, name='node.exe', exe=r'C:\Program Files\nodejs\node.exe', parents=[real])
        unrelated = self.add(303)
        perf.performance_profile('High')
        self.assertEqual(real.priority_calls, [0x80])
        self.assertEqual(fake.priority_calls + helper.priority_calls + unrelated.priority_calls, [])
        perf.performance_profile('AboveNormal', include_tool_helpers=True)
        self.assertEqual(helper.priority_calls, [0x8000])
        self.assertEqual(fake.priority_calls + unrelated.priority_calls, [])
        with self.assertRaises(perf.PerformanceError): perf.performance_profile(root_pid=fake.pid)

    def test_sleep_intent_is_durable_before_suspend_and_recoverable(self):
        process = self.add()
        record = perf._record(process)
        def verify_pending():
            state = json.loads(self.state_path.read_text())
            self.assertEqual(next(iter(state['entries'].values()))['status'], 'pending')
        process.before_suspend = verify_pending
        self.controller.sleep_process(record)
        self.assertEqual(process.suspend_calls, 1)
        new_controller = perf.PerformanceController(self.state_path)
        self.assertEqual(new_controller.list_suspended()[0]['status'], 'sleeping')
        new_controller.wake_all()
        self.assertEqual(process.resume_calls, 1)
        self.assertEqual(new_controller.list_suspended(), [])

    def test_reused_pid_never_resumed(self):
        process = self.add()
        record = perf._record(process)
        self.controller.sleep_process(record)
        process.created += 10
        result = self.controller.wake_all()
        self.assertEqual(result[0]['status'], 'needs_attention')
        self.assertEqual(process.resume_calls, 0)
        self.assertEqual(len(self.controller.list_suspended()), 1)

    def test_external_suspension_not_claimed(self):
        process = self.add()
        process.current_status = 'stopped'
        with self.assertRaises(perf.PerformanceError): self.controller.sleep_process(perf._record(process))
        self.assertEqual(process.suspend_calls, 0)
        self.assertFalse(self.state_path.exists())

    def test_failed_suspend_intent_remains_recoverable(self):
        process = self.add()
        process.fail_suspend = True
        with self.assertRaises(perf.PerformanceError): self.controller.sleep_process(perf._record(process))
        self.assertEqual(self.controller.list_suspended()[0]['status'], 'pending')
        self.controller.wake_all()
        self.assertEqual(process.resume_calls, 0)
        self.assertEqual(self.controller.list_suspended(), [])

    def test_state_failure_prevents_suspend(self):
        process = self.add()
        with patch.object(self.controller, '_save', side_effect=OSError('No write permission')):
            with self.assertRaises(OSError): self.controller.sleep_process(perf._record(process))
        self.assertEqual(process.suspend_calls, 0)

    def test_corrupt_state_is_preserved(self):
        process = self.add()
        self.state_path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(perf.PerformanceError): self.controller.sleep_process(perf._record(process))
        self.assertEqual(process.suspend_calls, 0)
        self.assertEqual(self.state_path.read_text(), '{broken')

    def test_failed_final_state_write_remains_recoverable(self):
        process = self.add()
        original_save = self.controller._save
        saves = []
        def save_then_fail(state):
            saves.append(1)
            if len(saves) == 2:
                raise OSError('Simulated final commit failure')
            return original_save(state)
        with patch.object(self.controller, '_save', side_effect=save_then_fail):
            with self.assertRaises(OSError): self.controller.sleep_process(perf._record(process))
        self.assertEqual(process.suspend_calls, 1)
        recovered = perf.PerformanceController(self.state_path)
        self.assertEqual(recovered.list_suspended()[0]['status'], 'pending')
        recovered.wake_all()
        self.assertEqual(process.resume_calls, 1)

    def test_service_inventory_failure_fails_closed(self):
        process = self.add()
        with patch.object(perf, '_service_ids', return_value=None):
            record = perf._record(process)
            self.assertTrue(record['protected'])
            with self.assertRaises(perf.PerformanceError): self.controller.sleep_process(record)
            with self.assertRaises(perf.PerformanceError): perf.set_process_priority(record, 'High')
        self.assertEqual(process.suspend_calls, 0)
        self.assertEqual(process.priority_calls, [])

    def test_cpu_io_and_details_are_read_only(self):
        process = self.add()
        record = perf._record(process)
        self.assertEqual(record['cpu_percent'], 20.0)
        self.assertEqual(record['io_read_bytes'], 1024)
        self.assertEqual(record['affinity'], [0, 1, 2, 3])
        details = perf.get_process_details(record)
        self.assertEqual(details['cpu_times']['system'], 2.0)
        self.assertIsNone(details['open_file_count'])
        self.assertIsNone(details['network_connection_count'])
        self.assertTrue(any('not collected' in note for note in details['notes']))
        counts = perf.get_process_details(record, include_expensive_counts=True)
        self.assertEqual(counts['open_file_count'], 0)
        self.assertEqual(counts['network_connection_count'], 0)
        self.assertFalse(any('command' in key.casefold() for key in details))
        self.assertEqual(process.priority_calls + process.affinity_calls, [])

    def test_affinity_apply_and_cross_launch_restore(self):
        process = self.add()
        record = perf._record(process)
        def verify_pending():
            state = json.loads(self.state_path.read_text())
            self.assertIn(state['allocation_history'][-1]['status'], {'pending', 'restore_pending'})
        process.before_affinity = verify_pending
        change = self.controller.set_affinity(record, '1,2,2')
        self.assertEqual(process.affinity, [1, 2])
        restored = perf.PerformanceController(self.state_path)
        restored.restore_affinity(change['change_id'])
        self.assertEqual(process.affinity, [0, 1, 2, 3])
        self.assertEqual(restored.allocation_history()[-1]['status'], 'restored')

    def test_affinity_rejects_invalid_and_protected_records(self):
        process = self.add()
        record = perf._record(process)
        for cores in ([], [-1], [4], [1.2], [True], 'zero'):
            with self.subTest(cores=cores):
                with self.assertRaises(perf.PerformanceError): self.controller.set_affinity(record, cores)
        process.session = 0
        with self.assertRaises(perf.PerformanceError): self.controller.set_affinity(record, [0])
        self.assertEqual(process.affinity_calls, [])

    def test_affinity_restore_rejects_reused_pid(self):
        process = self.add()
        change = self.controller.set_affinity(perf._record(process), [0])
        process.created += 1
        with self.assertRaises(perf.PerformanceError): self.controller.restore_affinity(change['change_id'])
        self.assertEqual(process.affinity_calls, [[0]])

    def test_priority_journal_and_restore_cross_launch(self):
        process = self.add()
        record = perf._record(process)
        changed = perf.set_process_priority(record, 'High')
        self.assertEqual(process.priority, 0x80)
        restored = perf.PerformanceController(self.state_path)
        history = restored.allocation_history()
        self.assertEqual(history[-1]['Kind'], 'Priority')
        self.assertEqual(history[-1]['Status'], 'Applied')
        self.assertEqual(history[-1]['source'], 'python-allocation')
        self.assertEqual(history[-1]['Undo']['previous'], 0x20)
        restored.restore_allocation(changed['change_id'])
        self.assertEqual(process.priority_calls, [0x80, 0x20])
        self.assertEqual(restored.allocation_history()[-1]['Status'], 'Restored')

    def test_priority_journal_must_exist_before_change(self):
        process = self.add()
        record = perf._record(process)
        with patch.object(self.controller, '_save', side_effect=OSError('No disk space')):
            with self.assertRaises(OSError): perf.set_process_priority(record, 'High')
        self.assertEqual(process.priority_calls, [])

    def test_priority_restore_rejects_reused_pid(self):
        process = self.add()
        change = perf.set_process_priority(perf._record(process), 'AboveNormal')
        process.created += 100
        with self.assertRaises(perf.PerformanceError): self.controller.restore_priority(change['change_id'])
        self.assertEqual(process.priority_calls, [0x8000])

    def test_recorded_codex_original_realtime_can_be_restored_explicitly(self):
        process = self.codex()
        process.priority = 0x100
        change = self.controller.set_process_priority(perf._record(process), 'High')
        self.assertEqual(process.priority_calls, [0x80])
        self.controller.restore_priority(change['change_id'])
        self.assertEqual(process.priority_calls, [0x80, 0x100])
        ordinary = self.add(400)
        ordinary.priority = 0x100
        other = self.controller.set_process_priority(perf._record(ordinary), 'High')
        with self.assertRaises(perf.PerformanceError): self.controller.restore_priority(other['change_id'])
        self.assertEqual(ordinary.priority_calls, [0x80])

    def test_concurrent_sleep_only_suspends_once(self):
        process = self.add()
        record = perf._record(process)
        errors = []
        def request():
            try: self.controller.sleep_process(record)
            except Exception as exc: errors.append(str(exc))
        threads = [threading.Thread(target=request) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(process.suspend_calls, 1)
        self.assertEqual(len(self.controller.list_suspended()), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
