"""New readback/device retry metadata survives the bounded worker journal."""
import pytest

from optimus.parallel_updates import _journal_result


@pytest.mark.parametrize('attempts', [1, 2, 3])
@pytest.mark.parametrize('verified', [False, True])
def test_readback_evidence_survives_worker_receipt(attempts, verified):
    saved = _journal_result({'items': [{'id': 'Fixture.App', 'status': 'completed',
        'result': {'verification_attempts': attempts, 'verified_after_readback': verified}}]})
    assert saved['items'][0]['result'] == {
        'verification_attempts': attempts, 'verified_after_readback': verified}


@pytest.mark.parametrize('attempts', [True, False, -1, 0, 4, '2', None])
def test_invalid_readback_counter_not_serialized(attempts):
    saved = _journal_result({'items': [{'result': {'verification_attempts': attempts,
        'verified_after_readback': 'yes'}}]})
    assert 'verification_attempts' not in saved['items'][0]['result']
    assert 'verified_after_readback' not in saved['items'][0]['result']


def test_matching_device_retry_is_retained_without_authorizing_it():
    outcome = {'failureKind': 'present_device_unmatched', 'retry_when': 'matching_device_present',
        'automatic_recheck': True, 'installer_started': False, 'outcomeKnown': True, 'running': False}
    saved = _journal_result({'items': [{'status': 'skipped', 'result': outcome}]})
    assert saved['items'][0]['result'] == outcome


@pytest.mark.parametrize('patch', [
    {'failureKind': 'unknown'}, {'retry_when': 'any_time'}, {'automatic_recheck': 'yes'},
])
def test_retry_metadata_requires_exact_typed_device_reason(patch):
    outcome = dict(failureKind='present_device_unmatched', retry_when='matching_device_present',
        automatic_recheck=True, **{})
    outcome.update(patch)
    saved = _journal_result({'items': [{'result': outcome}]})
    assert 'retry_when' not in saved['items'][0]['result']
    assert 'automatic_recheck' not in saved['items'][0]['result']
