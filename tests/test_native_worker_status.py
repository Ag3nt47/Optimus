"""Optional worker status observes actual owner transitions without host work."""
import threading

import pytest

from optimus.worker_status import WorkerStatus


def test_independent_workers_keep_counts_times_and_terminal_clock():
    now = [10.0]
    status = WorkerStatus(clock=lambda: now[0])
    cloud = status.begin('cloud.drive', 'Cloud drive', status='scanning')
    browser = status.begin('browser.privacy', 'Browser privacy', status='waiting')
    status.update('cloud.drive', cloud, current=2, total=5, processes=['Cloud metadata query'])
    now[0] = 16.0
    status.update('cloud.drive', cloud, status='completed', description='Two explicitly approved items returned results.')
    now[0] = 40.0
    rows = {row['id']: row for row in status.snapshot()}
    assert rows['cloud.drive']['elapsed_seconds'] == 6
    assert rows['browser.privacy']['elapsed_seconds'] == 30
    assert rows['cloud.drive']['current'] == 2 and rows['cloud.drive']['total'] == 5
    assert rows['browser.privacy']['current'] is rows['browser.privacy']['total'] is None
    assert rows['cloud.drive']['terminal'] and not rows['browser.privacy']['terminal']
    assert status.cancellation('browser.privacy', browser).is_set() is False


def test_cancel_does_not_fabricate_finished_state_and_keeps_active_owner():
    status = WorkerStatus()
    token = status.begin('apps.sleep', 'Background apps')
    assert status.request_stop() == ['apps.sleep']
    row = status.snapshot()[0]
    assert row['status'] == 'stopping' and not row['terminal']
    assert status.cancellation('apps.sleep', token).is_set()
    with pytest.raises(RuntimeError):
        status.begin('apps.sleep', 'Background apps')
    status.update('apps.sleep', token, status='cancelled')
    replacement = status.begin('apps.sleep', 'Background apps')
    with pytest.raises(ValueError):
        status.update('apps.sleep', token, status='completed')
    assert not status.cancellation('apps.sleep', replacement).is_set()


def test_history_evicts_only_finished_workers_and_snapshots_are_copies():
    status = WorkerStatus(limit=4)
    for number in range(4):
        status.begin('worker.' + str(number), 'Worker')
    with pytest.raises(RuntimeError):
        status.begin('worker.4', 'Worker')
    token = status._jobs['worker.1']['token']
    status.update('worker.1', token, status='completed')
    status.begin('worker.4', 'Worker')
    rows = status.snapshot()
    assert {row['id'] for row in rows} == {'worker.0', 'worker.2', 'worker.3', 'worker.4'}
    rows[0]['description'] = 'untrusted edit'
    rows[0]['processes'].append('injected')
    assert status.snapshot()[0]['description'] != 'untrusted edit'
    assert status.snapshot()[0]['processes'] == []


def test_context_never_promotes_unverified_return_or_exception_to_success():
    status = WorkerStatus()
    with status.operation('cloud.scan', 'Cloud scan'):
        pass
    assert status.snapshot()[0]['status'] == 'needs_review'
    with pytest.raises(OSError):
        with status.operation('cloud.scan', 'Cloud scan'):
            raise OSError('private error')
    assert status.snapshot()[0]['status'] == 'needs_review'
    with status.operation('cloud.scan', 'Cloud scan') as (token, _):
        status.update('cloud.scan', token, status='completed', description='Verified native result.')
    assert status.snapshot()[0]['status'] == 'completed'


@pytest.mark.parametrize('counts', [(True, 1), (1, True), (-1, 1), (2, 1), (0, None)])
def test_unknown_or_invalid_progress_is_not_converted_to_a_percentage(counts):
    status = WorkerStatus()
    token = status.begin('tool.scan', 'Scan')
    with pytest.raises(ValueError):
        status.update('tool.scan', token, current=counts[0], total=counts[1])


def test_status_updates_from_independent_threads_do_not_lose_jobs():
    status = WorkerStatus()
    def work(number):
        identity = 'tool.' + str(number)
        token = status.begin(identity, 'Tool ' + str(number))
        for current in range(11):
            status.update(identity, token, status='scanning', current=current, total=10)
        status.update(identity, token, status='completed')
    threads = [threading.Thread(target=work, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(status.snapshot()) == 8
    assert all(row['terminal'] and row['current'] == 10 for row in status.snapshot())
