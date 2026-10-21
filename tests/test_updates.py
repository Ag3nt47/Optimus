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
    updater._run = Mock(side_effect=[subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),''), subprocess.CompletedProcess([],0,'There are no pins configured.',''), subprocess.CompletedProcess([],0,'fixture completed','')])
    result = updater.updates_apply('Vendor.App',confirmed=True,unattended=True)
    arguments = updater._run.call_args.args[0]
    assert arguments == ['winget-fixture.exe','upgrade','--id','Vendor.App','--exact','--source','winget','--version','2.0','--silent','--disable-interactivity','--accept-source-agreements','--accept-package-agreements']
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
    updater._run = Mock(side_effect=[subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),''), subprocess.CompletedProcess([],0,'There are no pins configured.',''), subprocess.TimeoutExpired(['winget-fixture.exe'],1800)])
    with pytest.raises(RuntimeError,match='may still be running'):
        updater.updates_apply('Vendor.App',confirmed=True,unattended=True)
    assert updater._run.call_count == 3


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


def pin_table(*rows):
    header = f"{'Name':32}{'Id':44}{'Version':18}{'Source':16}{'Pin type':16}Pinned version"
    return '\n'.join([header, '-' * len(header)] + [
        f'{name:32}{identity:44}{version:18}{source:16}{kind:16}{pinned}'
        for name, identity, version, source, kind, pinned in rows])


def test_ordinary_pins_are_preserved_before_exact_unattended_upgrade(updater):
    scan(updater)
    updater._run = Mock(side_effect=[
        subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),''),
        subprocess.CompletedProcess([],0,pin_table(('Fixture App','Vendor.App','1.0','winget','Pinning','')),'')])
    result = updater.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['skipped'] and 'pin' in result['note']
    assert updater._run.call_count == 2
    assert updater._run.call_args.args[0] == ['winget-fixture.exe','pin','list','--disable-interactivity']


def test_installed_productcode_pin_is_matched_by_full_name(updater):
    scan(updater)
    updater._run = Mock(side_effect=[
        subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),''),
        subprocess.CompletedProcess([],0,pin_table(('Fixture App','{00000000-0000-0000-0000-000000000001}','1.0','Installed','Blocking','')),'')])
    result = updater.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['skipped'] and 'installed-version' in result['note']
    assert updater._run.call_count == 2


def test_unrelated_repository_pin_does_not_block_an_unpinned_application(updater):
    scan(updater)
    updater._run = Mock(side_effect=[
        subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),''),
        subprocess.CompletedProcess([],0,pin_table(('Other App','Vendor.Other','1.0','winget','Gating','1.*')),''),
        subprocess.CompletedProcess([],0,'fixture installed','')])
    result = updater.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['status'] == 'completed' and updater._run.call_count == 3


@pytest.mark.parametrize('output,code', [
    ('',0), ('Localized pin output',0), ('There are no pins configured.',5),
    (pin_table(('Fixture App','Vendor.App...','1.0','winget','Pinning','')),0),
    (pin_table(),0),
])
def test_unrecognized_or_failed_pin_inventory_never_starts_an_installer(updater,output,code):
    scan(updater)
    updater._run = Mock(side_effect=[
        subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),''),
        subprocess.CompletedProcess([],code,output,'')])
    result = updater.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['skipped'] and updater._run.call_count == 2


@pytest.mark.parametrize('installed,available', [('1.1','2.0'),('1.0','2.1'),('Unknown','2.0')])
def test_changed_current_or_available_version_is_not_applied_from_stale_preview(updater,installed,available):
    scan(updater)
    updater._run = Mock(return_value=subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App',installed,available,'winget')),''))
    result = updater.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['skipped'] and 'changed' in result['note']
    assert updater._run.call_count == 1


def test_pins_are_rechecked_for_each_application_not_reused_from_last_item(updater):
    scan(updater)
    upgrade_scan = subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),'')
    updater._run = Mock(side_effect=[upgrade_scan,
        subprocess.CompletedProcess([],0,'There are no pins configured.',''),
        subprocess.CompletedProcess([],0,'fixture installed',''), upgrade_scan,
        subprocess.CompletedProcess([],0,pin_table(('Fixture App','Vendor.App','1.0','winget','Pinning','')),'')])
    assert updater.updates_apply('Vendor.App',confirmed=True,unattended=True)['status'] == 'completed'
    assert updater.updates_apply('Vendor.App',confirmed=True,unattended=True)['skipped']
    assert updater._run.call_count == 5

@pytest.mark.parametrize('code,text,restart,running', [
    (0, 'Restart your PC to finish installation.', True, False),
    (3010, '', True, False), (1641, '', True, False),
    (0x8A150109, '', True, False), (0x8A15010A - 2**32, '', True, False),
    (1618, '', False, True), (0x8A150102 - 2**32, '', False, True),
    (0, 'Successfully installed.', False, False),
])
def test_native_win32_and_winget_restart_or_inprogress_results_are_preserved(updater,code,text,restart,running):
    scan(updater)
    updater._run = Mock(side_effect=[
        subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),''),
        subprocess.CompletedProcess([],0,'There are no pins configured.',''),
        subprocess.CompletedProcess([],code,text,'')])
    result = updater.updates_apply('Vendor.App',confirmed=True,unattended=True)
    assert (result['restartNeeded'] is True) == restart
    assert result['running'] is running

