"""The recommendation queue advances only past bounded terminal receipts."""
import copy

import pytest

from optimus.recommendation_presentation import recommendation_continuation_reason


IDLE = {'outcomeKnown': True, 'running': False, 'may_continue': False}
WUA_SUCCESS = {'ok': True, 'resultCode': 2, 'itemResultCode': 2,
               'hresult': 0, 'aggregate_hresult': 0, 'reboot_required': False}


@pytest.mark.parametrize('result', [
    {'status': 'completed'},
    {'status': 'manual_action_required', 'opened': 'windowsdefender:'},
    {'status': 'review', 'review_only': True},
    {'status': 'partial', 'review_only': True, 'failed_items': 2},
    {'review_only': True, 'note': 'Native vendor guidance.'},
    dict(IDLE, status='failed', operation_started=True, failure_scope='security_control'),
    dict(IDLE, status='blocked', operation_started=False, failure_scope='security_control'),
    dict(IDLE, status='needs_review', operation_started=True),
    {'status': 'failed', 'result': {'ok': False, 'may_continue': False}},
    {'status': 'completed', 'result': {'items': [
        {'status': 'completed', 'result': {'ok': True, 'may_continue': False}},
        {'status': 'skipped', 'result': {'ok': True, 'skipped': True}}]}},
    {'status': 'completed', 'items': [dict(IDLE, status='failed', failure_scope='package')]},
])
def test_terminal_results_and_proven_local_failures_allow_remaining_approved_actions(result):
    original = copy.deepcopy(result)
    assert recommendation_continuation_reason(result) == ''
    assert result == original


@pytest.mark.parametrize('value', [None, [], '', 1, True, {}, {'note': 'No lifecycle proof'},
    {'status': None}, {'status': ''}, {'status': 'unknown'}, {'status': []},
    {'status': 'failed'}, {'status': 'blocked'}, {'status': 'needs_review'},
    {'status': 'completed', 'result': None}, {'status': 'completed', 'result': {}},
    {'status': 'completed', 'items': {}}, {'status': 'completed', 'items': [None]},
    {'status': 'completed', 'failed_items': True}])
def test_missing_or_malformed_evidence_defers_the_queue(value):
    reason = recommendation_continuation_reason(value)
    assert reason and len(reason) <= 180


@pytest.mark.parametrize('flag,value', [
    ('running', True), ('outcomeKnown', False), ('may_continue', True),
    ('uncertain', True), ('timed_out', True), ('cancelled', True), ('canceled', True),
    ('restartNeeded', True), ('reboot_required', True), ('rebootRequired', True),
    ('shared_barrier', True), ('installation_blocked', True), ('blocked_by_prior_installer', True),
])
@pytest.mark.parametrize('nested', [False, True])
def test_explicit_operation_barriers_stop_even_if_the_outer_status_says_completed(flag, value, nested):
    receipt = dict(IDLE, status='completed')
    receipt[flag] = value
    result = {'status': 'completed', 'result': {'items': [{'status': 'completed', 'result': receipt}]}} if nested else receipt
    assert recommendation_continuation_reason(result)


@pytest.mark.parametrize('flag', ['outcomeKnown', 'running', 'may_continue', 'uncertain', 'timed_out',
    'cancelled', 'restartNeeded', 'reboot_required', 'shared_barrier', 'operation_started',
    'installer_started', 'requiresReview', 'read_only', 'review_only', 'ok', 'skipped'])
@pytest.mark.parametrize('value', [None, 0, 1, 'false'])
def test_operation_booleans_cannot_be_missing_types_or_truthy_strings(flag, value):
    result = dict(IDLE, status='completed')
    result[flag] = value
    assert recommendation_continuation_reason(result)


@pytest.mark.parametrize('status', ['uncertain', 'running', 'prepared', 'installing', 'pending',
    'pending_activation', 'pending_verification', 'deferred', 'timed_out', 'cancelled', 'canceled'])
def test_explicit_nonterminal_or_cancelled_operation_status_stops(status):
    assert recommendation_continuation_reason(dict(IDLE, status=status))


