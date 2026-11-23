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