def test_native_outcome_includes_version_pair_actually_sent_to_installer(updater):
    scan(updater)
    updater._run = Mock(side_effect=[
        subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),''),
        subprocess.CompletedProcess([],0,'There are no pins configured.',''),
        subprocess.CompletedProcess([],0,'Successfully installed.','')])
    result = updater.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['from_version'] == '1.0' and result['to_version'] == '2.0'
    assert updater._run.call_args.args[0][updater._run.call_args.args[0].index('--version') + 1] == result['to_version']


def test_pin_inventory_failure_requires_review_and_retains_actual_package_pair(updater):
    scan(updater)
    updater._run = Mock(side_effect=[
        subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),''),
        subprocess.CompletedProcess([],5,'Fixture failed pin index','')])
    result = updater.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['requiresReview'] and result['failureKind'] == 'pin_inventory_untrusted'
    assert result['skipped'] and result['from_version'] == '1.0' and result['to_version'] == '2.0'
    assert updater._run.call_count == 2

def scoped_table(*rows):
    header = f"{'Name':32}{'Id':36}{'Version':20}Available"
    return '\n'.join([header, '-' * (len(header) + 5)] + [
        f'{name:32}{identity:36}{installed:20}{available}'
        for name, identity, installed, available in rows]) + '\n'


def test_native_source_scoped_four_column_win_get_layout_remains_selectable(updater):
    text = scoped_table(('Fixture Application', 'Vendor.App', '1.0', '2.0'),
                        ('Unknown Fixture', 'Vendor.Other', '< 3.0', '4.0'),
                        ('Truncated Fixture', 'Vendor.Trunc...', '1.0', '2.0'))
    result = scan(updater, text)
    assert result['structured'] and result['exitCode'] == 0
    assert [item['id'] for item in result['packages']] == ['Vendor.App']
    assert result['packages'][0]['source'] == 'winget'
    assert updater._run.call_args.args[0] == ['winget-fixture.exe','upgrade','--source','winget','--disable-interactivity']


def test_four_column_layout_requires_explicit_trusted_source_context():
    output = scoped_table(('Fixture Application', 'Vendor.App', '1.0', '2.0'))
    assert Maintenance._parse_update_table(output)[1] is False
    assert Maintenance._parse_update_table(output, source_context='msstore')[1] is False
    rows, recognized, _ = Maintenance._parse_update_table(output, source_context='winget')
    assert recognized and rows[0]['id'] == 'Vendor.App' and rows[0]['source'] == 'winget'


def test_source_scoped_four_column_layout_can_be_revalidated_before_install(updater):
    output = scoped_table(('Fixture Application', 'Vendor.App', '1.0', '2.0'))
    scan(updater, output)
    updater._run = Mock(side_effect=[subprocess.CompletedProcess([],0,output,''),
        subprocess.CompletedProcess([],0,'There are no pins configured.',''),
        subprocess.CompletedProcess([],0,'Successfully installed.','')])
    result = updater.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['status'] == 'completed' and result['to_version'] == '2.0'

@pytest.mark.parametrize('boundary', ['before_probes', 'during_revalidation', 'during_pin_read', 'before_install'])
def test_cancellation_during_native_probes_cannot_start_an_installer(updater, boundary):
    import threading
    stop = threading.Event()
    scan(updater)
    operations = []
    def fake_run(arguments, **kwargs):
        if arguments[1] == 'pin':
            operations.append('pin-read')
            if boundary == 'during_pin_read':
                stop.set()
            return subprocess.CompletedProcess([],0,'There are no pins configured.','')
        if '--id' not in arguments:
            operations.append('revalidation')
            if boundary == 'during_revalidation':
                stop.set()
            return subprocess.CompletedProcess([],0,table(('Fixture App','Vendor.App','1.0','2.0','winget')),'')
        operations.append('INSTALLER')
        return subprocess.CompletedProcess([],0,'fixture installer','')
    updater._run = Mock(side_effect=fake_run)
    if boundary == 'before_probes':
        stop.set()
    if boundary == 'before_install':
        original_winget = updater._winget
        def command_path():
            # Revalidation and pin-read call _winget first; the install command
            # builds only after both probes complete.
            if operations == ['revalidation', 'pin-read']:
                stop.set()
            return original_winget()
        updater._winget = command_path
    result = updater.updates_apply('Vendor.App', confirmed=True, unattended=True, cancel_event=stop)
    assert result['cancelled'] and result['skipped'] and result['status'] == 'cancelled'
    assert 'INSTALLER' not in operations
    if boundary == 'before_probes':
        assert operations == []
    if boundary == 'during_revalidation':
        assert operations == ['revalidation']


def test_backend_passes_optional_cancellation_to_native_updater():
    import threading
    backend = Backend.__new__(Backend)
    backend.maintenance = Mock()
    stop = threading.Event()
    backend.update_app_unattended('Vendor.App', cancel_event=stop)
    backend.maintenance.updates_apply.assert_called_once_with('Vendor.App',confirmed=True,unattended=True,cancel_event=stop)