@pytest.mark.parametrize('evidence', [
    {'exitCode': 3010}, {'exitCode': 1641}, {'exitCode': 0x8A150109},
    {'hresult': 0x80070005}, {'exitCode': 1618}, {'aggregateHresult': 0x80240016},
    {'aggregate_hresult': 0x80070005}, {'aggregate_hresult': -2147024891},
    {'aggregate_hresult': 0, 'aggregateHresult': 1},
    {'failure_scope': 'shared'}, {'failure_scope': 'app_batch'}, {'failure_scope': 'system'},
    {'failureKind': 'source_untrusted'}, {'failureKind': 'authorization'},
    {'exitCode': True}, {'hresult': '0'}, {'aggregate_hresult': None}, {'resultCode': 1},
])
def test_native_restart_shared_refusal_and_malformed_codes_remain_binding(evidence):
    assert recommendation_continuation_reason(dict(IDLE, status='completed', **evidence))


def test_after_readback_process_inventory_and_historical_journals_are_not_live_operations():
    result = dict(IDLE, status='failed', failure_scope='security_control', operation_started=True,
        after={'status': 'completed', 'reboot_required': True, 'processes': [{'running': True}]},
        before={'status': 'running'}, security_review={'running': True},
        final_verification={'status': 'unverified', 'cancelled': True},
        history=[{'status': 'uncertain'}], journal={'result': {'outcomeKnown': False}},
        scan={'items': [{'status': 'running'}]})
    assert recommendation_continuation_reason(result) == ''


def test_one_uncertain_native_item_cannot_be_hidden_by_completed_siblings():
    result = {'status': 'completed', 'items': [
        {'status': 'completed'}, {'status': 'failed', 'result': {'outcomeKnown': False, 'running': True}}]}
    assert recommendation_continuation_reason(result)


def test_cycles_and_operation_depth_are_bounded():
    cyclic = {'status': 'completed'}
    cyclic['result'] = cyclic
    assert recommendation_continuation_reason(cyclic)
    deep = {'status': 'completed'}
    for _ in range(15):
        deep = {'status': 'completed', 'result': deep}
    assert recommendation_continuation_reason(deep)


def test_more_than_one_thousand_items_or_the_total_node_budget_is_refused():
    assert recommendation_continuation_reason({'status': 'completed', 'items': [{'status': 'completed'}] * 1001})
    result = {'status': 'completed', 'items': [
        {'status': 'completed', 'result': {'status': 'completed', 'result': {'status': 'completed'}}}
        for _ in range(1000)]}
    assert recommendation_continuation_reason(result)


def test_the_full_supported_one_thousand_item_native_batch_still_fits_the_budget():
    result = {'status': 'completed', 'items': [
        {'status': 'completed', 'result': {'ok': True, 'may_continue': False}} for _ in range(1000)]}
    assert recommendation_continuation_reason(result) == ''


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('code', [0, 1])
def test_typed_successful_wua_receipt_needs_no_redundant_generic_lifecycle_flags(nested, code):
    receipt = dict(WUA_SUCCESS, hresult=code, aggregate_hresult=code)
    result = {'status': 'completed', 'result': {'status': 'completed', 'items': [
        {'status': 'completed', 'result': receipt}]}} if nested else receipt
    assert recommendation_continuation_reason(result) == ''


@pytest.mark.parametrize('field', list(WUA_SUCCESS))
def test_every_wua_terminal_success_field_is_required_without_generic_lifecycle_evidence(field):
    result = dict(WUA_SUCCESS)
    del result[field]
    assert recommendation_continuation_reason(result)


@pytest.mark.parametrize('field,value', [
    ('ok', False), ('ok', 1), ('resultCode', True), ('resultCode', 3),
    ('itemResultCode', '2'), ('itemResultCode', 4), ('hresult', -1),
    ('aggregate_hresult', -1), ('hresult', None), ('aggregate_hresult', '0'),
    ('reboot_required', None), ('reboot_required', True), ('reboot_required', 0),
    ('cancelled', True), ('may_continue', True), ('outcomeKnown', False), ('running', True),
])
def test_incomplete_failed_cancelled_or_restarting_wua_results_never_gain_terminal_success(field, value):
    assert recommendation_continuation_reason(dict(WUA_SUCCESS, **{field: value}))


def test_ok_true_alone_and_an_unexplained_partial_result_cannot_advance_the_queue():
    assert recommendation_continuation_reason({'ok': True})
    assert recommendation_continuation_reason({'status': 'partial'})
    assert recommendation_continuation_reason(dict(IDLE, status='partial')) == ''
    assert recommendation_continuation_reason({'status': 'partial', 'review_only': True}) == ''
