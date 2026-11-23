import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from optimus import startup_jobs as sj  # noqa: E402
from optimus.workflow import DEFAULTS  # noqa: E402


class Lease:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class Backend:
    def __init__(self, root):
        self.data_dir = Path(root)
        self.reconciled = 0

    def reconcile_completed_updates(self, lease):
        self.reconciled += 1
        return {'status': 'reconciled'}


def settings(**changes):
    value = dict(DEFAULTS)
    value.update(driver_updates_enabled=True, software_updates_enabled=True, spyware_scan_enabled=True)
    value.update(changes)
    return value


def held_app_updates():
    """The real producer shape: two review refusals plus a coverage advisory."""
    from optimus.automated_software import UNKNOWN_VERSION_COVERAGE_WARNING
    from optimus.update_holds import held_update_result
    from optimus.update_scan_evidence import with_app_scan

    packages = [{'id': identity, 'source': 'winget', 'version': '1.0', 'available': '2.0'}
                for identity in ('Logitech.GHUB', 'Azul.Zulu.11.JRE', 'Git.Git')]
    held = held_update_result(packages[0], {'reason': 'Previous exact-offer outcome needs review.'})
    deferred = {'status': 'deferred', 'skipped': True, 'requiresReview': True, 'exitCode': None,
                'installer_started': False, 'outcomeKnown': True, 'running': False,
                'shared_barrier': True, 'note': 'A Windows restart is pending or could not be verified.'}
    return with_app_scan({'review_only': False,
        'scan': {'updates': packages, 'source': 'winget', 'structured': True, 'exitCode': 0,
                 'stderr': '', 'source_reported_offers_count': len(packages),
                 'excluded': [UNKNOWN_VERSION_COVERAGE_WARNING]},
        'items': [{'id': packages[0]['id'], 'status': 'skipped', 'result': held},
                  {'id': packages[1]['id'], 'status': 'skipped', 'result': deferred}],
        'failed_items': 3, 'scan_failed_items': 1, 'requires_review_items': 2,
        'skipped_items': 2, 'held_count': 1, 'held_ids': [packages[0]['id']],
        'deferred_ids': [packages[2]['id']], 'cancelled': False,
        'installer_started': False, 'outcomeKnown': True, 'running': False,
        'stop_reason': 'Update deferred: ' + deferred['note']})


class StartupJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = Backend(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_turn_the_feature_on(self):
        for key in ('startup_jobs_enabled', 'zero_breach_enabled', 'tracker_blocking_enabled', 'keep_running_on_close'):
            self.assertIs(DEFAULTS[key], True)

    def test_runs_every_enabled_job_in_order_and_saves_a_report(self):
        ran, lease = [], Lease()
        report = sj.run_startup_jobs(self.backend, settings(), lease_factory=lambda root: lease,
                                     job_runner=lambda key, *a: ran.append(key) or {'status': 'clean'})
        self.assertEqual(ran, ['drivers', 'apps', 'defender', 'zero_breach', 'trackers'])
        self.assertEqual(report['status'], 'completed')
        self.assertTrue(lease.released)
        self.assertEqual(self.backend.reconciled, 1)
        saved = json.loads((self.backend.data_dir / 'startup-jobs' / 'last.json').read_text())
        self.assertEqual([row['job'] for row in saved['jobs']], ran)

    def test_disabled_jobs_and_master_switch(self):
        ran = []
        sj.run_startup_jobs(self.backend, settings(driver_updates_enabled=False, zero_breach_enabled=False),
                            lease_factory=lambda root: Lease(), job_runner=lambda key, *a: ran.append(key))
        self.assertEqual(ran, ['apps', 'defender', 'trackers'])
        report = sj.run_startup_jobs(self.backend, settings(startup_jobs_enabled=False),
                                     lease_factory=lambda root: self.fail('no lease'), job_runner=lambda *a: self.fail('no job'))
        self.assertEqual(report['status'], 'disabled')

    def test_one_failure_does_not_stop_the_rest(self):
        def runner(key, *a):
            if key == 'apps':
                raise RuntimeError('winget blocked')
            return {}
        report = sj.run_startup_jobs(self.backend, settings(), lease_factory=lambda root: Lease(), job_runner=runner)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual([row['status'] for row in report['jobs']], ['completed', 'failed', 'completed', 'completed', 'completed'])

    def test_busy_lease_is_reported_not_raised(self):
        def busy(root):
            raise OSError('An optimization run is already active.')
        report = sj.run_startup_jobs(self.backend, settings(), lease_factory=busy, job_runner=lambda *a: self.fail('no job'))
        self.assertEqual(report['status'], 'busy')

    def test_cancel_skips_remaining_jobs(self):
        cancel = threading.Event()
        def runner(key, *a):
            cancel.set()
            return {}
        report = sj.run_startup_jobs(self.backend, settings(), cancel_event=cancel,
                                     lease_factory=lambda root: Lease(), job_runner=runner)
        self.assertEqual(report['status'], 'cancelled')
        self.assertEqual(sum(row['status'] == 'cancelled' for row in report['jobs']), 4)

    def test_default_app_dispatch_runs_and_reports_provider_progress(self):
        cancel, events, lease = threading.Event(), [], Lease()
        configuration = settings(driver_updates_enabled=False, spyware_scan_enabled=False,
                                 zero_breach_enabled=False, tracker_blocking_enabled=False)
        outcome = {'items': [], 'failed_items': 0, 'review_only': True}

        def update(backend, saved, *, cancel_event, item_callback):
            self.assertIs(backend, self.backend)
            self.assertEqual(saved['software_update_policy'], configuration['software_update_policy'])
            self.assertIs(cancel_event, cancel)
            self.assertFalse(lease.released)
            item_callback({'description': 'Fixture application discovery',
                           'item_progress': {'phase': 'scan', 'percent': None}})
            return outcome

        # Exercise the actual default dispatcher and report-based safety helper.
        # Only the application updater is replaced; no host update can run.
        with patch('optimus.automated_software.run_software', side_effect=update) as updater:
            report = sj.run_startup_jobs(self.backend, configuration, cancel_event=cancel,
                                        lease_factory=lambda root: lease, progress=events.append)
        updater.assert_called_once()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['jobs'][0]['job'], 'apps')
        self.assertEqual(report['jobs'][0]['status'], 'completed')
        self.assertEqual(report['jobs'][0]['result'], outcome)
        observed = [event for event in events if event.get('detail')]
        self.assertEqual(observed[0]['description'], 'Fixture application discovery')
        self.assertEqual(observed[0]['detail']['item_progress']['phase'], 'scan')
        self.assertTrue(lease.released)

    def test_default_app_dispatch_keeps_explicit_installer_refusal(self):
        reason = 'Fixture previous installer outcome requires review.'
        with patch('optimus.post_run_fixes._prior_installer_barrier', return_value=reason), \
             patch('optimus.automated_software.run_software') as updater:
            result = sj._run_job('apps', self.backend, settings(), None, lambda event: None)
        updater.assert_not_called()
        self.assertTrue(result['skipped'])
        self.assertTrue(result['requiresReview'])
        self.assertEqual(result['note'], reason)

    def test_default_app_dispatch_reports_the_actual_provider_failure(self):
        configuration = settings(driver_updates_enabled=False, spyware_scan_enabled=False,
                                 zero_breach_enabled=False, tracker_blocking_enabled=False)
        with patch('optimus.automated_software.run_software',
                   side_effect=RuntimeError('Fixture application discovery failed.')) as updater:
            report = sj.run_startup_jobs(self.backend, configuration, lease_factory=lambda root: Lease())
        updater.assert_called_once()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['jobs'][0]['error'], 'Fixture application discovery failed.')

    def test_live_progress_keeps_job_counts_separate_from_provider_counts(self):
        events, callbacks, lease = [], [], Lease()

        def run(key, backend, configuration, cancel, progress):
            callbacks.append(progress)
            progress({'current': 99, 'total': 100, 'status': 'completed',
                      'description': 'Fixture provider is still scanning'})
            return {'status': 'clean'}

        report = sj.run_startup_jobs(self.backend,
            settings(spyware_scan_enabled=False, zero_breach_enabled=False, tracker_blocking_enabled=False),
            lease_factory=lambda root: lease, job_runner=run, progress=events.append)
        running = [event for event in events if event.get('detail')]
        self.assertEqual([(event['job'], event['status'], event['current'], event['total']) for event in running],
                         [('drivers', 'running', 0, 2), ('apps', 'running', 1, 2)])
        self.assertEqual(running[0]['detail']['current'], 99)
        self.assertEqual(running[0]['description'], 'Fixture provider is still scanning')
        terminal = [event for event in events if event.get('job') and event['status'] != 'running']
        self.assertEqual([(event['job'], event['status'], event['current']) for event in terminal],
                         [('drivers', 'completed', 1), ('apps', 'completed', 2)])
        self.assertEqual(events[-1]['jobs'], report['jobs'])
        self.assertEqual(events[-1]['status'], 'completed')
        self.assertTrue(lease.released)
        count = len(events)
        callbacks[0]({'description': 'Late provider callback'})
        self.assertEqual(len(events), count)

    def test_completion_progress_follows_lease_release_and_saved_report(self):
        lease = Lease()
        completions = []

        def observe(event):
            if event.get('status') == 'completed' and 'jobs' in event:
                self.assertTrue(lease.released)
                saved = json.loads((self.backend.data_dir / 'startup-jobs' / 'last.json').read_text())
                self.assertEqual(event['jobs'], saved['jobs'])
                completions.append(event)

        sj.run_startup_jobs(self.backend, settings(), lease_factory=lambda root: lease,
                            job_runner=lambda *args: {'status': 'clean'}, progress=observe)
        self.assertEqual(len(completions), 1)

    def test_progress_observer_cannot_fail_or_alter_maintenance(self):
        events, ran, lease = [], [], Lease()

        def observe(event):
            events.append(event)
            if 'result' in event:
                event['result']['nested']['value'] = 'observer changed its copy'
            raise RuntimeError('Fixture display failure')

        def run(key, *args):
            ran.append(key)
            return {'nested': {'value': 'original'}}

        report = sj.run_startup_jobs(self.backend, settings(), lease_factory=lambda root: lease,
                                     job_runner=run, progress=observe)
        self.assertEqual(len(ran), 5)
        self.assertEqual(report['status'], 'completed')
        self.assertTrue(lease.released)
        self.assertTrue(all(job['result']['nested']['value'] == 'original' for job in report['jobs']))

    def test_failure_and_cancellation_are_visible_as_terminal_job_events(self):
        cancel, events = threading.Event(), []

        def run(key, *args):
            cancel.set()
            raise RuntimeError('Fixture driver failure')

        report = sj.run_startup_jobs(self.backend, settings(), cancel_event=cancel,
            lease_factory=lambda root: Lease(), job_runner=run, progress=events.append)
        terminal = [event for event in events if event.get('job') and event['status'] != 'running']
        self.assertEqual([event['status'] for event in terminal], ['failed'] + ['cancelled'] * 4)
        self.assertEqual([event['current'] for event in terminal], [1, 2, 3, 4, 5])
        self.assertEqual(terminal[0]['error'], 'Fixture driver failure')
        self.assertEqual(events[-1]['status'], report['status'])
        self.assertEqual(events[-1]['status'], 'cancelled')

    def test_failed_provider_results_are_not_labelled_completed(self):
        for result in ({'status': 'blocked', 'failed_items': 1},
                       {'failed_items': 1, 'skipped': True},
                       {'update': {'status': 'check_failed'}, 'scan': {'status': 'not_installed'}},
                       {'blocked': {'status': 'partial', 'errors': ['Fixture block failed']}}):
            with self.subTest(result=result):
                self.assertEqual(sj._status(result), 'failed')
        self.assertEqual(sj._status({'update': {'status': 'updated'}, 'scan': {'status': 'clean'}}), 'completed')

    def test_held_update_outcomes_need_review_without_mutating_evidence(self):
        outcome = held_app_updates()
        original = copy.deepcopy(outcome)
        self.assertEqual(sj._status(outcome), 'needs_review')
        self.assertEqual(sj._skip_status(outcome, sj._status(outcome)), 'needs_review')
        self.assertEqual(outcome, original)

    def test_held_update_pass_preserves_review_status_and_does_not_retry(self):
        outcome, events, timers = held_app_updates(), [], []
        configuration = settings(driver_updates_enabled=False, spyware_scan_enabled=False,
                                 zero_breach_enabled=False, tracker_blocking_enabled=False)

        def run(backend, saved, **arguments):
            return sj.run_startup_jobs(backend, saved, lease_factory=lambda root: Lease(), **arguments)

        jobs = sj.StartupJobs(self.backend, lambda: configuration, runner=run,
            on_progress=events.append, timer_factory=lambda *arguments: timers.append(arguments))
        with patch('optimus.automated_software.run_software', return_value=outcome) as updater:
            self.assertTrue(jobs.start())
            jobs.thread.join(5)
        self.assertFalse(jobs.running)
        updater.assert_called_once()
        self.assertEqual(jobs.last['status'], 'needs_review')
        self.assertEqual(jobs.last['jobs'][0]['status'], 'needs_review')
        self.assertEqual(jobs.last['jobs'][0]['result'], outcome)
        self.assertEqual(timers, [])
        self.assertIsNone(jobs.timer)
        saved = json.loads((self.backend.data_dir / 'startup-jobs' / 'last.json').read_text())
        self.assertEqual(saved, jobs.last)
        self.assertEqual(events[-1]['status'], 'needs_review')
        self.assertEqual(events[-1]['jobs'][0]['status'], 'needs_review')

    def test_review_bookkeeping_never_hides_a_real_item_failure_or_uncertainty(self):
        for status, receipt, expected in (
                ('failed', {'status': 'failed', 'requiresReview': True, 'exitCode': 1,
                            'installer_started': False, 'outcomeKnown': True, 'running': False}, 'failed'),
                ('skipped', {'status': 'error', 'requiresReview': True,
                             'outcomeKnown': True, 'running': False}, 'failed'),
                ('skipped', {'status': 'uncertain', 'requiresReview': True,
                             'outcomeKnown': False, 'running': True}, 'uncertain')):
            with self.subTest(expected=expected, status=receipt['status']):
                outcome = held_app_updates()
                outcome['items'].append({'id': 'Fixture.Failed', 'status': status, 'result': receipt})
                outcome['failed_items'] += 1
                outcome['requires_review_items'] += 1
                self.assertEqual(sj._status(outcome), expected)
                self.assertEqual(sj._skip_status(outcome, sj._status(outcome)), expected)

    def test_other_scan_problems_and_unexplained_counters_remain_failed(self):
        from optimus.update_scan_evidence import with_app_scan
        changes = ({'exitCode': 1}, {'structured': False}, {'complete': False},
                   {'stderr': 'Source refresh failed.'}, {'source_reported_offers_count': 4},
                   {'excluded': ['Unrecognized source response.']}, {'error': 'Discovery failed.'})
        for change in changes:
            with self.subTest(change=change):
                outcome = held_app_updates()
                outcome['scan'].update(change)
                with_app_scan(outcome)
                self.assertEqual(sj._status(outcome), 'failed')
                self.assertEqual(sj._skip_status(outcome, 'cancelled'), 'failed')
        outcome = held_app_updates()
        outcome['failed_items'] += 1
        self.assertEqual(sj._status(outcome), 'failed')
        for invalid in (3.0, True):
            with self.subTest(invalid_counter=invalid):
                outcome = held_app_updates()
                outcome['failed_items'] = invalid
                if invalid is True:
                    outcome['items'] = outcome['items'][:1]
                    outcome['scan_failed_items'] = 0
                self.assertEqual(sj._status(outcome), 'failed')
        outcome = held_app_updates()
        outcome['app_scan']['errors'].append({'source': 'winget', 'error': 'Other source error.'})
        self.assertEqual(sj._status(outcome), 'failed')

    def test_cancelled_review_pass_preserves_cancellation_and_skip_safety(self):
        outcome = held_app_updates()
        outcome['cancelled'] = True
        self.assertEqual(sj._status(outcome), 'cancelled')
        self.assertEqual(sj._skip_status(outcome, sj._status(outcome)), 'skipped')
        outcome['items'].append({'id': 'Fixture.Failed', 'status': 'failed',
                                 'result': {'status': 'failed', 'outcomeKnown': True, 'running': False}})
        outcome['failed_items'] += 1
        self.assertEqual(sj._skip_status(outcome, sj._status(outcome)), 'failed')
        outcome['items'][-1]['result']['outcomeKnown'] = False
        self.assertEqual(sj._status(outcome), 'uncertain')
        self.assertEqual(sj._skip_status(outcome, sj._status(outcome)), 'uncertain')

    def test_unresolved_provider_lifecycle_is_never_labelled_finished_or_skipped(self):
        for result in ({'skipped': True, 'uncertain': True, 'running': True, 'outcomeKnown': False},
                       {'cancelled': True, 'running': True},
                       {'update': {'status': 'completed'}, 'scan': {'outcomeKnown': False}}):
            with self.subTest(result=result):
                self.assertEqual(sj._status(result), 'uncertain')

    def test_backend_scan_progress_is_optional_for_native_backends(self):
        events, calls = [], []

        def windows_scan(*, settings, cancel_event, progress_callback):
            calls.append(settings)
            progress_callback({'description': 'Fixture Defender scan'})
            return {'status': 'clean'}

        backend = types.SimpleNamespace(scan_spyware_automatic=windows_scan)
        self.assertEqual(sj._run_job('defender', backend, {'fixture': True}, None, events.append),
                         {'status': 'clean'})
        self.assertEqual(events, [{'description': 'Fixture Defender scan'}])
        self.assertEqual(calls, [{'fixture': True}])
        native = types.SimpleNamespace(scan_spyware=lambda cancel_event=None: {'status': 'clean'})
        self.assertEqual(sj._run_job('defender', native, {}, None, events.append), {'status': 'clean'})

    def test_controller_forwards_live_progress_and_suppresses_callbacks_after_stop(self):
        entered, release, events = threading.Event(), threading.Event(), []

        def run(backend, configuration, *, cancel_event, progress):
            progress({'job': 'defender', 'status': 'running', 'description': 'Fixture scan'})
            entered.set()
            release.wait(5)
            progress({'job': 'defender', 'status': 'completed', 'description': 'Late display event'})
            return {'status': 'cancelled', 'jobs': []}

        jobs = sj.StartupJobs(self.backend, lambda: settings(), runner=run, on_progress=events.append)
        try:
            self.assertTrue(jobs.start())
            self.assertTrue(entered.wait(5))
            jobs.stop()
        finally:
            release.set()
            jobs.thread.join(5)
        self.assertFalse(jobs.running)
        self.assertEqual([event['description'] for event in events], ['Fixture scan'])
        self.assertEqual(jobs.last['status'], 'cancelled')
        self.assertIsNone(jobs.timer)

    def test_controller_preserves_runner_without_optional_progress_parameter(self):
        def legacy(backend, configuration, *, cancel_event):
            return {'status': 'completed', 'jobs': []}

        jobs = sj.StartupJobs(self.backend, lambda: settings(), runner=legacy, on_progress=lambda event: None)
        jobs.start()
        jobs.thread.join(5)
        self.assertEqual(jobs.last['status'], 'completed')

    def test_controller_retries_when_busy_and_stops_cleanly(self):
        timers = []

        class Timer:
            def __init__(self, delay, callback):
                self.delay, self.callback, self.cancelled, self.daemon = delay, callback, False, False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True
        jobs = sj.StartupJobs(self.backend, lambda: settings(), runner=lambda *a, **k: {'status': 'busy', 'jobs': []},
                              timer_factory=Timer)
        self.assertTrue(jobs.start())
        jobs.thread.join(5)
        self.assertEqual(jobs.last['status'], 'busy')
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0].delay, sj.RETRY_SECONDS)
        jobs.stop()
        self.assertTrue(timers[0].cancelled)

    def test_controller_does_not_retry_after_success(self):
        jobs = sj.StartupJobs(self.backend, lambda: settings(), runner=lambda *a, **k: {'status': 'completed', 'jobs': []},
                              timer_factory=lambda *a: self.fail('no retry'))
        jobs.start()
        jobs.thread.join(5)
        self.assertFalse(jobs.running)
        self.assertIsNone(jobs.timer)


class StartupJobSkipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = Backend(self.temp.name)
        self.timers = []

    def tearDown(self):
        self.temp.cleanup()

    def controller(self, provider, *, configuration=None, events=None):
        configuration = configuration or settings(spyware_scan_enabled=False,
            zero_breach_enabled=False, tracker_blocking_enabled=False)
        recorded = events if events is not None else []
        timers = self.timers

        class Timer:
            def __init__(self, delay, callback):
                self.delay, self.callback = delay, callback
                self.cancelled, self.daemon = False, False
                timers.append(self)

            def start(self):
                pass  # A fixture explicitly advances any retry.

            def cancel(self):
                self.cancelled = True

        def run(backend, saved, **arguments):
            return sj.run_startup_jobs(backend, saved, lease_factory=lambda root: Lease(),
                                       job_runner=provider, **arguments)

        jobs = sj.StartupJobs(self.backend, lambda: dict(configuration), runner=run,
                              timer_factory=Timer, on_progress=recorded.append)

        def cleanup():
            jobs.stop()
            if jobs.thread is not None:
                jobs.thread.join(5)
        self.addCleanup(cleanup)
        return jobs

    def test_queued_skip_never_dispatches_and_does_not_change_saved_choices(self):
        entered, release, calls, events = threading.Event(), threading.Event(), [], []
        configuration = settings(zero_breach_enabled=False, tracker_blocking_enabled=False)
        saved_choices = json.dumps(configuration, sort_keys=True)

        def provider(key, backend, config, cancel, progress):
            calls.append(key)
            if key == 'drivers':
                entered.set()
                self.assertTrue(release.wait(4))
            return {'status': 'clean'}

        jobs = self.controller(provider, configuration=configuration, events=events)
        self.assertFalse(jobs.skip('apps'))
        self.assertTrue(jobs.start())
        try:
            self.assertTrue(entered.wait(4))
            self.assertTrue(jobs.skip('apps'))
            self.assertFalse(jobs.skip('apps'))
            self.assertFalse(jobs.cancel.is_set())
            accepted = [event for event in events if event.get('job') == 'apps']
            self.assertEqual(accepted[-1]['status'], 'skipped')
            self.assertTrue(accepted[-1]['skip_requested'])
        finally:
            release.set()
            jobs.thread.join(5)
        self.assertFalse(jobs.running)
        self.assertEqual(calls, ['drivers', 'defender'])
        row = next(row for row in jobs.last['jobs'] if row['job'] == 'apps')
        self.assertEqual(row['status'], 'skipped')
        self.assertIs(row['result']['installer_started'], False)
        self.assertTrue(row['skip_requested'])
        self.assertEqual(json.dumps(configuration, sort_keys=True), saved_choices)
        self.assertFalse((self.backend.data_dir / 'preferences.json').exists())

    def test_active_skip_waits_for_saved_outcome_then_continues_next_job(self):
        entered, cancelled, receipt_release = threading.Event(), threading.Event(), threading.Event()
        next_entered, receipt_saved, events = threading.Event(), threading.Event(), []

        def provider(key, backend, config, cancel, progress):
            if key == 'drivers':
                entered.set()
                self.assertTrue(cancel.wait(4))
                cancelled.set()
                self.assertTrue(receipt_release.wait(4))
                receipt_saved.set()
                return {'cancelled': True, 'installer_started': True, 'outcomeKnown': True,
                        'running': False, 'receipt_saved': True}
            self.assertTrue(receipt_saved.is_set())
            next_entered.set()
            return {'status': 'completed'}

        jobs = self.controller(provider, events=events)
        jobs.start()
        try:
            self.assertTrue(entered.wait(4))
            self.assertTrue(jobs.skip('drivers'))
            self.assertTrue(cancelled.wait(4))
            self.assertTrue(jobs.running)
            self.assertFalse(jobs.cancel.is_set())
            self.assertFalse(next_entered.is_set())
            driver_events = [event for event in events if event.get('job') == 'drivers']
            self.assertEqual(driver_events[-1]['status'], 'running')
            self.assertTrue(driver_events[-1]['skip_requested'])
        finally:
            receipt_release.set()
            jobs.thread.join(5)
        self.assertTrue(next_entered.is_set())
        self.assertEqual(jobs.last['status'], 'completed')
        first = jobs.last['jobs'][0]
        self.assertEqual(first['status'], 'skipped')
        self.assertTrue(first['result']['receipt_saved'])
        self.assertTrue(first['skip_requested'])
        self.assertEqual(jobs.last['jobs'][1]['status'], 'completed')

    def test_skip_acknowledgement_cannot_arrive_after_the_terminal_result(self):
        entered, next_entered, events, tokens = threading.Event(), threading.Event(), [], {}

        def observe(event):
            if (event.get('job') == 'drivers' and event.get('skip_requested')
                    and event.get('status') == 'running'
                    and event.get('description', '').startswith('Skipping safely;')):
                # Force the formerly unsafe interleaving if cancellation was
                # signalled before this acknowledgement could be delivered.
                if tokens['driver'].is_set():
                    next_entered.wait(3)
            events.append(event)

        def provider(key, backend, config, cancel, progress):
            if key == 'drivers':
                tokens['driver'] = cancel
                entered.set()
                self.assertTrue(cancel.wait(4))
                return {'cancelled': True, 'outcomeKnown': True, 'running': False}
            next_entered.set()
            return {'status': 'completed'}

        jobs = self.controller(provider)
        jobs.on_progress = observe
        jobs.start()
        self.assertTrue(entered.wait(4))
        self.assertTrue(jobs.skip('drivers'))
        jobs.thread.join(5)
        self.assertFalse(jobs.running)
        self.assertTrue(next_entered.is_set())
        driver_events = [event for event in events if event.get('job') == 'drivers']
        self.assertEqual(driver_events[-1]['status'], 'skipped')
        requested = next(index for index, event in enumerate(driver_events)
                         if event.get('skip_requested') and event['status'] == 'running')
        terminal = next(index for index, event in enumerate(driver_events) if event['status'] == 'skipped')
        self.assertLess(requested, terminal)

    def test_skip_request_keeps_natural_completion_authoritative(self):
        entered, release = threading.Event(), threading.Event()

        def provider(key, backend, config, cancel, progress):
            if key == 'drivers':
                entered.set()
                self.assertTrue(release.wait(4))
            return {'status': 'completed', 'verified': True}

        jobs = self.controller(provider)
        jobs.start()
        try:
            self.assertTrue(entered.wait(4))
            self.assertTrue(jobs.skip('drivers'))
        finally:
            release.set()
            jobs.thread.join(5)
        self.assertEqual(jobs.last['jobs'][0]['status'], 'completed')
        self.assertTrue(jobs.last['jobs'][0]['skip_requested'])
        self.assertTrue(jobs.last['jobs'][0]['result']['verified'])

    def test_active_skip_does_not_hide_failed_or_uncertain_native_results(self):
        for expected, outcome in (
                ('failed', {'cancelled': True, 'failed_items': 1, 'status': 'failed'}),
                ('uncertain', {'cancelled': True, 'uncertain': True, 'outcomeKnown': False})):
            with self.subTest(expected=expected):
                entered, release = threading.Event(), threading.Event()

                def provider(key, backend, config, cancel, progress):
                    if key == 'drivers':
                        entered.set()
                        self.assertTrue(release.wait(4))
                        return outcome
                    return {'status': 'completed'}

                jobs = self.controller(provider)
                jobs.start()
                try:
                    self.assertTrue(entered.wait(4))
                    self.assertTrue(jobs.skip('drivers'))
                finally:
                    release.set()
                    jobs.thread.join(5)
                self.assertEqual(jobs.last['jobs'][0]['status'], expected)
                self.assertEqual(jobs.last['jobs'][0]['result'], outcome)
                self.assertEqual(jobs.last['status'], 'uncertain' if expected == 'uncertain' else 'failed')
                self.assertEqual(jobs.last['jobs'][1]['status'],
                                 'skipped' if expected == 'uncertain' else 'completed')
                if expected == 'uncertain':
                    self.assertTrue(jobs.last['jobs'][1]['result']['requiresReview'])
                    self.assertIn('unresolved native outcome', jobs.last['detail'])
                    self.assertIsNone(jobs.timer)
                jobs.stop()

    def test_global_stop_still_cancels_all_remaining_jobs(self):
        entered, calls = threading.Event(), []

        def provider(key, backend, config, cancel, progress):
            calls.append(key)
            entered.set()
            self.assertTrue(cancel.wait(4))
            return {'cancelled': True, 'outcomeKnown': True, 'running': False}

        jobs = self.controller(provider)
        jobs.start()
        self.assertTrue(entered.wait(4))
        jobs.stop()
        jobs.thread.join(5)
        self.assertTrue(jobs.cancel.is_set())
        self.assertEqual(calls, ['drivers'])
        self.assertEqual([row['status'] for row in jobs.last['jobs']], ['cancelled', 'cancelled'])
        self.assertEqual(jobs.last['status'], 'cancelled')
        self.assertFalse(jobs.skip('apps'))

    def test_skip_requests_survive_automatic_retry_and_clear_old_report(self):
        first, second = threading.Event(), threading.Event()
        release_first, release_second = threading.Event(), threading.Event()
        calls = []

        def provider(key, backend, config, cancel, progress):
            calls.append(key)
            if len(calls) == 1:
                first.set()
                self.assertTrue(release_first.wait(4))
            else:
                second.set()
                self.assertTrue(release_second.wait(4))
            return {'status': 'failed', 'failed_items': 1}

        jobs = self.controller(provider)
        jobs.start()
        try:
            self.assertTrue(first.wait(4))
            self.assertTrue(jobs.skip('apps'))
            release_first.set()
            jobs.thread.join(5)
            first_report = jobs.last
            self.assertEqual(len(self.timers), 1)
            self.timers[0].callback()
            self.assertTrue(second.wait(4))
            self.assertIsNone(jobs.last)
            self.assertFalse(jobs.cancel.is_set())
            release_second.set()
            jobs.thread.join(5)
        finally:
            release_first.set()
            release_second.set()
        self.assertEqual(calls, ['drivers', 'drivers'])
        self.assertEqual(first_report['jobs'][1]['status'], 'skipped')
        self.assertEqual(jobs.last['jobs'][1]['status'], 'skipped')
        self.assertTrue(jobs.last['jobs'][1]['skip_requested'])

    def test_unknown_disabled_and_completed_job_requests_are_rejected(self):
        entered, release = threading.Event(), threading.Event()

        def provider(key, backend, config, cancel, progress):
            if key == 'apps':
                entered.set()
                self.assertTrue(release.wait(4))
            return {'status': 'completed'}

        jobs = self.controller(provider)
        jobs.start()
        try:
            self.assertTrue(entered.wait(4))
            self.assertFalse(jobs.skip('drivers'))
            self.assertFalse(jobs.skip('unknown'))
            self.assertFalse(jobs.skip('trackers'))
            self.assertFalse(jobs.skip(None))
        finally:
            release.set()
            jobs.thread.join(5)
        self.assertFalse(jobs.skip('apps'))

    def test_owned_unresolved_journal_blocks_both_updaters_without_dispatch(self):
        from optimus.speed_enhancements import _current_owner
        directory = self.backend.data_dir / 'parallel-updates'
        directory.mkdir()
        identifier = 'a' * 32
        path = directory / (identifier + '.json')
        original = json.dumps({'schema': 1, 'id': identifier, 'owner': _current_owner(),
                               'status': 'prepared', 'installer_prepared': True})
        path.write_text(original)
        with patch('optimus.automated_system.run_drivers') as drivers, \
             patch('optimus.automated_software.run_software') as apps:
            report = sj.run_startup_jobs(self.backend,
                settings(spyware_scan_enabled=False, zero_breach_enabled=False, tracker_blocking_enabled=False),
                lease_factory=lambda root: Lease())
        drivers.assert_not_called()
        apps.assert_not_called()
        self.assertEqual([row['status'] for row in report['jobs']], ['needs_review', 'needs_review'])
        self.assertEqual(report['status'], 'needs_review')
        for row in report['jobs']:
            self.assertTrue(row['result']['requiresReview'])
            self.assertTrue(row['result']['installation_blocked'])
            self.assertIn('unresolved outcome', row['result']['note'])
            self.assertIs(row['result']['installer_started'], False)
        self.assertEqual(path.read_text(), original)


if __name__ == '__main__':
    unittest.main()
