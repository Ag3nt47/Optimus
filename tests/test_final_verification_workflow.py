"""Final checks precede completion; recorded uncertainty is never an all-clear."""
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.workflow import DEFAULTS, FINAL_VERIFICATION_STAGE, Workflow
from test_workflow import MockBackend, mock_verification


def config(**changes):
    return dict(copy.deepcopy(DEFAULTS), cleanup_enabled=False, browser_cache_enabled=False, **changes)


def verification(status='needs_review', failed_items=0):
    return {'schema': 1, 'read_only': True, 'status': status,
            'counts': {'confirmed': 1, 'needs_review': int(status == 'needs_review'),
                       'unverified': int(status == 'unverified'), 'not_applicable': 0},
            'coverage_complete': True, 'failed_items': failed_items, 'stages': [],
            'summary': 'A remaining outcome requires review; no all-clear was inferred.'}


@pytest.mark.parametrize('process_review', [False, True])
def test_verification_is_last_and_durable_before_100_percent(tmp_path, process_review):
    service = MockBackend(tmp_path)
    trace = []
    service.audit = Mock(side_effect=lambda: trace.append('audit') or {})
    service.process_harness_scan = Mock(side_effect=lambda **kwargs: trace.append('process-review') or {'complete': True, 'actions': []})
    def verify(report, **kwargs):
        trace.append('verify')
        assert 'finished_at' not in report and 'final_verification' not in report
        titles = [row['title'] for row in report['stages']]
        assert titles[-1] == ('Prepare background-program recommendations' if process_review else 'Capture final resource report')
        assert FINAL_VERIFICATION_STAGE not in titles
        assert kwargs['timeout'] == 30
        return mock_verification(report)
    service.verify_optimization = Mock(side_effect=verify)
    def progress(event):
        if event['percentage'] == 100:
            trace.append('100-percent')
            stored = json.loads(next((tmp_path / 'runs').glob('*.json')).read_text())
            assert stored['stages'][-1]['title'] == FINAL_VERIFICATION_STAGE
            assert stored['final_verification'] == stored['stages'][-1]['result']
    result = Workflow(service).run(config(post_process_review_enabled=process_review), progress)
    assert trace[-2:] == ['verify', '100-percent']
    assert trace[:2] == ['audit', 'audit']
    assert result['stages'][-1]['title'] == FINAL_VERIFICATION_STAGE
    assert result['finished_at'] >= result['stages'][-1]['finished_at']
    assert json.loads(Path(result['log_path']).read_text())['final_verification'] == result['final_verification']


def test_prior_failure_is_reviewed_without_counting_it_twice(tmp_path):
    service = MockBackend(tmp_path)
    service.fail_cleanup_once = True
    service.verify_optimization = Mock(return_value=verification())
    result = Workflow(service).run(dict(config(), cleanup_enabled=True), lambda _: None)
    assert result['failed_stages'] == 1 and result['status'] == 'completed_with_errors'
    assert result['stages'][-1]['status'] == 'completed'
    assert result['final_verification']['status'] == 'needs_review'
    prior = service.verify_optimization.call_args.args[0]
    assert any(stage['status'] == 'failed' for stage in prior['stages'])


def test_real_postcondition_failure_fails_the_verification_stage(tmp_path):
    service = MockBackend(tmp_path)
    service.verify_optimization = Mock(return_value=verification(failed_items=1))
    result = Workflow(service).run(config(), lambda _: None)
    assert result['failed_stages'] == 1 and result['stages'][-1]['status'] == 'failed'
    assert result['final_verification']['status'] == 'needs_review'


def test_time_bounded_unknown_check_stays_visible_without_inventing_a_second_failure(tmp_path):
    service = MockBackend(tmp_path)
    value = dict(verification('unverified'), deadline_reached=True)
    service.verify_optimization = Mock(return_value=value)
    events = []
    result = Workflow(service).run(config(), events.append)
    assert result['failed_stages'] == 0 and result['status'] == 'completed'
    assert result['final_verification']['status'] == 'unverified'
    assert result['final_verification']['deadline_reached']
    assert 'requires review' in events[-1]['description']


@pytest.mark.parametrize('invalid', [None, {}, {'schema': 1, 'status': 'confirmed'},
    dict(verification(), read_only=False),
    dict(verification(), counts={'confirmed': True, 'needs_review': 0, 'unverified': 0, 'not_applicable': 0}),
    dict(verification(), status='confirmed')])
def test_invalid_final_report_becomes_explicit_unverified_failure(tmp_path, invalid):
    service = MockBackend(tmp_path)
    service.verify_optimization = Mock(return_value=invalid)
    result = Workflow(service).run(config(), lambda _: None)
    assert result['final_verification']['status'] == 'unverified'
    assert result['final_verification']['coverage_complete'] is False
    assert result['status'] == 'completed_with_errors'


def test_verifier_exception_does_not_omit_final_report_or_expose_exception_payload(tmp_path):
    service = MockBackend(tmp_path)
    service.verify_optimization = Mock(side_effect=RuntimeError('private fixture payload'))
    result = Workflow(service).run(config(), lambda _: None)
    assert result['final_verification']['status'] == 'unverified'
    assert 'RuntimeError' in str(result['final_verification']['errors'])
    assert 'private fixture payload' not in json.dumps(result)


def test_cancellation_during_final_check_cannot_emit_100_percent(tmp_path):
    service = MockBackend(tmp_path)
    event = threading.Event()
    def verify(report, **kwargs):
        assert kwargs['cancel_event'] is event
        event.set()
        return dict(verification('unverified'), cancelled=True)
    service.verify_optimization = Mock(side_effect=verify)
    events = []
    result = Workflow(service).run(config(), events.append, event)
    assert result['cancelled'] and result['status'] == 'cancelled'
    assert result['stages'][-1]['status'] == 'cancelled'
    assert max(item['percentage'] for item in events) < 100


def test_production_backend_verifier_consumes_workflow_evidence_without_host_access(tmp_path, monkeypatch):
    """Exercise the actual Backend/module boundary, with only fixture measurements."""
    from optimus.backend import Backend
    import optimus.verification as verifier_module

    native_reader = Mock(side_effect=AssertionError('Measurement-only verification must stay local to the fixture report.'))
    monkeypatch.setattr(verifier_module, 'local_snapshot', native_reader)
    service = MockBackend(tmp_path)
    service.audit = Mock(return_value={'Processes': [], 'System': {'fixture': True}, 'InventoryErrors': []})
    service.sampler = SimpleNamespace(status=Mock(side_effect=AssertionError('No sampler stage was selected.')))
    service.verify_optimization = Backend.verify_optimization.__get__(service, Backend)

    result = Workflow(service).run(config(), lambda _: None)

    assert result['status'] == 'completed'
    checked = result['final_verification']
    assert checked['run_id'] == result['id']
    assert checked['read_only'] is True and checked['status'] == 'confirmed'
    assert checked['coverage_complete'] is True and checked['failed_items'] == 0
    assert checked['counts'] == {'confirmed': 2, 'needs_review': 0, 'unverified': 0, 'not_applicable': 1}
    assert [row['title'] for row in checked['stages']] == [row['title'] for row in result['stages'][:-1]]
    native_reader.assert_not_called()
    service.sampler.status.assert_not_called()
