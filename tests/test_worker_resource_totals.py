"""Resource accounting uses fixture observations, never host maintenance."""
import copy
import json
from types import SimpleNamespace

import pytest

from optimus.parallel_updates import (ProgressMux, ParallelAppUpdates, WINDOWS_SOFTWARE,
                                      _journal_result, update_completion_state)
from optimus.worker_totals import (WorkerResourceTotals, MAX_BYTES, normalize_resource_totals,
                                   resource_totals_from_smart_actions)


def progress(key='software:update:Fixture.One', done=40, total=100, *, estimated=False, source='winget_native'):
    return {'key': key, 'label': 'Fixture', 'phase': 'download', 'percent': 40,
            'downloaded_bytes': done, 'download_total_bytes': total,
            'bytes_estimated': estimated, 'bytes_source': source}


def receipt(identity='Fixture.One', *, status='completed', **changes):
    outcome = {'exitCode': 0, 'installer_started': True, 'outcomeKnown': True, 'running': False, 'verified': True}
    outcome.update(changes)
    return {'id': identity, 'status': status, 'result': outcome}


def test_unknown_is_distinct_from_measured_zero():
    ledger = WorkerResourceTotals()
    assert ledger.snapshot()['removed_known'] is False
    ledger.removal_receipt('cleanup:zero', {'deleted_bytes': 0, 'deleted_count': 1})
    assert ledger.snapshot()['removed_bytes'] == 0
    assert ledger.snapshot()['removed_files'] == 1
    assert ledger.snapshot()['removed_known'] is True


def test_update_payload_removals_are_actual_and_deduplicated_by_attempt():
    ledger = WorkerResourceTotals('software_updates')
    cleaned = {'schema': 1, 'kind': 'update_payload_cleanup',
        'workspace_id': 'a' * 32, 'ownership': 'optimus_private_update_workspace',
        'workspace_owned': True, 'status': 'partial', 'removed_files': 2, 'removed_bytes': 300}
    row = receipt(payload_cleanup=cleaned)
    ledger.receipt(row)
    ledger.receipt(copy.deepcopy(row))
    ledger.result({'items': [row]})
    assert ledger.snapshot()['removed_bytes'] == 300
    assert ledger.snapshot()['removed_files'] == 2
    assert ledger.snapshot()['removed_known'] is True
    row['result']['payload_cleanup'] = dict(cleaned, workspace_id='b' * 32, status='completed',
        removed_bytes=100, removed_files=1)
    ledger.receipt(row)
    assert ledger.snapshot()['removed_bytes'] == 400


@pytest.mark.parametrize('patch', [dict(workspace_owned=False), dict(status='preserved'),
    dict(ownership='operating_system'), dict(workspace_id='../personal'),
    dict(removed_bytes=True), dict(removed_files=-1), dict(kind='preview')])
def test_unsupported_or_unverified_payload_receipts_never_count_as_removed(patch):
    ledger = WorkerResourceTotals('driver_updates')
    cleaned = dict(schema=1, kind='update_payload_cleanup', workspace_id='a' * 32,
        ownership='optimus_private_update_workspace', workspace_owned=True,
        status='completed', removed_bytes=300, removed_files=2)
    cleaned.update(patch)
    ledger.receipt(receipt(payload_cleanup=cleaned))
    assert ledger.snapshot()['removed_known'] is False


def test_live_and_terminal_cleanup_counts_merge_once_per_plan():
    ledger = WorkerResourceTotals()
    event = {'item_progress': {'key': 'cleanup:first', 'phase': 'cleanup'},
             'deleted_bytes': 100, 'deleted_count': 2}
    ledger.observe(event)
    ledger.observe(copy.deepcopy(event))
    ledger.removal_receipt('cleanup:first', {'deleted_bytes': 100, 'deleted_count': 2})
    ledger.removal_receipt('cleanup:first', {'deleted_bytes': 0, 'deleted_count': 0})
    ledger.removal_receipt('cleanup:next', {'deleted_bytes': 50, 'deleted_count': 1})
    totals = ledger.snapshot()
    assert (totals['removed_bytes'], totals['removed_files']) == (150, 3)


