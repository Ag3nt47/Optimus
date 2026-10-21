"""App-open automation policy/state fixtures; no maintenance operations run."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from optimus import automation
from optimus.workflow import DEFAULTS, Preferences


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='optimus-automation-test-')
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def config(self, mode='daily'):
        result = copy.deepcopy(DEFAULTS)
        result.update({'setup_version': automation.CURRENT_SETUP_VERSION, 'automation_mode': mode, 'cleanup_enabled': False, 'browser_cache_enabled': False})
        return result

    def test_old_settings_and_manual_mode_never_automatically_run(self):
        self.assertFalse(automation.due(DEFAULTS, None, now=100))
        self.assertFalse(automation.due(self.config('manual'), None, now=100))
        self.assertTrue(automation.due(self.config(), None, now=100))

    def test_daily_and_weekly_elapsed_since_attempt_not_completion(self):
        config = self.config()
        record = automation.record_attempt(self.root, config, now=100)
        final = automation.record_attempt(self.root, config, status='failed', now=200, error='Fixture', run_id=record['run_id'])
        self.assertEqual(final['attempted_at'], 100)
        self.assertEqual(final['session_id'], record['session_id'])
        self.assertFalse(automation.due(config, final, now=86499))
        self.assertTrue(automation.due(config, final, now=86500))
        self.assertFalse(automation.due(self.config('weekly'), final, now=604899))
        self.assertTrue(automation.due(self.config('weekly'), final, now=604900))

    def test_startup_only_once_in_same_session_even_after_failure(self):
        config = self.config('startup')
        record = automation.record_attempt(self.root, config, now=100)
        self.assertFalse(automation.due(config, record, now=999999))
        with patch.object(automation, 'SESSION_ID', 'a' * 32):
            self.assertTrue(automation.due(config, record, now=100))

    def test_configured_baseline_runs_nothing_and_defers_run_now_false(self):
        saved = automation.record_attempt(self.root, self.config('startup'), status='configured', now=100)
        self.assertIsNone(saved['run_id'])
        self.assertIn('no maintenance operation', saved['note'])
        self.assertFalse(automation.due(self.config('startup'), saved, now=100))
        self.assertFalse(automation.due(self.config(), saved, now=86499))
        with self.assertRaises(ValueError):
            automation.record_attempt(self.root, self.config(), status='completed', now=200)

    def test_corrupt_and_semantically_invalid_state_never_authorizes_work(self):
        path = self.root / 'automation-state.json'
        valid = automation.record_attempt(self.root, self.config(), now=100)
        invalid = [dict(valid, status='completed', run_id=None), dict(valid, attempted_at=None),
                   dict(valid, attempted_at=float('nan')), dict(valid, schema=True),
                   dict(valid, status='completed', finished_at=None), dict(valid, status='failed', finished_at=99),
                   dict(valid, status='configured', run_id='a' * 32)]
        for record in invalid:
            with self.subTest(record=record):
                path.write_text(json.dumps(record), encoding='utf-8')
                loaded = automation.load_attempt(self.root)
                self.assertEqual(loaded['status'], 'invalid')
                self.assertFalse(automation.due(self.config(), loaded, now=999999))
        path.write_text('{broken', encoding='utf-8')
        self.assertFalse(automation.due(self.config(), automation.load_attempt(self.root), now=999999))

    def test_terminal_update_refuses_another_run_identity(self):
        before = automation.record_attempt(self.root, self.config(), now=100)
        with self.assertRaises(ValueError):
            automation.record_attempt(self.root, self.config(), status='completed', run_id='f' * 32, now=200)
        self.assertEqual(automation.load_attempt(self.root), before)

    def test_future_clock_and_invalid_settings_fail_closed(self):
        value = automation.record_attempt(self.root, self.config(), now=5000)
        self.assertFalse(automation.due(self.config(), value, now=100))
        self.assertFalse(automation.due({'automation_mode': 'hourly'}, None, now=100))
        self.assertFalse(automation.due(self.config(), value, now=float('inf')))

    def test_configured_write_refuses_active_maintenance_lease(self):
        with automation.RunLease(self.root):
            with self.assertRaisesRegex(RuntimeError, 'Another Optimus'):
                automation.record_attempt(self.root, self.config(), status='configured')
        self.assertIsNone(automation.load_attempt(self.root))

    def test_setup_transaction_saves_baseline_then_preferences_without_running(self):
        calls = []
        def save(value):
            calls.append(value)
            self.assertEqual(automation.load_attempt(self.root)['status'], 'configured')
            return value
        result = automation.save_setup(self.root, self.config(), save, run_now=False)
        self.assertEqual(result, self.config())
        self.assertEqual(len(calls), 1)
        self.assertFalse(automation.due(result, automation.load_attempt(self.root)))

    def test_setup_save_failure_restores_prior_bytes_exactly(self):
        path = self.root / 'automation-state.json'
        prior = b'{"unverified": true, "spacing": "keep exactly"}\n'
        path.write_bytes(prior)
        def fail(value): raise OSError('Synthetic preference write failure')
        with self.assertRaisesRegex(OSError, 'Synthetic preference'):
            automation.save_setup(self.root, self.config(), fail)
        self.assertEqual(path.read_bytes(), prior)
        with automation.RunLease(self.root): pass

    def test_setup_save_failure_restores_prior_absence(self):
        def fail(value): raise OSError('Fixture')
        with self.assertRaises(OSError): automation.save_setup(self.root, self.config(), fail)
        self.assertFalse((self.root / 'automation-state.json').exists())

    def test_setup_refuses_other_active_process_before_callback_or_state_write(self):
        calls = []
        with automation.RunLease(self.root):
            with self.assertRaises(RuntimeError):
                automation.save_setup(self.root, self.config(), lambda value: calls.append(value))
        self.assertEqual(calls, [])
        self.assertFalse((self.root / 'automation-state.json').exists())

    def test_os_lock_excludes_second_python_process_and_releases_afterward(self):
        code = "from optimus.automation import RunLease; import sys; RunLease(sys.argv[1]).acquire()"
        environment = os.environ.copy()
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        environment['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
        with automation.RunLease(self.root):
            blocked = subprocess.run([sys.executable, '-B', '-c', code, str(self.root)], env=environment, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('Another Optimus process', blocked.stderr)
        allowed = subprocess.run([sys.executable, '-B', '-c', code, str(self.root)], env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_cadence_notes_disclose_app_must_remain_open(self):
        for mode in ('startup', 'daily', 'weekly'):
            self.assertIn('while Optimus is open', automation.cadence_note(mode))


if __name__ == '__main__': unittest.main()
