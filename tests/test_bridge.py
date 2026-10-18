"""Mock PowerShell transport tests; no subprocess or Windows operation is run."""
import importlib.util
import sys
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

module_path = Path(__file__).parents[1] / 'src' / 'optimus' / 'bridge.py'
sys.path.insert(0, str(module_path.parents[1]))
spec = importlib.util.spec_from_file_location('optimus._bridge_mock_test', module_path)
bridge_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_module)


class MockLines:
    def __init__(self): self.lines = queue.Queue()
    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None: return
            yield line


class MockInput:
    def __init__(self, process):
        self.process, self.closed, self.writes, self.on_flush = process, False, [], None
    def write(self, value): self.writes.append(value)
    def flush(self):
        if self.on_flush: self.on_flush()
    def close(self):
        self.closed = True
        self.process.stdout.lines.put(None)


class MockProcess:
    def __init__(self):
        self.pid = 10001
        self.returncode = None
        self.stdout, self.stderr = MockLines(), []
        self.stdin = MockInput(self)
        self.terminated, self.wait_calls = False, 0
    def poll(self): return self.returncode
    def wait(self, timeout=None):
        self.wait_calls += 1
        self.returncode = 0
        return 0
    def terminate(self):
        self.terminated = True
        self.returncode = -1
        self.stdout.lines.put(None)
    def reply(self, ok=True, result=None, error=None, request_id=None):
        payload = json.loads(self.stdin.writes[-1])
        response = {'id': request_id or payload['id'], 'ok': ok, 'result': result, 'error': error}
        self.stdout.lines.put(json.dumps(response) + '\n')


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='optimus-bridge-test-')
        self.bridge = bridge_module.PowerShellBridge(Path(self.temp.name))
        self.process = MockProcess()
        self.popen = patch.object(bridge_module.subprocess, 'Popen', return_value=self.process)
        self.popen_mock = self.popen.start()
        self.addCleanup(self.popen.stop)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.process.stdout.lines.put(None))

    def pending(self):
        with self.bridge._pending_lock: return dict(self.bridge._pending)

    def wait_for(self, condition):
        deadline = time.monotonic() + 1
        while not condition():
            if time.monotonic() >= deadline: self.fail('Mock transport condition timed out')
            time.sleep(0.005)

    def test_windows_child_uses_only_matching_os_module_path(self):
        inherited = {'SystemRoot': r'C:\Windows', 'PSMODULEPATH': r'C:\Codex\PS7\Modules',
                     'PSModulePath': r'C:\Core\Modules', 'psmodulepath': r'C:\User\Modules',
                     'OPTIMUS_TEST_MARKER': 'preserved'}
        original = dict(inherited)
        with patch.object(bridge_module.os, 'environ', inherited):
            self.bridge._start()
        child = self.popen_mock.call_args.kwargs['env']
        matching = [key for key in child if key.casefold() == 'psmodulepath']
        self.assertEqual(matching, ['PSModulePath'])
        self.assertEqual(Path(child['PSModulePath']), Path(r'C:\Windows') / 'System32/WindowsPowerShell/v1.0/Modules')
        self.assertNotIn('Codex', child['PSModulePath'])
        self.assertNotIn('Core', child['PSModulePath'])
        self.assertEqual(child['OPTIMUS_TEST_MARKER'], 'preserved')
        self.assertEqual(inherited, original)
        self.assertIsNot(child, inherited)

    def test_child_environment_cannot_mutate_parent_environment(self):
        inherited = {'SystemRoot': r'C:\Windows', 'PSMODULEPATH': r'C:\Host\Modules', 'PATH': 'fixture-path'}
        with patch.object(bridge_module.os, 'environ', inherited):
            self.bridge._start()
        child = self.popen_mock.call_args.kwargs['env']
        child['PATH'] = 'child-only-value'
        child['PSModulePath'] = 'child-only-modules'
        self.assertEqual(inherited['PATH'], 'fixture-path')
        self.assertEqual(inherited['PSMODULEPATH'], r'C:\Host\Modules')
        self.assertNotIn('PSModulePath', inherited)

    def test_timeout_remains_pending_and_close_never_terminates(self):
        with self.assertRaises(TimeoutError):
            self.bridge.request('driver_update_apply', timeout=0.01, id='fixture-update')
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(len(self.process.stdin.writes), 1)
        self.bridge.close()
        self.assertFalse(self.process.stdin.closed)
        self.assertEqual(self.process.wait_calls, 0)
        self.assertFalse(self.process.terminated)

    def test_late_actual_reply_releases_pending_and_allows_graceful_close(self):
        with self.assertRaises(TimeoutError): self.bridge.request('fixture_mutation', timeout=0.01)
        self.process.reply(result={'applied': True})
        self.wait_for(lambda: not self.pending())
        self.bridge.close()
        self.assertTrue(self.process.stdin.closed)
        self.assertEqual(self.process.wait_calls, 1)
        self.assertFalse(self.process.terminated)

    def test_normal_reply_and_payload_are_structured(self):
        tricky_path = 'C:\\fixture\\quoted"folder; do-not-execute'
        self.process.stdin.on_flush = lambda: self.process.reply(result={'fixture': True})
        result = self.bridge.request('storage_scan', root=tricky_path, timeout=1)
        self.assertEqual(result, {'fixture': True})
        payload = json.loads(self.process.stdin.writes[0])
        self.assertEqual(payload['arguments']['root'], tricky_path)
        self.assertFalse(self.pending())
        arguments = self.popen_mock.call_args.args[0]
        self.assertNotIn(tricky_path, arguments)
        self.assertNotIn('-Command', arguments)
        self.bridge.close()
        self.assertFalse(self.process.terminated)

    def test_failed_reply_is_not_replayed(self):
        self.process.stdin.on_flush = lambda: self.process.reply(ok=False, error='Fixture was refused')
        with self.assertRaisesRegex(RuntimeError, 'Fixture was refused'):
            self.bridge.request('fixture_change', timeout=1)
        self.assertEqual(len(self.process.stdin.writes), 1)
        self.assertFalse(self.pending())
        self.bridge.close()
        self.assertFalse(self.process.terminated)

    def test_engine_exit_clears_its_own_pending_and_reports_failure(self):
        def exit_instead():
            self.process.returncode = 1
            self.process.stdout.lines.put(None)
        self.process.stdin.on_flush = exit_instead
        with self.assertRaisesRegex(RuntimeError, 'engine exited'):
            self.bridge.request('fixture_change', timeout=1)
        self.assertFalse(self.pending())
        self.assertFalse(self.process.terminated)


if __name__ == '__main__':
    unittest.main(verbosity=2)
