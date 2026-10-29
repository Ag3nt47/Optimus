"""Offline transport protocol and harmless native-console fixture children."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from optimus import winget_console as console


class FakeConsole:
    instances = []
    automatic = True
    terminal_nonce = None
    exit_code = 0
    launch_error = None
    post_launch_error = None
    flood = False

    def __init__(self):
        self.started, self.closed, self.pid = False, False, 4231
        self.exit = None
        self.channels = {'output': queue.Queue(), 'receipt': queue.Queue()}
        self.close_calls = []
        self.dispose_calls = 0
        self.events = []
        type(self).instances.append(self)

    def launch(self, nonce, arguments):
        if type(self).launch_error:
            raise type(self).launch_error
        self.started, self.nonce, self.arguments = True, nonce, list(arguments)
        self.events.append('launched')
        if type(self).post_launch_error:
            raise type(self).post_launch_error
        if type(self).flood:
            self.channels['output'].put(b'x' * (2 * 1024 ** 2 + 100))
        self.channels['output'].put(b'Downloading https://example.test/fake.exe\r512 KB / 1 MB\r')
        if type(self).automatic:
            threading.Timer(0.12, self.complete).start()

    def rows(self):
        first = {'schema': 1, 'nonce': type(self).terminal_nonce or self.nonce,
                 'supervisor_pid': self.pid, 'target_pid': 9876,
                 'argv_sha256': console._digest(self.arguments), 'event': 'started'}
        return [first, dict(first, event='quiescent', quiescent=True, console_client_count=1,
                            job_verified=True, job_active_processes=0, exit_code=type(self).exit_code)]

    def complete(self):
        self.events.append('target_exited')
        self.exit = 0
        self.channels['receipt'].put(('\n'.join(json.dumps(row) for row in self.rows()) + '\n').encode('ascii'))
        self.channels['receipt'].put(b'')

    def poll(self):
        return self.exit

    def read(self, channel):
        return self.channels[channel].get(timeout=10)

    def dispose_unused(self):
        if self.started:
            raise AssertionError('A launched target was disposed.')
        self.dispose_calls += 1
        self.closed = True
        for value in self.channels.values():
            value.put(b'')

    def close_quiescent(self):
        if self.exit is None:
            raise AssertionError('Console was closed before target exit.')
        self.close_calls.append(time.monotonic())
        self.events.append('console_closed')
        self.channels['output'].put(b'')

    def finish(self):
        self.closed = True
        self.events.append('finished')


class ConsoleProtocolTests(unittest.TestCase):
    def setUp(self):
        FakeConsole.instances = []
        FakeConsole.automatic = True
        FakeConsole.terminal_nonce = None
        FakeConsole.exit_code = 0
        FakeConsole.launch_error = None
        FakeConsole.post_launch_error = None
        FakeConsole.flood = False
        self.patch = mock.patch.object(console, '_ConsoleProcess', FakeConsole)
        self.patch.start()
        self.interpreter_patch = mock.patch.object(console, '_source_console_executable', return_value=sys.executable)
        self.interpreter_patch.start()

    def tearDown(self):
        self.patch.stop()
        self.interpreter_patch.stop()
        # Restore only fake sessions whose malformed receipt was deliberate.
        with console._RETAINED_LOCK:
            sessions = list(console._RETAINED)
        for session in sessions:
            if isinstance(session.native, FakeConsole):
                if session.native.exit is None:
                    session.native.complete()
                session.receipts = session.native.rows()
                for row in session.receipts:
                    row['nonce'] = session.nonce
                session.receipt_error = False
                session.receipt_eof.set()
                session.finished.wait(2)

    def test_live_native_progress_precedes_exit_and_actual_target_code_returns(self):
        events = []
        result = console.run_winget_console(['C:/fixtures/fake.exe', '--exact', 'quotes " and Unicode é'],
            timeout=2, progress_callback=lambda value: events.append((value, FakeConsole.instances[-1].exit)))
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.capture_complete)
        self.assertTrue(any(value.get('percent') == 50 and exit_code is None for value, exit_code in events))
        self.assertEqual(result.download_progress['downloaded_bytes'], 524288)
        self.assertEqual(FakeConsole.instances[-1].events,
            ['launched', 'target_exited', 'console_closed', 'finished'])
        self.assertEqual(FakeConsole.instances[-1].arguments, result.args)
        identity = result.native_diagnostics
        self.assertTrue(identity['verified'])
        self.assertEqual(identity['target_pid'], 9876)
        self.assertEqual(identity['argv_sha256'], console._digest(result.args))
        self.assertLessEqual(identity['started_at'], identity['finished_at'])
        from optimus.speed_enhancements import _current_owner
        self.assertEqual(identity['owner'], _current_owner())
        self.assertNotIn('target_pid', result.console_receipt)

    def test_nonzero_target_exit_does_not_become_supervisor_success(self):
        FakeConsole.exit_code = 3010
        result = console.run_winget_console(['C:/fixtures/fake.exe'], timeout=2, progress_callback=lambda _: None)
        self.assertEqual(result.returncode, 3010)
        self.assertEqual(result.stderr, '')

    def test_supplementary_diagnostic_failure_never_invalidates_completed_native_receipt(self):
        original = console._Session._terminal
        def terminal(session):
            if session.finished.is_set():
                raise ValueError('Supplementary identity unavailable')
            return original(session)
        with mock.patch.object(console._Session, '_terminal', terminal):
            result = console.run_winget_console(['C:/fixtures/fake.exe'], timeout=2, progress_callback=lambda _: None)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.capture_complete)
        self.assertTrue(result.console_receipt['verified'])
        self.assertFalse(hasattr(result, 'native_diagnostics'))

    def test_authenticated_resident_handoff_releases_queue_but_retains_console_owner(self):
        arguments = ['C:/fixtures/winget.exe', 'upgrade', '--id', 'Ollama.Ollama', '--exact',
                     '--source', 'winget', '--version', '2.0', '--silent', '--disable-interactivity']
        class ResidentConsole(FakeConsole):
            def launch(native, nonce, values):
                native.started, native.nonce, native.arguments = True, nonce, list(values)
                native.channels['output'].put(b'Native installation finished.\n')
                first = native.rows()[0]
                handoff = dict(first, event='resident_handoff', exit_code=0, quiescent=False,
                    job_verified=True, job_active_processes=1, console_client_count=1,
                    resident_proof={'schema': 1, 'kind': 'verified-ollama-resident-runtime-v1',
                        'package_id': 'Ollama.Ollama', 'version': '2.0', 'target_pid': 9876,
                        'restart_required': False, 'install_location': 'D:/Ollama',
                        'processes': [{'pid': 333, 'path': 'D:/Ollama/ollama app.exe', 'created_at': 10,
                                       'sha256': 'c' * 64}],
                        'winget_log_sha256': 'a' * 64, 'installer_log_sha256': 'b' * 64})
                native.channels['receipt'].put((json.dumps(first) + '\n' + json.dumps(handoff) + '\n').encode('ascii'))
                threading.Timer(.5, native.complete).start()
            def complete(native):
                native.exit = 0
                native.channels['receipt'].put((json.dumps(native.rows()[1]) + '\n').encode('ascii'))
                native.channels['receipt'].put(b'')
        started = time.monotonic()
        with mock.patch.object(console, '_ConsoleProcess', ResidentConsole):
            result = console.run_winget_console(arguments, timeout=2, progress_callback=lambda _: None,
                allow_resident_handoff=True)
        self.assertLess(time.monotonic() - started, .4)
        self.assertFalse(result.capture_complete)
        self.assertTrue(result.native_completion_verified)
        self.assertFalse(result.console_receipt['quiescent'])
        native = ResidentConsole.instances[-1]
        self.assertFalse(native.closed)
        self.assertEqual(native.close_calls, [])
        with console._RETAINED_LOCK:
            owner = next(session for session in console._RETAINED if session.native is native)
        self.assertTrue(owner.finished.wait(3))
        self.assertTrue(native.closed)

    def test_new_handoff_payload_is_explicit_and_default_payload_remains_unchanged(self):
        import base64
        values = ['C:/fixtures/fake.exe', '--silent']
        default = console._helper_arguments(432, 'f' * 32, values)
        enabled = console._helper_arguments(432, 'f' * 32, values, allow_resident_handoff=True)
        self.assertEqual(json.loads(base64.b64decode(default[-1])), values)
        self.assertEqual(json.loads(base64.b64decode(enabled[-1])),
            {'schema': 1, 'arguments': values, 'resident_handoff': True})

    def test_timeout_retains_console_drainer_and_handles_until_quiescence(self):
        FakeConsole.automatic = False
        with self.assertRaises(subprocess.TimeoutExpired):
            console.run_winget_console(['C:/fixtures/fake.exe'], timeout=0.08, progress_callback=lambda _: None)
        native = FakeConsole.instances[-1]
        self.assertTrue(native.started)
        self.assertFalse(native.closed)
        self.assertEqual(native.close_calls, [])
        with console._RETAINED_LOCK:
            session = next(value for value in console._RETAINED if value.native is native)
        self.assertFalse(session.reporting.is_set())
        native.complete()
        self.assertTrue(session.finished.wait(2))
        self.assertTrue(native.closed)
        self.assertEqual(len(native.close_calls), 1)

    def test_wrong_nonce_never_authorizes_console_close(self):
        FakeConsole.terminal_nonce = 'a' * 32
        with self.assertRaises(console.ConsoleOutcomeError):
            console.run_winget_console(['C:/fixtures/fake.exe'], timeout=2, progress_callback=lambda _: None)
        self.assertEqual(FakeConsole.instances[-1].close_calls, [])
        self.assertFalse(FakeConsole.instances[-1].closed)

    def test_prelaunch_failure_is_explicitly_known_no_target(self):
        FakeConsole.launch_error = PermissionError('fixture launch refused')
        with self.assertRaises(console.ConsolePreflightError) as caught:
            console.run_winget_console(['C:/fixtures/fake.exe'], timeout=1, progress_callback=lambda _: None)
        self.assertIs(caught.exception.installer_started, False)
        self.assertIs(caught.exception.outcomeKnown, True)
        self.assertIs(caught.exception.running, False)
        self.assertEqual(FakeConsole.instances[-1].dispose_calls, 1)

    def test_post_launch_failure_retains_unknown_state_and_never_relaunches(self):
        FakeConsole.post_launch_error = RuntimeError('fixture after CreateProcess')
        with self.assertRaises(console.ConsoleOutcomeError) as caught:
            console.run_winget_console(['C:/fixtures/fake.exe'], timeout=1, progress_callback=lambda _: None)
        self.assertIs(caught.exception.installer_started, True)
        self.assertIs(caught.exception.outcomeKnown, False)
        self.assertIs(caught.exception.running, True)
        self.assertEqual(len(FakeConsole.instances), 1)
        native = FakeConsole.instances[0]
        self.assertEqual(native.close_calls, [])
        self.assertFalse(native.closed)

    def test_capture_bound_is_independent_of_native_terminal_success(self):
        FakeConsole.flood = True
        result = console.run_winget_console(['C:/fixtures/fake.exe'], timeout=2, progress_callback=lambda _: None)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.capture_complete)
        self.assertLess(len(result.stdout), 2 * 1024 ** 2 + 200)
        self.assertIn('middle text omitted', result.stdout)

    def test_slow_or_throwing_callback_does_not_block_output_drain(self):
        callback_started = threading.Event()
        callback_release = threading.Event()
        def slow(_):
            callback_started.set()
            callback_release.wait(2)
            raise RuntimeError('Display fixture')
        try:
            started = time.monotonic()
            result = console.run_winget_console(['C:/fixtures/fake.exe'], timeout=1, progress_callback=slow)
            self.assertTrue(callback_started.is_set())
            self.assertLess(time.monotonic() - started, 0.9)
            self.assertTrue(result.capture_complete)
        finally:
            callback_release.set()

    def test_private_app_entry_bypasses_parser_and_backend(self):
        from optimus import app
        with mock.patch.object(console, 'monitor_main', return_value=37) as monitor, \
                mock.patch.object(app, 'parser', side_effect=AssertionError('Normal startup was invoked')):
            self.assertEqual(app.main([console._FLAG, 'handle', 'nonce', 'payload']), 37)
        monitor.assert_called_once_with(['handle', 'nonce', 'payload'])

    def test_probe_private_entries_bypass_normal_startup(self):
        from optimus import app
        with mock.patch.object(console, 'console_progress_probe', return_value=0) as probe, \
                mock.patch.object(console, 'probe_child_main', return_value=0) as child, \
                mock.patch.object(app, 'parser', side_effect=AssertionError('Normal startup was invoked')):
            self.assertEqual(app.main(['--console-progress-probe', 'D:/fixture-output']), 0)
            self.assertEqual(app.main(['--console-progress-fixture-child']), 0)
            self.assertEqual(app.main(['--console-progress-probe']), 2)
            self.assertEqual(app.main(['--console-progress-fixture-child', 'extra']), 2)
        probe.assert_called_once_with('D:/fixture-output')
        child.assert_called_once_with()

    def test_final_private_receipt_rejects_contradictory_or_coerced_fields(self):
        native = FakeConsole()
        arguments = ['C:/fixtures/fake.exe']
        session = console._Session(native, arguments, lambda _: None)
        native.nonce, native.arguments = session.nonce, arguments
        native.started = True
        session.receipt_eof.set()
        for field, value in (('schema', True), ('console_client_count', True), ('quiescent', 1),
                             ('job_active_processes', True), ('job_active_processes', 1), ('job_verified', 1),
                             ('exit_code', True), ('supervisor_pid', True), ('target_pid', False),
                             ('argv_sha256', 'a' * 64), ('nonce', 'a' * 32)):
            with self.subTest(field=field, value=value):
                session.receipts = native.rows()
                session.receipts[1][field] = value
                self.assertIsNone(session._terminal())

    def test_frozen_helper_arguments_use_only_private_entry(self):
        with tempfile.TemporaryDirectory(prefix='optimus-helper-fixture-') as directory:
            executable = str(Path(directory) / 'Optimus.exe')
            helper = Path(directory) / 'OptimusConsole.exe'
            helper.write_bytes(b'not executed fixture')
            with mock.patch.object(sys, 'frozen', True, create=True), mock.patch.object(sys, 'executable', executable):
                args = console._helper_arguments(432, 'f' * 32, ['C:/fixtures/fake.exe', '--silent'])
                probe = console._probe_arguments()
            self.assertEqual(args[:4], [str(helper), console._FLAG, '432', 'f' * 32])
            self.assertEqual(probe, [str(helper), '--console-progress-fixture-child'])
            self.assertNotIn('--source-workspace', args)
            self.assertNotIn('--admin-relaunch', args)

    def test_missing_frozen_helper_is_known_no_launch_before_console_preparation(self):
        with tempfile.TemporaryDirectory(prefix='optimus-missing-helper-fixture-') as directory:
            executable = str(Path(directory) / 'Optimus.exe')
            with mock.patch.object(sys, 'frozen', True, create=True), mock.patch.object(sys, 'executable', executable):
                with self.assertRaises(console.ConsolePreflightError) as caught:
                    console.run_winget_console(['C:/fixtures/fake.exe'], timeout=1, progress_callback=lambda _: None)
            self.assertIs(caught.exception.installer_started, False)
            self.assertIs(caught.exception.outcomeKnown, True)
            self.assertIs(caught.exception.running, False)
            self.assertEqual(FakeConsole.instances, [])

    def test_missing_source_console_interpreter_is_known_no_launch_before_preparation(self):
        with mock.patch.object(console, '_source_console_executable', side_effect=FileNotFoundError('fixture')):
            with self.assertRaises(console.ConsolePreflightError) as caught:
                console.run_winget_console(['C:/fixtures/fake.exe'], timeout=1, progress_callback=lambda _: None)
        self.assertIs(caught.exception.installer_started, False)
        self.assertIs(caught.exception.outcomeKnown, True)
        self.assertIs(caught.exception.running, False)
        self.assertEqual(FakeConsole.instances, [])

    def test_invalid_input_does_not_prepare_or_launch_console(self):
        for arguments, timeout in ((['relative.exe'], 1), (['C:/fake.exe'], float('nan')), (['C:/fake.exe'], True)):
            with self.subTest(arguments=arguments, timeout=timeout), self.assertRaises(ValueError):
                console.run_winget_console(arguments, timeout=timeout, progress_callback=lambda _: None)
        self.assertEqual(FakeConsole.instances, [])


class SourceConsoleSelectionTests(unittest.TestCase):
    def test_windowed_trusted_base_uses_only_same_directory_console_interpreter(self):
        with tempfile.TemporaryDirectory(prefix='optimus-source-interpreter-') as directory:
            windowed = Path(directory) / 'PYTHONW.EXE'
            executable = Path(directory) / 'python.exe'
            windowed.write_bytes(b'not executed windowed fixture')
            executable.write_bytes(b'not executed console fixture')
            with mock.patch.object(sys, '_base_executable', str(windowed), create=True), \
                    mock.patch.object(sys, 'executable', 'C:/unrelated/venv/pythonw.exe'):
                self.assertEqual(console._source_console_executable(), str(executable))
                self.assertEqual(console._helper_arguments(432, 'f' * 32, ['C:/fixtures/fake.exe'])[0], str(executable))
                self.assertEqual(console._probe_arguments()[0], str(executable))

    def test_missing_console_sibling_never_falls_back_to_gui_or_other_interpreter(self):
        with tempfile.TemporaryDirectory(prefix='optimus-source-no-console-') as directory:
            windowed = Path(directory) / 'pythonw.exe'
            windowed.write_bytes(b'not executed windowed fixture')
            with mock.patch.object(sys, '_base_executable', str(windowed), create=True):
                with self.assertRaises(FileNotFoundError):
                    console._source_console_executable()

    def test_reparse_console_interpreter_is_refused(self):
        with tempfile.TemporaryDirectory(prefix='optimus-source-indirect-') as directory:
            executable = Path(directory) / 'python.exe'
            executable.write_bytes(b'not executed console fixture')
            original = Path.lstat
            def indirect(path):
                if path == executable:
                    return type('IndirectFixture', (), {'st_file_attributes': 0x400,
                        'st_mode': original(path).st_mode})()
                return original(path)
            with mock.patch.object(sys, '_base_executable', str(executable), create=True), \
                    mock.patch.object(Path, 'lstat', indirect):
                with self.assertRaises(FileNotFoundError):
                    console._source_console_executable()


@unittest.skipUnless(sys.platform == 'win32', 'Harmless ConPTY fixture children require Windows.')
class NativeConsoleFixtureTests(unittest.TestCase):
    executable = getattr(sys, '_base_executable', None) or sys.executable

    def run_child(self, source, *, timeout=5, callback=lambda _: None, extra=()):
        return console.run_winget_console([self.executable, '-I', '-u', '-c', source, *extra],
            timeout=timeout, progress_callback=callback)

    def test_native_console_reports_uninstall_percent_before_harmless_fixture_exits(self):
        callbacks = []
        source = ("import time;print('Starting package uninstall...',flush=True);"
                  "print('37%',flush=True);time.sleep(.3);"
                  "print('100%',flush=True);time.sleep(.2)")
        result = self.run_child(source, callback=lambda value: callbacks.append((time.monotonic(), value)))
        finished = time.monotonic()
        self.assertEqual(result.returncode, 0)
        self.assertIs(result.capture_complete, True)
        self.assertIs(result.console_receipt['verified'], True)
        self.assertTrue(any(value['phase'] == 'uninstall' and value['percent'] == 37
                            and when < finished - .1 for when, value in callbacks))
        self.assertFalse(any(value['percent'] == 100 for _, value in callbacks))

    def test_native_packaged_probe_protocol_uses_only_local_declared_fixture(self):
        with tempfile.TemporaryDirectory(prefix='optimus-console-probe-') as directory:
            self.assertEqual(console.console_progress_probe(directory), 0)
            path = Path(directory) / 'console-progress-probe.json'
            report = json.loads(path.read_text(encoding='utf8'))
            self.assertEqual(report['status'], 'passed')
            self.assertIs(report['maintenance_applied'], False)
            self.assertEqual(report['network_requests'], 0)
            self.assertEqual(report['real_winget_invocations'], 0)
            self.assertIs(report['private_receipt_verified'], True)
            self.assertIs(report['download_progress_before_exit'], True)
            self.assertIs(report['install_progress_before_exit'], True)
            original = path.read_bytes()
            with mock.patch.object(console, 'run_winget_console', side_effect=AssertionError('Existing proof was overwritten')):
                self.assertEqual(console.console_progress_probe(directory), 2)
            self.assertEqual(path.read_bytes(), original)

    def test_native_source_pythonw_frontend_reports_live_download_and_install_progress(self):
        windowed = Path(self.executable).with_name('pythonw.exe')
        if not windowed.is_file():
            self.skipTest('The harmless windowed Python frontend fixture is unavailable.')
        with tempfile.TemporaryDirectory(prefix='optimus-pythonw-progress-fixture-') as directory:
            metadata = Path(directory) / 'frontend.json'
            frontend = ('import json,sys;from pathlib import Path;sys.path.insert(0,'
                + repr(str(Path(console.__file__).resolve().parents[1])) + ');'
                + 'from optimus.winget_console import console_progress_probe,_helper_arguments,_probe_arguments;'
                + 'Path(' + repr(str(metadata)) + ').write_text(json.dumps('
                + "{'base':sys._base_executable,'stdout_none':sys.stdout is None,"
                + "'helper':_helper_arguments(432,'f'*32,['C:/fixtures/fake.exe'])[0],"
                + "'probe':_probe_arguments()[0]}),encoding='utf8');"
                + 'raise SystemExit(console_progress_probe(' + repr(directory) + '))')
            child = subprocess.Popen([str(windowed), '-I', '-c', frontend],
                close_fds=True, creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0x8))
            self.assertEqual(child.wait(timeout=12), 0)
            launch = json.loads(metadata.read_text(encoding='utf8'))
            self.assertEqual(Path(launch['base']).name.casefold(), 'pythonw.exe')
            self.assertIs(launch['stdout_none'], True)
            self.assertEqual(launch['helper'], str(windowed.with_name('python.exe')))
            self.assertEqual(launch['probe'], launch['helper'])
            report = json.loads((Path(directory) / 'console-progress-probe.json').read_text(encoding='utf8'))
            self.assertEqual(report['status'], 'passed')
            self.assertIs(report['frozen'], False)
            self.assertIs(report['console_query_verified'], True)
            self.assertIs(report['download_progress_before_exit'], True)
            self.assertIs(report['install_progress_before_exit'], True)
            self.assertIs(report['private_receipt_verified'], True)
            self.assertEqual(report['target_exit_code'], 0)
            self.assertIs(report['capture_complete'], True)
            self.assertEqual(report['network_requests'], 0)
            self.assertEqual(report['real_winget_invocations'], 0)
            self.assertIs(report['maintenance_applied'], False)

    def test_native_console_queries_and_live_download_install_progress(self):
        callbacks = []
        source = """import ctypes,time
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32');k.GetStdHandle.restype=w.HANDLE
k.GetConsoleScreenBufferInfo.argtypes=[w.HANDLE,ctypes.c_void_p]
print('console='+str(bool(k.GetConsoleScreenBufferInfo(k.GetStdHandle(-11),ctypes.create_string_buffer(64)))),flush=True)
print('Downloading https://example.test/fake.exe',flush=True)
print('512 KB / 1 MB',flush=True)
time.sleep(.25)
print('Starting package install...',flush=True)
print('25%',flush=True)
time.sleep(.25)
"""
        result = self.run_child(source, callback=lambda value: callbacks.append((time.monotonic(), value)))
        finished = time.monotonic()
        self.assertIn('console=True', result.stdout)
        self.assertTrue(result.capture_complete)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(any(row.get('phase') == 'download' and row.get('percent') == 50 for _, row in callbacks))
        self.assertTrue(any(row.get('phase') == 'install' and row.get('percent') == 25 for _, row in callbacks))
        self.assertTrue(any(timestamp < finished - 0.1 for timestamp, _ in callbacks))
        self.assertEqual(result.download_progress['download_total_bytes'], 1048576)

    def test_native_vendor_console_client_outlives_target_wrapper(self):
        source = """import subprocess,sys
