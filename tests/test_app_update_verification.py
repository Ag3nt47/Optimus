"""Owned software update observations with no app shutdown or real installation."""
from types import SimpleNamespace

import psutil
import pytest

from optimus import app_update_verification as verification
from optimus.app_update_verification import AppUpdateVerification, diagnostic_blockers, target_installed
from test_update_payload_providers import provider


PACKAGE = {'id': 'Vendor.App', 'version': '1.0', 'available': '2.0', 'source': 'winget'}
FAMILY = 'OpenAI.Codex_2p2nqsd0c76g0'
BLOCKER = {'pid': 1024, 'created_at': 123.0, 'package_family': FAMILY, 'name': 'Codex.exe'}


@pytest.mark.parametrize('expected,actual,good', [('2.0', '2.0', True), ('2.0', '2.0.0', True),
    ('2.0', '2.1', True), ('2.0', '1.9', False), ('2.0', '2.0-rc1', False),
    ('2.0', 'unknown', False), ('2.0', None, False), ('6000.6.7f1', '6000.6.7f1', True)])
def test_installed_target_requires_exact_or_provably_newer_version(expected, actual, good):
    assert target_installed(expected, actual, PACKAGE['id']) is good


def make(tmp_path, monkeypatch, *, families=True, blockers=True, restart=True, version='1.0'):
    service = AppUpdateVerification(tmp_path, PACKAGE, owner='uid:1', boot=lambda: 100, now=lambda: 200,
        directory_policy=lambda path, owner: path.mkdir(mode=0o700))
    log = service.prepare()
    if families:
        log.write_text('Deployment error 0x80073D02: Unable to install because the following apps need to be closed '
            'OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0.\n', encoding='utf-8')
    monkeypatch.setattr(verification, 'blocker_processes', lambda values: [dict(BLOCKER)] if blockers and values else [])
    receipt = dict(verified=False, installed_version=version, installation_reported=True,
        outcomeKnown=True, running=False, application_restart_required=restart, note='WinGet reported success.')
    service.finish(receipt)
    return service, receipt


def test_owned_msix_log_keeps_internal_package_identity_without_urls_or_commands():
    text = ('arbitrary https://example.test/payload.exe is not an instruction\n'
        'Deployment 0x80073D02: apps need to be closed OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0.\n'
        '0x80073D02 apps need to be closed ..\\evil.exe\n')
    assert diagnostic_blockers(text) == [FAMILY]


def test_pending_activation_records_exact_blocker_and_does_not_count_install(tmp_path, monkeypatch):
    service, receipt = make(tmp_path, monkeypatch)
    monkeypatch.setattr(verification.psutil, 'Process', lambda pid: SimpleNamespace(create_time=lambda: 123.0))
    previous = service.pending()
    assert previous['blocking_processes'] == [BLOCKER]
    assert receipt['verified'] is False and receipt['application_restart_required'] is True
    assert receipt['verification_status'] == 'pending_activation'
    assert FAMILY in receipt['note']
    assert (service.folder / 'winget.log').is_file()
    assert not (tmp_path / 'preferences.json').exists()


@pytest.mark.parametrize('state', ['exited', 'reused_pid', 'still_running', 'access_denied'])
def test_retry_needs_exact_blockers_to_exit_not_an_optimus_restart(tmp_path, monkeypatch, state):
    service, receipt = make(tmp_path, monkeypatch)
    def process(pid):
        if state == 'exited':
            raise psutil.NoSuchProcess(pid)
        if state == 'access_denied':
            raise psutil.AccessDenied(pid)
        return SimpleNamespace(create_time=lambda: 999.0 if state == 'reused_pid' else 123.0)
    monkeypatch.setattr(verification.psutil, 'Process', process)
    second = AppUpdateVerification(tmp_path, PACKAGE, owner='uid:1', boot=lambda: 100)
    assert bool(second.pending()) is (state in ('still_running', 'access_denied'))


def test_new_boot_allows_fresh_retry_but_launching_optimus_does_not(tmp_path, monkeypatch):
    service, receipt = make(tmp_path, monkeypatch, families=False, blockers=False, restart=False)
    assert service.pending()
    second = AppUpdateVerification(tmp_path, PACKAGE, owner='uid:1', boot=lambda: 200)
    assert second.pending() is None
    second.boot = lambda: 50
    assert second.pending()


def test_changed_version_pair_is_a_new_offer_not_a_replayed_pending_attempt(tmp_path, monkeypatch):
    make(tmp_path, monkeypatch, blockers=False)
    other = AppUpdateVerification(tmp_path, dict(PACKAGE, available='3.0'), owner='uid:1', boot=lambda: 100)
    assert other.pending() is None
    other = AppUpdateVerification(tmp_path, PACKAGE, owner='uid:2', boot=lambda: 100)
    with pytest.raises(ValueError, match='owner'):
        other.pending()