@pytest.mark.parametrize('invalid', [True, False, -1, 1.5, None, '1', MAX_BYTES + 1])
def test_invalid_removed_and_downloaded_bytes_remain_unknown(invalid):
    ledger = WorkerResourceTotals('software_updates')
    ledger.removal_receipt('cleanup:test', {'deleted_bytes': invalid, 'deleted_count': 1})
    ledger.observe({'item_progress': progress(done=invalid)})
    assert ledger.snapshot()['removed_known'] is False
    assert ledger.snapshot()['downloaded_known'] is False


def test_previews_catalog_recycle_and_sqlite_counts_never_become_removed_bytes():
    ledger = WorkerResourceTotals()
    ledger.observe({'preview_summary': {'bytes': 999}, 'total_bytes': 999,
                    'recycled_bytes': 999, 'verified_bytes': 999, 'counts': {'history': 100}})
    ledger.removal_receipt('recycle:one', {'deleted_bytes': 999, 'deleted_count': 1})
    assert ledger.snapshot()['removed_known'] is False


def test_download_highwater_and_terminal_receipt_do_not_double_count():
    ledger = WorkerResourceTotals('software_updates')
    ledger.observe({'item_progress': progress(done=40)})
    ledger.observe({'item_progress': progress(done=40)})
    ledger.observe({'item_progress': progress(done=30)})
    ledger.observe({'item_progress': progress(done=100)})
    row = receipt(download_progress=progress(done=100))
    ledger.receipt(row)
    ledger.result({'items': [row]})
    totals = ledger.snapshot()
    assert totals['downloaded_bytes'] == 100
    assert totals['installed_items'] == 1
    assert totals['downloaded_partial'] is False


def test_each_package_has_separate_download_counter_and_later_failure_keeps_success():
    ledger = WorkerResourceTotals('software_updates')
    ledger.observe({'item_progress': progress(done=100)})
    ledger.receipt(receipt())
    ledger.observe({'item_progress': progress('software:update:Fixture.Two', done=20)})
    ledger.receipt(receipt('Fixture.Two', status='failed', exitCode=2))
    assert ledger.snapshot()['downloaded_bytes'] == 120
    assert ledger.snapshot()['installed_items'] == 1
    assert ledger.snapshot()['downloaded_partial'] is True


def test_partial_and_rounded_downloads_stay_labeled():
    ledger = WorkerResourceTotals('software_updates')
    ledger.observe({'item_progress': progress(done=20, estimated=True)})
    assert ledger.snapshot()['downloaded_estimated'] is True
    assert ledger.snapshot()['downloaded_partial'] is True
    ledger.observe({'item_progress': progress(done=100)})
    assert ledger.snapshot()['downloaded_estimated'] is True
    assert ledger.snapshot()['downloaded_partial'] is False


@pytest.mark.parametrize('change', [{'phase': 'install'}, {'bytes_source': 'catalog'},
    {'download_total_bytes': 1}, {'bytes_estimated': None}, {'download_total_bytes': True}])
def test_nontransfer_and_malformed_download_observations_ignored(change):
    ledger = WorkerResourceTotals('driver_updates')
    ledger.observe({'item_progress': dict(progress(), **change)})
    assert ledger.snapshot()['downloaded_known'] is False


def test_percent_and_catalog_size_without_native_counter_are_not_bytes():
    ledger = WorkerResourceTotals('driver_updates')
    ledger.observe({'item_progress': {'key': 'driver:one', 'label': 'Fixture', 'phase': 'download',
        'percent': 100}, 'MinDownloadSize': 100, 'MaxDownloadSize': 100, 'IsDownloaded': True})
    assert ledger.snapshot()['downloaded_known'] is False


@pytest.mark.parametrize('change', [{'outcomeKnown': False}, {'running': True},
    {'installer_started': False}, {'exitCode': True}, {'exitCode': 2},
    {'requiresReview': True}, {'verified': False}, {'cancelled': True}, {'uncertain': True}])
def test_success_label_alone_never_counts_install(change):
    ledger = WorkerResourceTotals('software_updates')
    ledger.receipt(receipt(**change))
    assert ledger.snapshot()['installed_items'] == 0


