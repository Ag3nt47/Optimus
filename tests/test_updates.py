"""Exact WinGet candidates and unattended flags; all process calls are mocked."""
from pathlib import Path
import subprocess
import time
from unittest.mock import Mock, patch
import pytest
from optimus.maintenance import Maintenance
from optimus.backend import Backend


def table(*rows):
    header = f"{'Name':24}{'Id':32}{'Version':18}{'Available':18}Source"
    lines = [header, '-' * 104]
    for name, package_id, installed, available, source in rows:
        lines.append(f'{name:24}{package_id:32}{installed:18}{available:18}{source}')
    return '\n'.join(lines) + '\n'


@pytest.fixture
def updater(tmp_path):
    value = Maintenance(None, tmp_path)
    value._winget = Mock(return_value='winget-fixture.exe')
    return value


def scan(updater, text=None, code=0):
    updater._run = Mock(return_value=subprocess.CompletedProcess([],code,text or table(('Fixture App','Vendor.App','1.0','2.0','winget')),''))
    return updater.updates_scan()


def test_scan_exposes_matching_aliases_and_pins_source(updater):
    result = scan(updater)
    assert result['structured'] and result['exitCode'] == 0
    assert result['packages'] == result['updates']
    row = result['packages'][0]
    assert row['id'] == row['package_id'] == 'Vendor.App' and row['source'] == 'winget'
    assert updater._run.call_args.args[0] == ['winget-fixture.exe','upgrade','--source','winget','--disable-interactivity']
    assert result['text'] == result['output']


@pytest.mark.parametrize('installed,available', [('Unknown','2.0'),('1.0','Unknown'),('1.0','1.0'),('1.0','Latest'),('1.0','2.0...'),('< 1.0','2.0')])
def test_unknown_equal_and_truncated_versions_are_not_candidates(updater,installed,available):
    result = scan(updater,table(('Fixture','Vendor.App',installed,available,'winget')))
    assert result['packages'] == []
    updater._run.reset_mock()
    with pytest.raises(ValueError):updater.updates_apply('Vendor.App',confirmed=True,unattended=True)
    updater._run.assert_not_called()


def test_repeated_id_including_unknown_installation_is_ambiguous(updater):
    result = scan(updater,table(('Known','Vendor.App','1.0','2.0','winget'),('Unknown','Vendor.App','Unknown','2.0','winget')))
    assert result['packages'] == [] and any('Ambiguous' in text for text in result['excluded'])


@pytest.mark.parametrize('output', ['Nom Id Version Disponible Source\n------------------------------\nFixture Vendor.App 1.0 2.0 winget', 'Name Id Version Available Source\nFixture Vendor.App 1.0 2.0 winget', 'Name Id Version Available\n--------------------------------\nFixture Vendor.App 1.0 2.0'])
def test_localized_or_changed_layout_is_text_only(updater,output):
    result = scan(updater,output)
    assert not result['structured'] and not result['packages']
    assert result['text'] == output


def test_source_mismatch_and_nonzero_scan_clear_candidates(updater):
    assert not scan(updater,table(('Fixture','Vendor.App','1.0','2.0','msstore')))['packages']
    assert not scan(updater,code=5)['structured']
    with pytest.raises(ValueError):updater.updates_apply('Vendor.App',confirmed=True,unattended=True)


def test_failed_scan_discards_previous_candidate_cache(updater):
    scan(updater)
    updater._run = Mock(side_effect=subprocess.TimeoutExpired(['winget-fixture.exe'],1))
    with pytest.raises(subprocess.TimeoutExpired):updater.updates_scan()
    updater._run.reset_mock()
    with pytest.raises(ValueError):updater.updates_apply('Vendor.App',confirmed=True,unattended=True)
    updater._run.assert_not_called()


def test_unattended_exact_command_accepts_only_required_agreements(updater):
    scan(updater)
    updater._run = Mock(return_value=subprocess.CompletedProcess([],0,'fixture completed',''))
    result = updater.updates_apply('Vendor.App',confirmed=True,unattended=True)
    arguments = updater._run.call_args.args[0]
    assert arguments == ['winget-fixture.exe','upgrade','--id','Vendor.App','--exact','--source','winget','--silent','--disable-interactivity','--accept-source-agreements','--accept-package-agreements']
    assert not {'--all','--force','--include-unknown','--allow-reboot','--ignore-security-hash'} & set(arguments)
    assert result['unattended'] and result['status'] == 'completed' and result['exitCode'] == 0


@pytest.mark.parametrize('package_id', ['--all','Vendor.App --all','Vendor.*','Vendor/App','Vendor.App;calc',None,'Other.App'])
def test_invalid_or_unselected_id_never_invokes_updater(updater,package_id):
    scan(updater);updater._run.reset_mock()
    with pytest.raises(ValueError):updater.updates_apply(package_id,confirmed=True,unattended=True)
    updater._run.assert_not_called()


def test_explicit_confirmation_freshness_and_source_are_required(updater):
    scan(updater);updater._run.reset_mock()
    with pytest.raises(ValueError):updater.updates_apply('Vendor.App',unattended=True)
    updater._last_update_scan = time.time() - 901
    with pytest.raises(ValueError):updater.updates_apply('Vendor.App',confirmed=True,unattended=True)
    updater._last_update_scan = time.time()
    updater._last_update_packages['Vendor.App']['source'] = 'other'
    with pytest.raises(ValueError):updater.updates_apply('Vendor.App',confirmed=True,unattended=True)
    updater._run.assert_not_called()


def test_manual_mode_still_requires_confirmation_and_accepts_no_agreement(updater):
    scan(updater)
    updater._run = Mock(return_value=subprocess.CompletedProcess([],5,'fixture failed',''))
    result = updater.updates_apply('Vendor.App',confirmed=True)
    arguments = updater._run.call_args.args[0]
    assert '--silent' not in arguments and '--accept-package-agreements' not in arguments
    assert '--accept-source-agreements' not in arguments and result['status'] == 'failed'


def test_timeout_is_reported_without_replaying_command(updater):
    scan(updater)
    updater._run = Mock(side_effect=subprocess.TimeoutExpired(['winget-fixture.exe'],1800))
    with pytest.raises(RuntimeError,match='may still be running'):
        updater.updates_apply('Vendor.App',confirmed=True,unattended=True)
    assert updater._run.call_count == 1


def test_process_stdin_cannot_wait_for_interactive_answers():
    with patch('optimus.maintenance.subprocess.run') as runner:
        Maintenance._run(['winget-fixture.exe','upgrade'])
    assert runner.call_args.kwargs['stdin'] == subprocess.DEVNULL
    assert runner.call_args.kwargs['shell'] is False


def test_backend_passes_explicit_unattended_confirmation():
    backend = Backend.__new__(Backend)
    backend.maintenance = Mock()
    backend.update_app_unattended('Vendor.App')
    backend.maintenance.updates_apply.assert_called_once_with('Vendor.App',confirmed=True,unattended=True)