subprocess.Popen([sys.executable,'-I','-u','-c',\"import time;time.sleep(.6);print('vendor-client-finished',flush=True)\"])
print('wrapper-finished',flush=True)
"""
        started = time.monotonic()
        result = self.run_child(source)
        self.assertGreater(time.monotonic() - started, 0.5)
        self.assertIn('wrapper-finished', result.stdout)
        self.assertIn('vendor-client-finished', result.stdout)
        self.assertTrue(result.capture_complete)
        self.assertEqual(result.returncode, 0)

    def test_native_timeout_never_closes_running_fixture_client(self):
        source = "import time;print('fixture-started',flush=True);time.sleep(.7);print('fixture-finished',flush=True)"
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_child(source, timeout=0.25)
        with console._RETAINED_LOCK:
            sessions = [value for value in console._RETAINED if value.arguments[-1:] == [source]]
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertFalse(session.native.closed)
        self.assertFalse(session.reporting.is_set())
        self.assertTrue(session.finished.wait(4))
        self.assertIn('fixture-finished', session.output.value())
        self.assertTrue(session.native.closed)

    def test_native_timed_out_fixture_survives_frontend_normal_exit(self):
        with tempfile.TemporaryDirectory(prefix='optimus-console-fixture-') as directory:
            marker = Path(directory) / 'vendor-finished.txt'
            child = ("import time;from pathlib import Path;time.sleep(.65);"
                     + 'Path(' + repr(str(marker)) + ").write_text('finished',encoding='utf8')")
            frontend = ("import sys,subprocess;sys.path.insert(0," + repr(str(Path(console.__file__).resolve().parents[1])) + ");"
                + "from optimus.winget_console import run_winget_console\n"
                + "try:run_winget_console(" + repr([self.executable, '-I', '-u', '-c', child])
                + ",timeout=.2,progress_callback=lambda _:None)\n"
                + "except subprocess.TimeoutExpired:print('frontend-returned',flush=True)\n")
            started = time.monotonic()
            completed = subprocess.run([self.executable, '-I', '-u', '-c', frontend],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=8, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn('frontend-returned', completed.stdout)
            self.assertGreater(time.monotonic() - started, .6)
            self.assertEqual(marker.read_text(encoding='utf8'), 'finished')

    def test_native_gui_descendant_is_tracked_before_any_console_attachment(self):
        windowed = Path(self.executable).with_name('pythonw.exe')
        if not windowed.is_file():
            self.skipTest('The harmless windowed Python fixture is unavailable.')
        with tempfile.TemporaryDirectory(prefix='optimus-gui-descendant-fixture-') as directory:
            marker = Path(directory) / 'gui-client-finished.txt'
            descendant = ("import time;from pathlib import Path;time.sleep(.6);"
                          + 'Path(' + repr(str(marker)) + ").write_text('finished',encoding='utf8')")
            source = ("import subprocess;subprocess.Popen(" + repr([str(windowed), '-I', '-c', descendant])
                      + ");print('wrapper-finished',flush=True)")
            started = time.monotonic()
            # Explicit handoff mode must still refuse an unknown GUI child.
            # This also exercises the real native private-job PID inventory.
            result = console.run_winget_console([self.executable, '-I', '-u', '-c', source],
                timeout=5, progress_callback=lambda _: None, allow_resident_handoff=True)
            self.assertGreater(time.monotonic() - started, .5)
            self.assertEqual(marker.read_text(encoding='utf8'), 'finished')
            self.assertTrue(result.console_receipt['job_verified'])
            self.assertEqual(result.console_receipt['job_active_processes'], 0)

    def test_native_authenticated_handoff_keeps_harmless_gui_runtime_alive(self):
        """Native transport only; classifier evidence has separate offline tests.

        The fixture substitutes its evidence provider inside its private child.
        It never invokes WinGet, writes ARP keys or installs actual software.
        """
        import shutil
        windowed = Path(self.executable).with_name('pythonw.exe')
        if not windowed.is_file():
            self.skipTest('The harmless windowed Python fixture is unavailable.')
        with tempfile.TemporaryDirectory(prefix='optimus-resident-handoff-fixture-') as directory:
            folder = Path(directory)
            wrapper, runtime = folder / 'winget.exe', folder / 'ollama app.exe'
            shutil.copy2(self.executable, wrapper)
            shutil.copy2(windowed, runtime)
            for name in ('python312.dll', 'python3.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
                original = Path(self.executable).with_name(name)
                if original.is_file():
                    shutil.copy2(original, folder / name)
            marker = folder / 'runtime-finished.txt'
            runtime_code = ("import time;from pathlib import Path;time.sleep(1.4);Path("
                            + repr(str(marker)) + ").write_text('finished')")
            (folder / 'upgrade').write_text('import subprocess\nsubprocess.Popen('
                + repr([str(runtime), '-c', runtime_code]) + ")\nprint('fixture-provider-finished',flush=True)\n")
            original_arguments = console._helper_arguments
            def helper(handle, nonce, values, **options):
                result = original_arguments(handle, nonce, values, **options)
                # Only this test child substitutes native provider evidence.
                injection = ("import hashlib,time;from pathlib import Path;"
                    "import optimus.resident_update_completion as r;"
                    "r.verify_resident_completion=lambda argv,pid,started,rows:dict("
                    "schema=1,kind='verified-ollama-resident-runtime-v1',package_id='Ollama.Ollama',"
                    "version='2.0',target_pid=pid,restart_required=False,install_location=" + repr(directory)
                    + ",processes=[dict(row,sha256=hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()) for row in rows],"
                    "winget_log_sha256='a'*64,installer_log_sha256='b'*64);"
                    )
                result[result.index('-c') + 1] = result[result.index('-c') + 1].replace(
                    'from optimus.winget_console import monitor_main;', injection + 'from optimus.winget_console import monitor_main;')
                return result
            previous_directory = os.getcwd()
            try:
                os.chdir(directory)
                arguments = [str(wrapper), 'upgrade', '--id', 'Ollama.Ollama', '--exact', '--source',
                             'winget', '--version', '2.0', '--silent', '--disable-interactivity']
                with mock.patch.dict(os.environ, {'PYTHONHOME': str(Path(self.executable).parent)}), \
                        mock.patch.object(console, '_helper_arguments', side_effect=helper):
                    result = console.run_winget_console(arguments, timeout=5, progress_callback=lambda _: None,
                        allow_resident_handoff=True)
                self.assertIs(result.native_completion_verified, True)
                self.assertIs(result.capture_complete, False)
                self.assertFalse(marker.exists(), 'The resident GUI fixture exited before handoff.')
                from optimus.resident_update_completion import verified_resident_handoff
                self.assertTrue(verified_resident_handoff(result, arguments))
                with console._RETAINED_LOCK:
                    owner = next(value for value in console._RETAINED if value.arguments == arguments)
                self.assertFalse(owner.native.closed)
                self.assertTrue(owner.finished.wait(8))
                self.assertEqual(marker.read_text(), 'finished')
                self.assertTrue(owner.native.closed)
            finally:
                os.chdir(previous_directory)

    def test_native_preserves_literal_unicode_and_shell_metacharacter_arguments(self):
        values = ('text with spaces', 'a"b', 'c\\d\\', 'é ☃', '$value;$(never)', '`literal`')
        result = self.run_child("import json,sys;print('ARGV:'+json.dumps(sys.argv[1:],ensure_ascii=True),flush=True)", extra=values)
        stripped = __import__('optimus.winget_progress', fromlist=['_ANSI'])._ANSI.sub('', result.stdout)
        data = next(line.split('ARGV:', 1)[1] for line in stripped.splitlines() if 'ARGV:' in line)
        self.assertEqual(json.loads(data), list(values))
        self.assertTrue(result.capture_complete)

    def test_native_large_render_stream_still_drains_without_callback_backpressure(self):
        result = self.run_child("import sys;sys.stdout.write('fixture-output'*180000);sys.stdout.flush();print('final-fixture-message',flush=True)", timeout=8)
        # ConPTY renders the screen; it may coalesce overwritten frames. The
        # deterministic received-byte bound is exercised by the fake above.
        self.assertEqual(result.returncode, 0)
        self.assertLess(len(result.stdout), 2 * 1024 ** 2 + 200)
        self.assertIn('final-fixture-message', result.stdout)


if __name__ == '__main__':
    unittest.main()