def test_unknown_legacy_receipt_does_not_claim_zero_known_installs():
    ledger = WorkerResourceTotals('software_updates')
    ledger.receipt({'id': 'Fixture.One', 'status': 'completed', 'result': {'exitCode': 0}})
    assert ledger.snapshot()['installed_known'] is False
    assert ledger.snapshot()['installed_items'] == 0


def test_explicit_provider_completion_provenance_does_not_claim_version_readback():
    ledger = WorkerResourceTotals('software_updates')
    row = receipt(installation_reported=True)
    del row['result']['verified']
    saved = _journal_result({'items': [row]})
    ledger.result(saved)
    assert ledger.snapshot()['installed_items'] == 1
    assert ledger.snapshot()['installed_reported'] is True


def test_installed_success_without_download_bytes_stays_unknown_size():
    ledger = WorkerResourceTotals('software_updates')
    ledger.receipt(receipt())
    assert ledger.snapshot()['installed_items'] == 1
    assert ledger.snapshot()['downloaded_known'] is False
    assert ledger.snapshot()['downloaded_partial'] is True


def test_nested_presence_is_counted_once_but_uninstalls_and_satisfied_are_excluded():
    ledger = WorkerResourceTotals('software_updates')
    install = dict(receipt('Fixture.Install', verified=True), presence='keep_installed')
    uninstall = dict(receipt('Fixture.Remove', verified=True), presence='keep_uninstalled')
    satisfied = dict(receipt('Fixture.Satisfied', status='already_satisfied', installer_started=False),
                     presence='keep_installed')
    batch = {'items': [receipt()], 'app_presence': {'items': [install, uninstall, satisfied]}}
    ledger.result(batch)
    ledger.result(copy.deepcopy(batch))
    assert ledger.snapshot()['installed_items'] == 2


@pytest.mark.parametrize('revision', [3, 2147483647])
def test_driver_install_requires_all_native_success_receipts(revision):
    ledger = WorkerResourceTotals('driver_updates')
    row = {'binding': {'id': 'fixture-guid', 'revision': revision}, 'status': 'applied', 'result': {
        'installer_started': True, 'outcomeKnown': True, 'running': False,
        'resultCode': 2, 'hresult': 0, 'aggregateResultCode': 2, 'aggregateHresult': 0}}
    bad = copy.deepcopy(row)
    bad['binding']['id'] = 'bad-guid'
    del bad['result']['aggregateHresult']
    ledger.receipt(row)
    ledger.receipt(row)
    ledger.receipt(bad)
    assert ledger.snapshot()['installed_items'] == 1


def test_mismatched_terminal_download_binding_is_ignored():
    ledger = WorkerResourceTotals('software_updates')
    ledger.receipt(receipt(download_progress=progress('software:update:Wrong', done=100)))
    assert ledger.snapshot()['downloaded_known'] is False


def test_receipt_bytes_persist_in_compact_journal_and_reaggregate_once():
    row = receipt(downloaded_bytes=100, download_total_bytes=100,
                  bytes_estimated=True, bytes_source='winget_native')
    saved = _journal_result({'items': [row]})
    assert saved['items'][0]['result']['downloaded_bytes'] == 100
    ledger = WorkerResourceTotals('software_updates')
    ledger.result(saved)
    ledger.result(saved)
    assert ledger.snapshot()['downloaded_bytes'] == 100
    assert ledger.snapshot()['installed_items'] == 1


def test_progress_mux_keeps_maintenance_and_workers_separate_and_snapshots_independent():
    events = []
    mux = ProgressMux(events.append)
    mux({'stage': 'Clean temporary files', 'item_progress': {'key': 'cleanup:a', 'phase': 'cleanup'},
         'deleted_bytes': 50, 'deleted_count': 1})
    mux.background('installing', {'item_progress': progress(done=100)})
    mux.for_job('software_updates').item_receipt(receipt())
    mux.heartbeat()
    latest = events[-1]
    assert latest['resource_totals']['removed_bytes'] == 50
    assert latest['resource_totals']['downloaded_known'] is False
    assert latest['jobs'][0]['resource_totals']['downloaded_bytes'] == 100
    assert latest['jobs'][0]['resource_totals']['installed_items'] == 1
    latest['jobs'][0]['resource_totals']['downloaded_bytes'] = 999
    mux.heartbeat()
    assert events[-1]['jobs'][0]['resource_totals']['downloaded_bytes'] == 100


