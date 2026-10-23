"""Unified undo uses only injected recovery records and disposable journals."""
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import change_history as module


class Lease:
    def __init__(self, root):
        self.root, self.released = root, False
    def acquire(self):
        return self
    def release(self):
        self.released = True


def change(letter='a', **kwargs):
    return dict({'Id':letter * 32, 'Kind':'Startup', 'Target':'Fixture startup registration',
                 'Status':'Applied', 'created_at':1, 'Undo':{'private':'DO NOT EXPORT THIS VALUE'}}, **kwargs)


@pytest.fixture
def service(tmp_path, monkeypatch):
    from optimus import parallel_updates
    monkeypatch.setattr(parallel_updates, 'pending_updates_barrier', Mock(return_value=None))
    backend = SimpleNamespace(data_dir=tmp_path, records=[change()])
    backend.changes = Mock(side_effect=lambda:copy.deepcopy(backend.records))
    def restore(record):
        next(row for row in backend.records if row['Id'] == record['Id'])['Status'] = 'Restored'
        return {'restored':True}
    backend.restore = Mock(side_effect=restore)
    value = module.ChangeHistory(backend, owner='fixture-account', lease_factory=Lease)
    return value, backend


def select(value):
    plan = value.scan()
    return plan, [row['selection_id'] for row in plan['items'] if row['undo_available']]


def test_constructor_does_not_resolve_owner_or_create_private_state(tmp_path, monkeypatch):
    owner = Mock(side_effect=AssertionError('No native identity call during construction'))
    monkeypatch.setattr(module, '_current_owner', owner)
    module.ChangeHistory(SimpleNamespace(data_dir=tmp_path))
    assert not list(tmp_path.iterdir())
    owner.assert_not_called()


def test_scan_returns_opaque_bounded_metadata_and_visible_irreversible_history(service):
    value, backend = service
    folder = backend.data_dir / 'runs'; folder.mkdir()
    (folder / ('c'*32 + '.json')).write_text(json.dumps({'id':'c'*32, 'created_at':2,
        'status':'completed', 'items':[{'secret':'private browser/registry details'}]}))
    plan, selected = select(value)
    assert len(selected) == 1 and len(plan['items']) == 2
    assert plan['items'][0]['provider'] == 'history:runs' and not plan['items'][0]['undo_available']
    assert 'Undo' not in json.dumps(plan) and 'DO NOT EXPORT' not in json.dumps(plan)
    assert 'private browser' not in json.dumps(plan) and 'secret' not in json.dumps(plan)
    assert not (backend.data_dir / 'change-history').exists()
    backend.restore.assert_not_called()


@pytest.mark.parametrize('policy', [dict(confirmed=False), dict(confirmed=True, selection_ids=[]),
    dict(confirmed=True, selection_ids=['bad']), dict(confirmed=True, plan_id='b'*32)])
def test_confirmation_and_exact_plan_are_required_before_native_dispatch(service, policy):
    value, backend = service
    plan, identities = select(value)
    kwargs = dict(plan_id=plan['plan_id'], selection_ids=identities, confirmed=True)
    kwargs.update(policy)
    with pytest.raises(ValueError):
        value.revert(**kwargs)
    backend.restore.assert_not_called()


def test_newest_first_revalidation_and_no_batch_replay(service):
    value, backend = service
    backend.records.append(change('b', created_at=5))
    plan, identities = select(value)
    report = value.revert(plan['plan_id'], list(reversed(identities)), confirmed=True)
    assert report['status'] == 'completed' and report['restored'] == 2
    assert [call.args[0]['Id'] for call in backend.restore.call_args_list] == ['b'*32, 'a'*32]
    with pytest.raises(ValueError):
        value.revert(plan['plan_id'], identities, confirmed=True)
    assert not value.pending_barrier()
    saved = json.loads(next(value.directory.glob('*.json')).read_text())
    assert saved['schema'] == 1 and saved['owner'] == 'fixture-account'
    assert 'DO NOT EXPORT' not in json.dumps(saved) and 'Undo' not in json.dumps(saved)


