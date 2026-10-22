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


def mock_verification(report, **kwargs):
    return {'schema': 1, 'read_only': True, 'status': 'not_applicable',
            'counts': {'confirmed': 0, 'needs_review': 0, 'unverified': 0, 'not_applicable': len(report['stages'])},
            'coverage_complete': True, 'failed_items': 0,
            'stages': [{'title': row['title'], 'status': 'not_applicable', 'checks': []} for row in report['stages']],
            'summary': 'Fixture outcome verification completed without native system access.'}


class MockBackend:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.bridge = types.SimpleNamespace(_process=None)
        self.calls = []
        self.audit_callback = None
        self.scan_callback = None
        self.fail_cleanup_once = False
        self.verifications = []
    def verify_optimization(self, report, **kwargs):
        self.verifications.append(copy.deepcopy(report['stages']))
        return mock_verification(report, **kwargs)
    def audit(self):
        self.calls.append(('audit',))
        if self.audit_callback: self.audit_callback()
        return {'fixture': True}
    def cleanup_categories(self):
        return [{'id': 'chrome:Default:Cache', 'process_names': ['chrome.exe'], 'available': True}, {'id': 'mock-extra', 'available': True}]
    def browser_cleanup_status(self, browser_id):
        return {'running': False, 'complete': True, 'can_close': True, 'processes': [], 'errors': []}
    def browser_cleanup_close(self, browser_id, *, cancel_event=None):
        raise AssertionError('Closed-browser workflow fixture must not attempt a browser close.')
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
        return {'planId': 'mock-plan', 'targets': [{'id': 'mock-target', 'name': 'Microsoft.BingNews', 'kind': 'appx', 'scope': 'current-user'}]}
    def debloat_apply(self, plan):
        self.calls.append(('debloat_apply', plan))
        return {'fixture': True}
    def installed_apps(self):
        self.calls.append(('installed_apps',)); return [{'Name': 'Fixture'}]
    def processes(self):
        self.calls.append(('processes',)); return [{'pid': 123, 'memory_rss': 1024}]
    def slowdown_report(self, cancel_event=None):
        self.calls.append(('processes',)); return {'sampled': True, 'programs': []}
    def startup_list(self):
        self.calls.append(('startup_list',)); return [{'Name': 'Fixture'}]
    def duplicate_scan(self, root):
        self.calls.append(('duplicate_scan', root)); return {'Groups': []}
    def cloud_status(self):
        self.calls.append(('cloud_status',)); return {'connected': getattr(self, 'cloud_connected', False)}
    def cloud_scan(self):
        self.calls.append(('cloud_scan',)); return {'groups': []}
    def driver_updates_scan(self):
        self.calls.append(('driver_updates_scan',)); return getattr(self, 'driver_result', {'resultCode': 2, 'updates': []})
    def updates_scan(self):
        self.calls.append(('updates_scan',)); return copy.deepcopy(getattr(self, 'update_scan', {'structured': True, 'exitCode': 0, 'updates': [
            {'id': 'Fixture.One', 'package_id': 'Fixture.One', 'source': 'winget', 'version': '1.0', 'available': '2.0'},
            {'id': 'Fixture.Two', 'package_id': 'Fixture.Two', 'source': 'winget', 'version': '1.0', 'available': '2.0'}]}))
    def update_app_unattended(self, identity, *, cancel_event=None):
        self.calls.append(('update_app_unattended', identity))
        if getattr(self, 'update_callback', None): self.update_callback(identity)
        if getattr(self, 'update_timeout', False): raise RuntimeError('Vendor installer timed out and may still be running')
        return {'id': identity, 'exitCode': getattr(self, 'update_exit_code', 0)}
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
        for invalid in ({'cleanup_enabled': 'yes'}, {'cleanup_age_days': True}, {'cleanup_age_days': -1}, {'analysis_interval': 1}, {'codex_priority': 'RealTime'}, {'intensity': 'Extreme'}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError): workflow_module.Preferences.validate(invalid)

    def test_preferences_round_trip_and_corrupt_file_fallback(self):
        preferences = workflow_module.Preferences(self.temp.name)
        saved = preferences.save(self.config(cleanup_enabled=False, analysis_interval=8))
        self.assertEqual(preferences.load(), saved)
        preferences.path.write_text('{broken', encoding='utf-8')
        self.assertEqual(preferences.load(), workflow_module.Preferences.safe_defaults())
        self.assertEqual(preferences.path.read_text(), '{broken')

    def test_progress_corresponds_to_completed_stages_and_log(self):
        events = []
        result = self.workflow.run(self.config(), events.append)
        self.assertEqual(len(result['stages']), 6)
        self.assertTrue(result['maintenance_insights']['read_only'])
        self.assertEqual(result['maintenance_insights']['resource_changes']['status'], 'unavailable')
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
        with self.assertRaises(ValueError): self.workflow.run({'cleanup_age_days': -1}, lambda event: None)
        self.assertFalse(self.workflow.active)
        result = self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False), lambda event: None)
        self.assertEqual(len(result['stages']), 4)

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
        self.assertEqual(len(result['stages']), 4)

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


    def test_old_settings_migrate_without_automatic_opt_in(self):
        old = {'cleanup_enabled': False, 'background_analysis_enabled': True}
        result = workflow_module.Preferences.validate(old)
        self.assertEqual(result['setup_version'], 0)
        self.assertEqual(result['automation_mode'], 'manual')
        for key in ('applications_review_enabled', 'performance_review_enabled', 'startup_review_enabled', 'duplicates_enabled', 'cloud_review_enabled', 'software_updates_enabled', 'driver_updates_enabled'):
            self.assertFalse(result[key])
        self.assertTrue(result['background_analysis_enabled'])

    def test_new_preferences_strict_validation_and_no_disabled_update_consent(self):
        bad = [{'setup_version': True}, {'automation_mode': 'hourly'}, {'duplicate_roots': r'C:\Users\Example'},
               {'duplicate_roots': ['C:/']}, {'duplicate_roots': ['relative']}, {'duplicate_roots': [r'\\server\share\folder']},
               {'software_update_ids': ['--all']}, {'software_update_ids': ['Fixture.One', 'fixture.one']},
               {'cleanup_category_ids': ['unknown command;']}, {'driver_updates_enabled': 1},
               {'software_updates_enabled': True, 'software_update_policy': 'selected', 'software_update_ids': ['Fixture.One']},
               {'software_updates_enabled': True, 'software_update_policy': 'selected', 'software_update_agreements_accepted': True}]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError): workflow_module.Preferences.validate(value)
        valid = workflow_module.Preferences.validate({'software_updates_enabled': False, 'software_update_policy': 'selected'})
        self.assertFalse(valid['software_update_agreements_accepted'])

    def test_review_stages_never_call_mutation_methods(self):
        self.backend.cloud_connected = True
        result = self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False,
            applications_review_enabled=True, performance_review_enabled=True, startup_review_enabled=True,
            duplicates_enabled=True, duplicate_roots=[r'C:\Users\Example\Documents'], cloud_review_enabled=True, driver_updates_enabled=True), lambda event: None)
        self.assertEqual(result['failed_stages'], 0)
        self.assertEqual([call[0] for call in self.backend.calls], ['audit', 'installed_apps', 'processes', 'startup_list', 'duplicate_scan', 'cloud_status', 'cloud_scan', 'driver_updates_scan', 'audit'])
        self.assertTrue(all(stage['result']['review_only'] for stage in result['stages'][1:-3]))

    def test_unconnected_cloud_and_empty_folders_are_truthful_skips(self):
        result = self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False, cloud_review_enabled=True, duplicates_enabled=True), lambda event: None)
        self.assertFalse(any(call[0] in ('cloud_scan', 'duplicate_scan') for call in self.backend.calls))
        self.assertTrue(all(stage['result']['skipped'] for stage in result['stages'][1:-3]))

    def test_selected_extra_cleanup_runs_independently_of_user_temp(self):
        self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False, cleanup_category_ids=['mock-extra', 'missing-category']), lambda event: None)
        scans = [call for call in self.backend.calls if call[0] == 'cleanup_scan']
        self.assertEqual(scans, [('cleanup_scan', ['mock-extra'], 7)])

    def software_config(self, **updates):
        return self.config(cleanup_enabled=False, browser_cache_enabled=False, software_updates_enabled=True,
            software_update_policy='selected', software_update_agreements_accepted=True,
            software_update_ids=updates.pop('software_update_ids', ['Fixture.Two', 'Fixture.Absent']), **updates)

    def test_unattended_updates_apply_only_configured_fresh_ids_and_emit_item_progress(self):
        events = []
        result = self.workflow.run(self.software_config(), events.append)
        self.assertEqual([call for call in self.backend.calls if call[0] == 'update_app_unattended'], [('update_app_unattended', 'Fixture.Two')])
        stage = next(item for item in result['stages'] if 'software' in item['title'])
        self.assertEqual(stage['result']['not_available_ids'], ['Fixture.Absent'])
        self.assertTrue(any(event.get('items') == 1 for event in events))
        self.assertEqual(result['failed_stages'], 0)

    def test_review_policy_never_installs_software(self):
        self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False, software_updates_enabled=True), lambda event: None)
        self.assertFalse(any(call[0] == 'update_app_unattended' for call in self.backend.calls))

    def test_ambiguous_unrecognized_or_wrong_source_updates_never_apply(self):
        base = self.backend.updates_scan()
        self.backend.calls.clear()
        scans = [dict(base, structured=False), dict(base, exitCode=5),
                 dict(base, updates=[dict(base['updates'][0], source='msstore')]),
                 dict(base, updates=[base['updates'][0], base['updates'][0]]),
                 dict(base, updates=[dict(base['updates'][0], version='Unknown')])]
        for scan in scans:
            with self.subTest(scan=scan):
                self.backend.update_scan = scan
                self.backend.calls.clear()
                self.workflow.run(self.software_config(software_update_ids=['Fixture.One']), lambda event: None)
                self.assertFalse(any(call[0] == 'update_app_unattended' for call in self.backend.calls))

    def test_cancel_between_software_items_does_not_apply_remaining_or_claim_100(self):
        event = threading.Event()
        self.backend.update_callback = lambda identity: event.set()
        progress = []
        result = self.workflow.run(self.software_config(software_update_ids=['Fixture.One', 'Fixture.Two']), progress.append, event)
        self.assertEqual([call for call in self.backend.calls if call[0] == 'update_app_unattended'], [('update_app_unattended', 'Fixture.One')])
        self.assertEqual(result['status'], 'cancelled')
        self.assertLess(max(item['percentage'] for item in progress), 100)

    def test_software_nonzero_results_and_driver_scan_failure_are_failed_stages(self):
        self.backend.update_exit_code = 5
        self.backend.driver_result = {'resultCode': 4, 'updates': []}
        result = self.workflow.run(self.software_config(driver_updates_enabled=True), lambda event: None)
        self.assertEqual(result['failed_stages'], 2)
        self.assertEqual(result['status'], 'completed_with_errors')
        stage = next(item for item in result['stages'] if 'software' in item['title'])
        self.assertEqual(stage['result']['items'][0]['result']['exitCode'], 5)

    def test_installer_timeout_defers_remaining_selected_ids(self):
        self.backend.update_timeout = True
        result = self.workflow.run(self.software_config(software_update_ids=['Fixture.One', 'Fixture.Two']), lambda event: None)
        self.assertEqual([call for call in self.backend.calls if call[0] == 'update_app_unattended'], [('update_app_unattended', 'Fixture.One')])
        stage = next(item for item in result['stages'] if 'software' in item['title'])
        self.assertEqual(stage['result']['deferred_ids'], ['Fixture.Two'])
        self.assertEqual(result['failed_stages'], 1)

    def test_configured_run_records_attempt_and_completion_without_changing_preferences(self):
        from optimus.automation import load_attempt
        config = self.config(cleanup_enabled=False, browser_cache_enabled=False, setup_version=workflow_module.CURRENT_SETUP_VERSION, automation_mode='daily')
        result = self.workflow.run(config, lambda event: None)
        state = load_attempt(self.backend.data_dir)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['run_id'], result['id'])
        self.assertLessEqual(state['attempted_at'], state['finished_at'])
        self.assertFalse((self.backend.data_dir / 'preferences.json').exists())

    def test_nested_uninstall_refusal_remains_visible_as_failed_stage(self):
        self.backend.debloat_apply = lambda plan: {'results': [{'id': 'mock-target', 'removed': False, 'status': 'Removal was not verified'}]}
        report = self.workflow.run(self.config(cleanup_enabled=False, browser_cache_enabled=False, consumer_debloat_enabled=True), lambda event: None)
        self.assertEqual(report['failed_stages'], 1)
        self.assertEqual(report['status'], 'completed_with_errors')
        stage = report['stages'][1]
        self.assertEqual(stage['status'], 'failed')
        self.assertFalse(stage['result']['results'][0]['removed'])

    def test_failed_configured_run_postpones_daily_retry_with_original_attempt_time(self):
        from optimus.automation import due, load_attempt
        self.backend.driver_result = {'resultCode': 4, 'updates': []}
        config = self.config(cleanup_enabled=False, browser_cache_enabled=False, setup_version=workflow_module.CURRENT_SETUP_VERSION, automation_mode='daily', driver_updates_enabled=True)
        report = self.workflow.run(config, lambda event: None)
        state = load_attempt(self.backend.data_dir)
        self.assertEqual(report['failed_stages'], 1)
        self.assertEqual(state['status'], 'failed')
        self.assertFalse(due(config, state, now=state['finished_at'] + 10))

    def test_separate_workflow_objects_cannot_overlap_same_data_directory(self):
        from optimus.automation import RunLease
        with RunLease(self.backend.data_dir):
            with self.assertRaisesRegex(RuntimeError, 'Another Optimus'):
                self.workflow.run(self.config(), lambda event: None)
        self.assertFalse(self.workflow.active)
        self.assertEqual(self.backend.calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
