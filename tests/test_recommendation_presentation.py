"""Recommendation labels describe real routes without granting authority."""
import copy

import pytest

from optimus.recommendation_presentation import recommendation_presentation, recommendation_result_text


@pytest.mark.parametrize('identity,selection,label', [
    ('enable_firewall', 'Apply', 'Enable disabled Windows Firewall profiles'),
    ('enable_defender_realtime', 'Apply', 'Enable Microsoft Defender real-time protection'),
    ('update_defender_signatures', 'Apply', 'Update Microsoft Defender security intelligence'),
    ('review_security', 'Open', 'Open Windows Security'),
    ('review_windows_update', 'Open', 'Open Windows Update'),
    ('restart_required', 'Open', 'Open Windows restart controls'),
    ('review_legacy_patches', 'View', 'View vendor and legacy patch coverage'),
    ('remediate_defender_threats', 'Review', 'Request Defender remediation of reviewed active threats'),
    ('native-system-updates', 'Review', 'Review native system update guidance'),
])
def test_actions_describe_the_known_route(identity, selection, label):
    value = {'id': identity, 'title': 'Untrusted title', 'action': 'Force everything'}
    before = copy.deepcopy(value)
    result = recommendation_presentation(value)
    assert result['supported'] is True
    assert result['selection_label'] == selection and result['action_label'] == label
    assert result['confirmation_title'] == label
    assert 'Force' not in str(result) and value == before


@pytest.mark.parametrize('approved,expected', [(True, 'Install'), (False, 'Open'), ('true', 'Open'), (1, 'Open'), (None, 'Open')])
def test_windows_install_label_requires_literal_display_approval(approved, expected):
    result = recommendation_presentation({'id': 'review_windows_update'}, windows_install_approved=approved)
    assert result['selection_label'] == expected
    assert 'saved' in result['description']


@pytest.mark.parametrize('value', [None, [], {}, {'id': []}, {'id': 1}, {'id': 'unknown'}, {'id': 'ENABLE_FIREWALL'}])
def test_unknown_recommendation_never_promises_a_fix(value):
    result = recommendation_presentation(value, windows_install_approved=True)
    assert result['supported'] is False
    assert result['selection_label'] == 'Review'
    assert 'Unsupported' in result['action_label']


def test_manual_open_and_coverage_reports_cannot_look_like_installed_updates():
    result = recommendation_result_text({'status': 'manual_action_required', 'opened': 'windowsdefender:',
        'reason': 'Review historical detections.', 'installed_count': 20, 'scan': {'updates': [1] * 20}})
    assert 'Windows controls opened' in result
    assert 'No automatic installation or removal was applied' in result
    assert 'Review historical detections.' in result and '20' not in result
    result = recommendation_result_text({'status': 'review', 'review_only': True,
        'items': [{'status': 'completed'}], 'note': 'No compatible patch is asserted.'})
    assert 'No automatic installation' in result and 'completed update' not in result
    assert 'No compatible patch' in result


def test_real_receipts_show_completion_separate_from_verification():
    value = {'status': 'completed', 'items': [
        {'status': 'completed', 'result': {'verified': True}},
        {'status': 'completed', 'result': {'exitCode': 0}},
        {'status': 'failed'}, {'status': 'skipped'}, {'status': 'uncertain'}],
        'deferred_ids': ['one', 'two'], 'reboot_required': True}
    before = copy.deepcopy(value)
    result = recommendation_result_text(value)
    for expected in ('2 completed updates', '1 explicitly verified', '1 failed', '1 skipped',
                     '1 uncertain', '2 remaining updates deferred', 'Restart required'):
        assert expected in result
    assert value == before


def test_scan_offers_do_not_become_installed_count():
    value = {'status': 'completed', 'scan': {'updates': [{'id': 'x'}]},
        'eligible_count': 5, 'recognized_offers_count': 12}
    assert recommendation_result_text(value) == 'completed'


@pytest.mark.parametrize('value', [True, -1, 1.0, '2', 1000001])
def test_invalid_reported_count_is_not_rendered(value):
    assert recommendation_result_text({'status': 'completed', 'installed_count': value}) == 'completed'


def test_reported_counts_never_claim_independent_verification():
    result = recommendation_result_text({'status': 'completed', 'installed_count': 2, 'updated_count': 0})
    assert '2 reported installed' in result and '0 reported updated' in result
    assert 'verified' not in result.lower()


def test_windows_security_recommendation_wrapper_exposes_verified_install_count():
    result = recommendation_result_text({'kind': 'security-recommendation',
        'recommendation_id': 'review_windows_update', 'status': 'completed', 'verified_installs': 2,
        'result': {'items': [{'status': 'completed'}, {'status': 'completed'}, {'status': 'skipped'}]}})
    assert '2 verified installations' in result and '2 completed updates' in result
    assert '1 skipped' in result and '0 explicitly verified' not in result
    assert 'Outcome verified' not in result


@pytest.mark.parametrize('bad', [True, -1, 1.0, '2', 1000001])
def test_bad_verified_install_count_does_not_claim_verification(bad):
    assert recommendation_result_text({'status': 'completed', 'verified_installs': bad}) == 'completed'


def test_manual_result_cannot_claim_verified_installations():
    result = recommendation_result_text({'status': 'manual_action_required', 'verified_installs': 2})
    assert 'verified installation' not in result


def test_specific_block_reason_is_exposed_instead_of_bare_status():
    value = {'status': 'blocked', 'reason': 'Administrator access is required.',
        'note': 'Generic service summary.', 'verified': False}
    result = recommendation_result_text(value)
    assert 'Administrator access is required.' in result and 'Outcome not verified' in result
    assert 'Generic' not in result


@pytest.mark.parametrize('value', [None, [], 'completed', 1])
def test_bad_result_is_visibly_unverified(value):
    assert recommendation_result_text(value).startswith('Unverified:')


def test_result_rendering_is_bounded_and_strips_control_characters():
    result = recommendation_result_text({'status': 'blocked\x00\n', 'reason': 'A\t' * 10000,
        'deferred_ids': ['id'] * 1001, 'items': [{'status': 'completed'}] * 1001})
    assert len(result) <= 1200 and '\x00' not in result and '\n' not in result and '\t' not in result
    assert 'completed update' not in result and 'deferred' not in result