def test_changed_record_and_expired_or_wrong_owner_plan_never_restore(service):
    value, backend = service
    plan, identities = select(value)
    backend.records[0]['Undo']['private'] = 'later different saved original'
    with pytest.raises(ValueError, match='changed'):
        value.revert(plan['plan_id'], identities, confirmed=True)
    plan, identities = select(value)
    value.owner = 'different-account'
    with pytest.raises(ValueError, match='another session'):
        value.revert(plan['plan_id'], identities, confirmed=True)
    value.owner = 'fixture-account'
    value.clock = lambda:float('inf')
    with pytest.raises(ValueError, match='expired'):
        value.revert(plan['plan_id'], identities, confirmed=True)
    backend.restore.assert_not_called()


@pytest.mark.parametrize('result', [None, {}, {'status':'running'}, {'status':'uncertain'},
                                    {'status':'completed', 'outcomeKnown':False}])
def test_unknown_outcome_stops_remaining_records_and_retains_barrier(service, result):
    value, backend = service
    backend.records.append(change('b', created_at=2))
    backend.restore.side_effect = None; backend.restore.return_value = result
    plan, identities = select(value)
    report = value.revert(plan['plan_id'], identities, confirmed=True)
    assert report['status'] == 'uncertain' and report['restored'] == 0 and report['not_processed'] == 1
    assert backend.restore.call_count == 1 and value.pending_barrier()
    second, selected = select(value)
    with pytest.raises(RuntimeError, match='unresolved'):
        value.revert(second['plan_id'], selected, confirmed=True)
    assert backend.restore.call_count == 1


def test_thrown_native_exception_is_unknown_and_never_blindly_retried(service):
    value, backend = service
    backend.restore.side_effect = OSError('fixture lost native receipt')
    plan, identities = select(value)
    result = value.revert(plan['plan_id'], identities, confirmed=True)
    assert result['status'] == 'uncertain' and value.pending_barrier()
    assert 'fixture lost native receipt' not in json.dumps(result)


def test_cancel_after_first_restore_never_starts_next_record(service):
    value, backend = service
    backend.records.append(change('b', created_at=2))
    stop = threading.Event()
    def restored(_):
        stop.set()
        return {'restored':True}
    backend.restore.side_effect = restored
    plan, identities = select(value)
    result = value.revert(plan['plan_id'], identities, confirmed=True, cancel_event=stop)
    assert result['status'] == 'cancelled' and result['restored'] == 1 and result['not_processed'] == 1
    assert backend.restore.call_count == 1 and not value.pending_barrier()


def test_cancellation_from_progress_prevents_first_restore(service):
    value, backend = service
    stop = threading.Event()
    plan, identities = select(value)
    result = value.revert(plan['plan_id'], identities, confirmed=True, cancel_event=stop,
                          progress_callback=lambda _:stop.set())
    assert result['status'] == 'cancelled' and not value.pending_barrier()
    backend.restore.assert_not_called()


def test_journal_final_save_failure_preserves_uncertain_intent(service, monkeypatch):
    value, backend = service
    original = value._write
    def fail_terminal(record):
        if record['status'] == 'completed':
            raise OSError('fixture durable final write unavailable')
        return original(record)
    monkeypatch.setattr(value, '_write', fail_terminal)
    plan, identities = select(value)
    with pytest.raises(OSError):
        value.revert(plan['plan_id'], identities, confirmed=True)
    assert backend.restore.call_count == 1 and value.pending_barrier()


def test_private_history_schema_and_owner_mismatch_fail_closed(service):
    value, _ = service
    value.directory.mkdir()
    path = value.directory / ('c'*32 + '.json')
    path.write_text(json.dumps({'id':'c'*32,'kind':'change-history','schema':1,'owner':'wrong-account',
                                'status':'completed','items':[]}))
    assert value.pending_barrier() and value.scan()['complete'] is False


