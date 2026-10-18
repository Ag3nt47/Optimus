"""Preference/workflow tests use only mock services and temporary fixture logs."""
import copy
import importlib.util
import sys
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

module_path = Path(__file__).parents[1] / 'src' / 'optimus' / 'workflow.py'
sys.path.insert(0, str(module_path.parents[1]))
spec = importlib.util.spec_from_file_location('optimus._workflow_mock_test', module_path)
workflow_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow_module)


class MockBackend:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.bridge = types.SimpleNamespace(_process=None)
        self.calls = []
        self.audit_callback = None
        self.scan_callback = None
        self.fail_cleanup_once = False
    def audit(self):
        self.calls.append(('audit',))
        if self.audit_callback: self.audit_callback()
        return {'fixture': True}
    def cleanup_categories(self):
        return [{'id': 'mock-browser-cache', 'process_names': ['FixtureBrowser.exe'], 'available': True}]
    def cleanup_scan(self, category_ids, age):
        self.calls.append(('cleanup_scan', list(category_ids), age))
        if self.scan_callback: self.scan_callback()
        return {'files': [{'path': 'disposable-mock'}], 'total_bytes': 100, 'complete': True, 'skipped': []}
    def cleanup_apply(self, plan):
        self.calls.append(('cleanup_apply',))
        if self.fail_cleanup_once:
            self.fail_cleanup_once = False
            raise RuntimeError('Synthetic cleanup refusal')
        return {'deleted_count': 1, 'deleted_bytes': 100, 'skipped_count': 0}
    def debloat_preview(self, action):
        self.calls.append(('debloat_preview', action))
        return {'planId': 'mock-plan', 'targets': [{'id': 'mock-target'}]}
    def debloat_apply(self, plan):
        self.calls.append(('debloat_apply', plan))
        return {'fixture': True}
    def codex_profile(self, priority):
        self.calls.append(('codex_profile', priority))
        return {'fixture': True}
    def analysis_start(self, interval):
        self.calls.append(('analysis_start', interval))
        return {'enabled': True}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='optimus-workflow-test-')
        self.backend = MockBackend(self.temp.name)
        self.workflow = workflow_module.Workflow(self.backend)
        self.addCleanup(self.temp.cleanup)

    def config(self, **values):
        result = copy.deepcopy(workflow_module.DEFAULTS)
        result.update(values)
        return result

    def test_settings_validation_and_independent_defaults(self):
        result = workflow_module.Preferences.validate({'unknown': 'ignored'})
        self.assertNotIn('unknown', result)
        result['cleanup_enabled'] = False
        self.assertTrue(workflow_module.DEFAULTS['cleanup_enabled'])
        for invalid in ({'cleanup_enabled': 'yes'}, {'cleanup_age_days': True}, {'cleanup_age_days': 0}, {'analysis_interval': 1}, {'codex_priority': 'RealTime'}, {'intensity': 'Extreme'}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError): workflow_module.Preferences.validate(invalid)

    def test_preferences_round_trip_and_corrupt_file_fallback(self):
        preferences = workflow_module.Preferences(self.temp.name)
        saved = preferences.save(self.config(cleanup_enabled=False, analysis_interval=8))
        self.assertEqual(preferences.load(), saved)
        preferences.path.write_text('{broken', encoding='utf-8')
        self.assertEqual(preferences.load(), workflow_module.DEFAULTS)
        self.assertEqual(preferences.path.read_text(), '{broken')

    def test_progress_corresponds_to_completed_stages_and_log(self):
        events = []
        result = self.workflow.run(self.config(), events.append)
        self.assertEqual(len(result['stages']), 4)
        self.assertEqual(result['failed_stages'], 0)
        self.assertEqual(events[0]['percentage'], 0)
        self.assertEqual(events[-1]['percentage'], 100)
        percentages = [event['percentage'] for event in events]
        self.assertEqual(percentages, sorted(percentages))
        self.assertTrue(all(event['completed'] <= event['total'] for event in events))
        persisted = json.loads(Path(result['log_path']).read_text())
        self.assertEqual(persisted['stages'], result['stages'])
        self.assertFalse(self.workflow.active)

    def test_low_intensity_preserves_longer_cleanup_age(self):
        self.workflow.run(self.config(intensity='Low', cleanup_age_days=7), lambda event: None)
        ages = [item[2] for item in self.backend.calls if item[0] == 'cleanup_scan']
        self.assertEqual(ages, [30, 30])

    def test_preexisting_cancellation_does_not_apply_any_stage(self):
        cancelled = threading.Event(); cancelled.set()
        events = []
        result = self.workflow.run(self.config(), events.append, cancelled)
        self.assertLess(max((event['percentage'] for event in events), default=0), 100)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['stages'], [])
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(self.workflow.active)

    def test_cancellation_between_stages_stops_further_operations(self):
        cancelled = threading.Event()
        self.backend.audit_callback = cancelled.set
        events = []
        result = self.workflow.run(self.config(), events.append, cancelled)
        self.assertLess(max((event['percentage'] for event in events), default=0), 100)
        self.assertTrue(result['cancelled'])
        self.assertEqual(len(result['stages']), 1)
        self.assertEqual(self.backend.calls, [('audit',)])

    def test_cancellation_during_preview_does_not_start_cleanup_apply(self):
        cancelled = threading.Event()
        self.backend.scan_callback = cancelled.set
        events = []
        result = self.workflow.run(self.config(), events.append, cancelled)
        self.assertLess(max((event['percentage'] for event in events), default=0), 100)
        self.assertTrue(result['cancelled'])
        self.assertFalse(any(item[0] == 'cleanup_apply' for item in self.backend.calls))

    def test_stage_failure_is_logged_and_final_report_still_runs(self):
        self.backend.fail_cleanup_once = True
        events = []
        result = self.workflow.run(self.config(), events.append)
        self.assertEqual(result['failed_stages'], 1)
        self.assertIn('Synthetic cleanup refusal', result['stages'][1]['error'])
        self.assertEqual(sum(item[0] == 'audit' for item in self.backend.calls), 2)
        self.assertEqual(events[-1]['percentage'], 100)
        self.assertTrue(any('FAILED:' in event['log'] for event in events))

    def test_invalid_config_does_not_leave_workflow_locked(self):
        with self.assertRaises(ValueError): self.workflow.run({'cleanup_age_days': 0}, lambda event: None)
        self.assertFalse(self.workflow.active)
        result = self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False), lambda event: None)
        self.assertEqual(len(result['stages']), 2)

    def test_progress_callback_failure_releases_run_lock(self):
        def fail(event): raise RuntimeError('Synthetic UI failure')
        with self.assertRaisesRegex(RuntimeError, 'Synthetic UI failure'):
            self.workflow.run(self.config(), fail)
        self.assertFalse(self.workflow.active)
        result = self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False), lambda event: None)
        self.assertEqual(result['failed_stages'], 0)

    def test_initial_journal_failure_releases_lock_and_reports_failed(self):
        failure = OSError(17, 'Synthetic cross-volume journal failure')
        with patch.object(workflow_module, 'atomic_json', side_effect=failure):
            with self.assertRaisesRegex(OSError, 'Synthetic cross-volume'):
                self.workflow.run(self.config(), lambda event: None)
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(self.workflow.active)
        self.assertFalse(self.workflow.lock.locked())
        self.assertEqual(self.workflow.last_report['status'], 'failed')
        self.assertIn('Synthetic cross-volume', self.workflow.last_report['journal_error'])
        result = self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False), lambda event: None)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['stages']), 2)

    def test_preparation_failure_after_lock_acquire_releases_lock(self):
        previous = self.backend.data_dir
        self.backend.data_dir = None
        try:
            with self.assertRaises(TypeError):
                self.workflow.run(self.config(), lambda event: None)
        finally:
            self.backend.data_dir = previous
        self.assertFalse(self.workflow.active)
        self.assertFalse(self.workflow.lock.locked())
        self.assertEqual(self.workflow.last_report['status'], 'failed')
        self.assertEqual(self.backend.calls, [])

    def test_concurrent_run_is_refused(self):
        self.workflow.lock.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, 'already active'):
                self.workflow.run(self.config(), lambda event: None)
        finally:
            self.workflow.lock.release()
        self.assertEqual(self.backend.calls, [])

    def test_optional_profile_and_analysis_are_opt_in(self):
        self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False), lambda event: None)
        self.assertFalse(any(item[0] in {'codex_profile', 'analysis_start'} for item in self.backend.calls))
        self.backend.calls.clear()
        self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False, codex_priority_enabled=True, background_analysis_enabled=True, analysis_interval=8), lambda event: None)
        self.assertIn(('codex_profile', 'High'), self.backend.calls)
        self.assertIn(('analysis_start', 8), self.backend.calls)


if __name__ == '__main__':
    unittest.main(verbosity=2)
