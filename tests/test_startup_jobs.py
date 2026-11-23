import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest

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


if __name__ == '__main__':
    unittest.main()