def test_failed_terminal_journal_write_never_updates_display_count(tmp_path, monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    mux = ProgressMux(lambda _: None)
    job = ParallelAppUpdates(SimpleNamespace(data_dir=tmp_path), 'a' * 32,
                             WINDOWS_SOFTWARE, lambda *_: None, mux)
    def refuse_write():
        raise OSError('Fixture journal unavailable')
    monkeypatch.setattr(job, '_write', refuse_write)
    with pytest.raises(OSError):
        job.item_receipt(receipt())
    assert mux.job_resource_snapshot()['installed_items'] == 0


def test_terminal_journal_precedes_display_accounting(tmp_path, monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    mux = ProgressMux(lambda _: None)
    job = ParallelAppUpdates(SimpleNamespace(data_dir=tmp_path), 'a' * 32,
                             WINDOWS_SOFTWARE, lambda *_: None, mux)
    observed = []
    def written_first():
        observed.append(mux.job_resource_snapshot()['installed_items'])
    monkeypatch.setattr(job, '_write', written_first)
    job.item_receipt(receipt())
    assert observed == [0]
    assert mux.job_resource_snapshot()['installed_items'] == 1


@pytest.mark.parametrize('facts', ['unknown', 'installed', 'zero_download'])
def test_worker_enriches_only_measured_facts_without_mutating_backend_receipts(tmp_path, monkeypatch, facts):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)
    outcome = {'items': [], 'review_only': True}
    if facts == 'installed':
        outcome = {'items': [receipt()]}
    elif facts == 'zero_download':
        row = receipt(status='failed', exitCode=1, verified=False)
        row['result']['download_progress'] = progress(done=0)
        outcome = {'items': [row], 'failed_items': 1}
    original = copy.deepcopy(outcome)
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', bridge=SimpleNamespace(_process=None))
    job = ParallelAppUpdates(backend, 'c' * 32, WINDOWS_SOFTWARE,
                             lambda *_: outcome, ProgressMux(lambda _: None))
    job.start(inline=True)
    job._run()
    result = job.join()['result']
    assert outcome == original
    saved = json.loads(job.path.read_text())['result']
    if facts == 'unknown':
        assert result == original and result is outcome
        assert 'resource_totals' not in saved
    else:
        assert result is not outcome
        assert result['resource_totals'] == saved['resource_totals']
        if facts == 'installed':
            assert result['resource_totals']['installed_items'] == 1
        else:
            assert result['resource_totals']['downloaded_known'] is True
            assert result['resource_totals']['downloaded_bytes'] == 0


def test_snapshot_validation_rejects_malformed_values():
    snapshot = WorkerResourceTotals().snapshot()
    assert normalize_resource_totals(snapshot) == snapshot
    assert normalize_resource_totals(dict(snapshot, downloaded_bytes=True)) is None
    assert normalize_resource_totals(dict(snapshot, removed_known=1)) is None


def test_completion_state_retains_sanitized_totals_for_saved_outcomes():
    ledger = WorkerResourceTotals('software_updates')
    ledger.receipt(receipt())
    state = update_completion_state({'items': [], 'resource_totals': ledger.snapshot()})
    assert state['resource_totals'] == ledger.snapshot()
    assert 'resource_totals' not in update_completion_state({'items': [], 'resource_totals': {'installed_items': 3}})


def test_multiple_observations_cannot_overflow_the_snapshot_bound():
    ledger = WorkerResourceTotals('software_updates')
    ledger.observe({'item_progress': progress(done=MAX_BYTES, total=MAX_BYTES)})
    ledger.observe({'item_progress': progress('software:update:TooMuch', done=1, total=1)})
    ledger.removal_receipt('cleanup:first', {'deleted_bytes': MAX_BYTES, 'deleted_count': 1})
    ledger.removal_receipt('cleanup:overflow', {'deleted_bytes': 1, 'deleted_count': 1})
    assert ledger.snapshot()['downloaded_bytes'] == MAX_BYTES
    assert ledger.snapshot()['removed_bytes'] == MAX_BYTES
    assert normalize_resource_totals(ledger.snapshot()) is not None


def test_smart_actions_count_only_receipted_removals_once_and_ignore_database_rows():
    junk = {'receipt': 'fixture-junk-receipt', 'status': 'completed_with_errors',
            'result': {'deleted_bytes': 20, 'deleted_count': 1, 'skipped_count': 1}}
    browser = {'receipt': 'fixture-browser-receipt', 'status': 'completed', 'result': {'items': [
        {'profiles': [{'counts': {'cookies': 900, 'history': 2000}}],
         'cache': {'deleted_bytes': 30, 'deleted_count': 2}}]}}
    totals = resource_totals_from_smart_actions({'actions': [junk, browser, copy.deepcopy(junk)]})
    assert (totals['removed_bytes'], totals['removed_files']) == (50, 3)
    assert totals['removed_known'] is True
    assert resource_totals_from_smart_actions({'preview': True, 'actions': [junk]})['removed_known'] is False
    assert resource_totals_from_smart_actions({'actions': [dict(junk, status='uncertain')]})['removed_known'] is False
    assert resource_totals_from_smart_actions({'actions': [dict(junk,
        result=dict(junk['result'], uncertain=True))]})['removed_known'] is False
    uncertain_browser=copy.deepcopy(browser)
    uncertain_browser['result']['items'][0]['cache']['uncertain']=True
    assert resource_totals_from_smart_actions({'actions': [uncertain_browser]})['removed_known'] is False


def test_workflow_cleanup_reports_actual_bytes_not_preview(tmp_path):
    from test_workflow import MockBackend, workflow_module
    backend = MockBackend(tmp_path)
    def scan(categories, age, *, progress_callback=None):
        return {'id': 'fixture-plan', 'files': [{'path': 'fixture'}], 'total_bytes': 99999,
                'complete': True, 'skipped': []}
    def apply(plan, *, progress_callback=None):
        progress_callback({'item_progress': {'key': 'cleanup:fixture-plan', 'label': 'Fixture files',
            'phase': 'cleanup', 'percent': 50}, 'deleted_bytes': 25, 'deleted_count': 1})
        return {'deleted_count': 1, 'deleted_bytes': 25, 'skipped_count': 1}
    backend.cleanup_scan, backend.cleanup_apply = scan, apply
    settings = dict(workflow_module.DEFAULTS, browser_cache_enabled=False)
    events = []
    report = workflow_module.Workflow(backend).run(settings, events.append)
    assert report['resource_totals']['removed_bytes'] == 25
    assert report['resource_totals']['removed_files'] == 1
    assert events[-1]['resource_totals'] == report['resource_totals']
    stored = json.loads((tmp_path / 'runs' / (report['id'] + '.json')).read_text())
    assert stored['resource_totals'] == report['resource_totals']


def test_native_workflow_retains_same_confirmed_cleanup_totals(tmp_path):
    from test_workflow import MockBackend, workflow_module
    from optimus.native_workflow import NativeWorkflow
    backend = MockBackend(tmp_path)
    backend.system = 'linux'
    backend.preference_store = SimpleNamespace(validate=workflow_module.Preferences.validate)
    backend.cleanup_categories = lambda: [{'id': 'user-temp', 'available': True}]
    backend.cleanup_scan = lambda *_: {'id': 'native-plan', 'complete': True, 'files': [], 'total_bytes': 99999}
    backend.cleanup_apply = lambda _: {'deleted_count': 2, 'deleted_bytes': 20, 'errors': []}
    settings = dict(workflow_module.DEFAULTS, browser_cache_enabled=False, cleanup_category_ids=['user-temp'])
    events = []
    report = NativeWorkflow(backend).run(settings, events.append)
    assert report['resource_totals']['removed_bytes'] == 20
    assert report['resource_totals']['removed_files'] == 2
    assert events[-1]['resource_totals'] == report['resource_totals']