def test_corrupt_pending_evidence_does_not_silently_allow_retry(tmp_path, monkeypatch):
    service, receipt = make(tmp_path, monkeypatch)
    (service.folder / 'verification.json').write_text('{broken', encoding='utf-8')
    with pytest.raises(ValueError):
        service.pending()


def test_corrupt_app_receipt_does_not_block_other_packages(tmp_path, monkeypatch):
    service, receipt = make(tmp_path, monkeypatch)
    (service.folder / 'verification.json').write_text('{broken', encoding='utf-8')
    for identity in ('Vendor.Other', 'Vendor.Third'):
        other = AppUpdateVerification(tmp_path, dict(PACKAGE, id=identity), owner='uid:1', boot=lambda: 100,
            directory_policy=lambda path, owner: path.mkdir(mode=0o700))
        assert other.pending() is None
        assert other.prepare().parent.is_dir()
        assert other.directory != service.directory
    with pytest.raises(ValueError):
        service.pending()


def test_corrupt_old_offer_does_not_block_a_new_exact_version_pair(tmp_path, monkeypatch):
    service, receipt = make(tmp_path, monkeypatch)
    (service.folder / 'verification.json').write_text('{broken', encoding='utf-8')
    for change in ({'available': '3.0'}, {'version': '1.1'}):
        other = AppUpdateVerification(tmp_path, dict(PACKAGE, **change), owner='uid:1', boot=lambda: 100,
            directory_policy=lambda path, owner: path.mkdir(mode=0o700))
        assert other.pending() is None
        assert other.prepare().parent.is_dir()
        assert other.directory != service.directory
    with pytest.raises(ValueError):
        service.pending()


def test_installed_version_readback_runs_even_when_payload_cleanup_is_off(provider, tmp_path):
    raw = b'{"update_payload_cleanup_enabled":false}\n'
    (tmp_path / 'preferences.json').write_bytes(raw)
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is True and result['installed_version'] == '2.0'
    assert result['payload_cleanup']['status'] == 'disabled'
    assert (tmp_path / 'preferences.json').read_bytes() == raw
    assert any(call.args[0][1] == 'list' for call in provider._run.call_args_list)


def test_unsupported_payload_cleanup_handler_does_not_undo_real_version_verification(provider):
    provider.fixture_type = 'portable'
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is True
    assert result['payload_cleanup']['removed_files'] == 0


def test_app_restart_is_distinct_from_windows_reboot_and_not_completed(provider, monkeypatch):
    original = provider._run.side_effect
    provider.fixture_after = '1.0'
    def run(arguments, **options):
        result = original(arguments, **options)
        if arguments[1] == 'upgrade' and '--id' in arguments:
            result.stdout = 'Successfully installed. Restart the application to complete the upgrade.'
        return result
    provider._run.side_effect = run
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is False and result['requiresReview'] is True
    assert result['status'] == 'needs_review' and result['verification_status'] == 'pending_activation'
    assert result['application_restart_required'] is True and result['restartNeeded'] is None


def test_same_unverified_offer_is_rechecked_not_redownloaded(provider):
    provider.fixture_after = '1.0'
    first = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert first['verified'] is False
    before = len(provider.fixture_paths)
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert len(provider.fixture_paths) == before
    assert result['status'] == 'deferred' and result['installer_started'] is False
    assert result['outcomeKnown'] is True and result['running'] is False
    provider.fixture_after = '2.0'
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is True and result['installer_started'] is False
    assert len(provider.fixture_paths) == before


@pytest.mark.parametrize('before_launch', [False, True])
def test_one_pending_application_does_not_stop_other_approved_apps_or_claim_install(before_launch):
    from test_automated_software import backend, configuration
    from optimus.automated_software import run_software
    from optimus.parallel_updates import _journal_result, update_completion_state
    service = backend()
    pending = {'exitCode': None if before_launch else 0, 'status': 'deferred' if before_launch else 'needs_review',
        'skipped': before_launch, 'installer_started': not before_launch, 'outcomeKnown': True, 'running': False,
        'verified': False, 'installed_version': '1.0', 'from_version': '1.0', 'to_version': '2.0',
        'requiresReview': True, 'failure_scope': 'package', 'application_restart_required': True,
        'verification_status': 'pending_activation', 'blocking_package_families': [FAMILY]}
    complete = dict(exitCode=0, verified=True, installer_started=True, outcomeKnown=True, running=False)
    service.update_app_unattended.side_effect = [pending, complete]
    result = run_software(service, configuration())
    assert service.update_app_unattended.call_count == 2 and result['deferred_ids'] == []
    assert result['items'][0]['status'] == ('skipped' if before_launch else 'needs_review')
    assert result['items'][1]['status'] == 'completed'
    saved = _journal_result(result)['items'][0]
    assert saved['result']['installed_version'] == '1.0'
    assert saved['result']['to_version'] == '2.0' and saved['result']['verified'] is False
    assert saved['result']['blocking_package_families'] == [FAMILY]
    state = update_completion_state(result)
    assert state['app_counts']['updated'] == 1
    assert state['app_counts']['deferred' if before_launch else 'unverified'] == 1