def test_available_provider_shapes_match_real_browser_privacy_power_and_native_records(service):
    value, backend = service
    backend.records = [dict(change(), source='python-allocation', Id='a'*64, Kind='Priority'),
        {'Id':'b'*32,'source':'native-startup','Name':'Fixture autostart','state':'applied',
         'created_at':3,'restored':False}]
    backend.system_privacy_history = Mock(return_value={'complete':True, 'items':[
        {'id':'c'*32,'kind':'privacy','status':'completed','changed_count':2,'created_at':4}]})
    backend.restore_system_privacy = Mock(return_value={'status':'completed','failed_items':0,'items':[{'status':'restored'}]})
    backend.speed_enhancements_history = Mock(return_value=[{'id':'d'*32,'status':'completed',
                                                           'restore_available':True,'created_at':5}])
    raw = SimpleNamespace(restore=Mock(return_value={'status':'restored'}))
    backend._speed_enhancements_service = Mock(return_value=raw)
    backend.privacy_backups = Mock(return_value=[{'backupId':'e'*32,'state':'partial','browser':'chrome',
        'profileName':'Fixture profile','createdUtc':'2026-10-20T00:00:00Z'}, {'backupId':'f'*32,'state':'failed'}])
    backend.privacy_restore = Mock(return_value={'state':'restored'})
    plan, identities = select(value)
    assert len(identities) == 5
    failed = next(row for row in plan['items'] if row['record_id'] == 'f'*32)
    assert failed['undo_available'] is False
    # Use only browser/privacy/power here; native records are independently
    # revalidated by their original restorers, not synthetic Undo arguments.
    identities = [row['selection_id'] for row in plan['items'] if row['provider'] in ('browser','privacy','speed') and row['undo_available']]
    result = value.revert(plan['plan_id'], identities, confirmed=True)
    assert result['status'] == 'completed' and result['restored'] == 3
    backend.privacy_restore.assert_called_once_with('e'*32, confirmed=True)
    raw.restore.assert_called_once()
    assert value.scan()['undo_available'] == 2  # completed receipts suppress replayable privacy summaries


def test_already_restored_and_unsupported_records_are_not_selectable(service):
    value, backend = service
    backend.records += [change('b', Kind='ProcessClose', Status='Logged'), change('c', Status='Restored'),
                         change('d', Status='Failed')]
    plan, identities = select(value)
    assert len(identities) == 1 and sum(not row['undo_available'] for row in plan['items']) == 3
    unsupported = next(row for row in plan['items'] if row['record_id'] == 'b'*32)
    with pytest.raises(ValueError):
        value.revert(plan['plan_id'], [unsupported['selection_id']], confirmed=True)
    backend.restore.assert_not_called()


def test_plan_memory_is_bounded_and_expired_previews_are_evicted(service):
    value, _ = service
    for _ in range(20):
        value.scan()
    assert len(value._plans) == 8
    value.clock = lambda:1e12
    value.scan()
    assert len(value._plans) == 1


def test_ordinary_path_guard_rejects_linked_private_history(service, monkeypatch):
    value, backend = service
    from types import SimpleNamespace
    original = module._ordinary_data_path
    def linked(path, **kwargs):
        if path == value.directory:
            raise ValueError('fixture reparse directory')
        return original(path, **kwargs)
    monkeypatch.setattr(module, '_ordinary_data_path', linked)
    assert value.pending_barrier()
    backend.restore.assert_not_called()


def test_large_normal_workflow_receipt_is_bounded_and_later_receipts_are_still_indexed(service):
    value, backend = service
    folder = backend.data_dir / 'runs'; folder.mkdir()
    (folder / ('b'*32 + '.json')).write_text(json.dumps({'id':'b'*32,'status':'completed',
        'created_at':3,'large_private_payload':'x' * (3 * 1024 * 1024)}))
    (folder / ('c'*32 + '.json')).write_text(json.dumps({'id':'c'*32,'status':'completed','created_at':2}))
    result = value.scan()
    assert result['complete'] is True and len(result['items']) == 3
    assert 'large_private_payload' not in json.dumps(result)
    assert result['undo_available'] == 1


def test_oversized_or_invalid_history_is_visible_unverified_and_does_not_hide_later_receipts(service):
    value, backend = service
    folder = backend.data_dir / 'runs'; folder.mkdir()
    (folder / ('b'*32 + '.json')).write_text('x' * (8 * 1024 * 1024 + 1))
    (folder / ('c'*32 + '.json')).write_text(json.dumps({'id':'c'*32,'status':'completed'}))
    result = value.scan()
    oversized = next(row for row in result['items'] if row['record_id'] == 'b'*32)
    assert oversized['status'] == 'unverified' and oversized['undo_available'] is False
    assert result['complete'] is False and len(result['items']) == 3
    plan, identities = select(value)
    assert value.revert(plan['plan_id'], identities, confirmed=True)['restored'] == 1
