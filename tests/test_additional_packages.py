import copy
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from optimus.additional_packages import (AdditionalPackages, SOURCE, _installed_version, _native_receipt,
                                         _ordinary_executable, _read_command, parse_outdated, validate_configuration)
from optimus.automation import RunLease
from optimus.parallel_updates import pending_updates_barrier
from optimus.winget_console import ConsoleOutcomeError, ConsolePreflightError


CONFIG={'enabled':True,'ids':['alpha','beta'],'agreements_accepted':True}


def native(arguments, code=0):
    value=subprocess.CompletedProcess(arguments,code,'','')
    value.capture_complete=True
    value.console_receipt={'schema':1,'verified':True,'quiescent':True,'job_verified':True,'output_eof':True,
        'job_active_processes':0,'console_client_count':1,'supervisor_exit_code':0,'target_exit_code':code}
    return value


class Backend:
    def __init__(self,path,config=CONFIG):
        self.data_dir=path
        self.settings={'toolkit_settings':{'providers':copy.deepcopy(config)},'software_updates_enabled':False}
    def preferences(self): return copy.deepcopy(self.settings)
    def security_status(self):return {'reboot_required':False}


class Harness:
    def __init__(self,tmp_path,codes=(0,0),post_error=False):
        self.backend=Backend(tmp_path/'data')
        self.exe=tmp_path/'choco.exe';self.exe.write_bytes(b'fixed ordinary mock executable')
        self.codes=list(codes);self.calls=[];self.reads=[];self.post_error=post_error;self.version='2.5.1'
        self.inventory='alpha|1.0|2.0|false\nbeta|3.0|4.0|false\n'
        self.installed='alpha|2.0\nbeta|4.0\n'
        self.service=AdditionalPackages(self.backend,discover=lambda:self.exe,reader=self.reader,installer=self.install)
    def reader(self,args):
        self.reads.append(args)
        if args[1]=='--version':return self.version
        if args[1]=='outdated':return self.inventory
        if args[1]=='list':
            if self.post_error:raise OSError('post verification unavailable')
            return self.installed
        raise AssertionError(args)
    def install(self,args,**kwargs):
        self.calls.append((list(args),kwargs))
        return native(args,self.codes.pop(0))
    def run(self,**kwargs):
        with RunLease(self.backend.data_dir) as lease:
            return self.service.run(self.backend.preferences()['toolkit_settings']['providers'],lease=lease,**kwargs)


def test_exact_approved_known_failure_continues_and_hashes_intent(tmp_path):
    harness=Harness(tmp_path,codes=(1603,0))
    result=harness.run()
    assert [row['status'] for row in result['items']]==['failed','completed']
    assert result['outcomeKnown'] is True and result['failed_items']==1
    assert len(harness.calls)==2
    assert pending_updates_barrier(harness.backend) is None
    for args,options in harness.calls:
        assert args[1:3] in (['upgrade','alpha'],['upgrade','beta'])
        assert '--source='+SOURCE in args and '--ignore-dependencies' in args
        assert '--require-checksums' in args and '--fail-on-not-installed' in args
        assert '--ignore-remembered-arguments' in args and '--exclude-prerelease' in args
        assert not any(item in args for item in ('--force','--allow-downgrade','--ignore-checksums','all'))
        assert options['timeout']==1800
    journals=list((harness.backend.data_dir/'parallel-updates').glob('*.json'))
    assert len(journals)==2
    for path in journals:
        record=json.loads(path.read_text())
        assert record['status']=='completed'
        assert len(record['argv_sha256'])==len(record['approval_sha256'])==len(record['executable_sha256'])==64
        assert record['owner'].startswith(('sid:','uid:'))


def test_post_version_query_failure_does_not_reblock_queue(tmp_path):
    harness=Harness(tmp_path,post_error=True)
    result=harness.run()
    assert len(harness.calls)==2
    assert [row['status'] for row in result['items']]==['needs_review','needs_review']
    assert result['outcomeKnown'] is True and not result.get('uncertain')
    assert all(row['uncertain'] is False and row['result']['outcomeKnown'] is True for row in result['items'])
    assert pending_updates_barrier(harness.backend) is None


@pytest.mark.parametrize('bad',['alpha|2.0|extra','alpha|2.0\nALPHA|2.0','warning: there was an error','alpha|;bad'])
def test_malformed_post_inventory_is_known_unverified_not_native_unknown(tmp_path,bad):
    harness=Harness(tmp_path);harness.installed=bad
    result=harness.run()
    assert len(harness.calls)==2 and result['outcomeKnown'] is True
    assert pending_updates_barrier(harness.backend) is None


def test_unknown_native_outcome_blocks_next_and_stays_durable(tmp_path):
    harness=Harness(tmp_path)
    def unknown(args,**kwargs):harness.calls.append((args,kwargs));raise ConsoleOutcomeError('Native outcome unverified')
    harness.service.installer=unknown
    result=harness.run()
    assert len(harness.calls)==1 and result['outcomeKnown'] is False
    assert pending_updates_barrier(harness.backend)
    record=json.loads(next((harness.backend.data_dir/'parallel-updates').glob('*.json')).read_text())
    assert record['status']=='uncertain' and record['installer_prepared'] is True


def test_known_preflight_failure_does_not_block_next(tmp_path):
    harness=Harness(tmp_path)
    def install(args,**kwargs):
        harness.calls.append((args,kwargs))
        if len(harness.calls)==1:raise ConsolePreflightError('No target launched')
        return native(args)
    harness.service.installer=install
    result=harness.run()
    assert len(harness.calls)==2 and result['items'][0]['result']['installer_started'] is False
    assert pending_updates_barrier(harness.backend) is None


@pytest.mark.parametrize('code',[1641,3010,350,1604])
def test_restart_request_stops_new_installs_and_retains_restart_gate(tmp_path,code):
    harness=Harness(tmp_path,codes=(code,0))
    result=harness.run()
    assert len(harness.calls)==1 and result['restartNeeded'] is True
    harness.backend.security_status=lambda:{'reboot_required':True}
    assert pending_updates_barrier(harness.backend)
    harness.backend.security_status=lambda:{'reboot_required':False}
    assert pending_updates_barrier(harness.backend) is None


@pytest.mark.parametrize('version',['2.0.0','10.0.0','21.1'])
def test_recognized_current_and_future_stable_manager_versions(tmp_path,version):
    harness=Harness(tmp_path);harness.version=version
    assert harness.service.scan()['provider_version']==version


@pytest.mark.parametrize('version',['1.4.0','2.5.0-beta','Chocolatey 2.5.0',';evil',''])
def test_unfamiliar_manager_versions_never_install(tmp_path,version):
    harness=Harness(tmp_path);harness.version=version
    with pytest.raises(ValueError):harness.run()
    assert not harness.calls


@pytest.mark.parametrize('ids',[['all'],['ALL'],['chocolatey'],['alpha','ALPHA'],['-y'],['alpha;format'],['../alpha']])
def test_reserved_duplicate_or_option_like_ids_refused(ids):
    with pytest.raises(ValueError):validate_configuration(dict(CONFIG,ids=ids))


def test_pins_and_unknown_ordering_are_excluded(tmp_path):
    harness=Harness(tmp_path);harness.inventory='alpha|1.0|2.0|true\nbeta|3.0|4.0-beta|false'
    result=harness.run()
    assert [row['status'] for row in result['items']]==['skipped','skipped']
    assert not harness.calls


@pytest.mark.parametrize('text',['alpha|1.0|2.0','alpha|1|2|maybe','alpha|1|2|false\nALPHA|1|2|false','Header','alpha|1|2|false|extra'])
def test_bad_outdated_output_refused(text):
    with pytest.raises(ValueError):parse_outdated(text)


def test_approvals_revalidated_between_packages(tmp_path):
    harness=Harness(tmp_path)
    def install(args,**kwargs):
        harness.calls.append((args,kwargs));harness.backend.settings['toolkit_settings']['providers']['ids']=['alpha']
        return native(args)
    harness.service.installer=install
    result=harness.run()
    assert len(harness.calls)==1
    assert result['items'][-1]['status']=='skipped'


@pytest.mark.parametrize('policy,rules', [('all_supported',[]),('selected',[]),('review',[{'id':'Vendor.Alpha','source':'winget','update_policy':'always_update','presence':'keep_current'}]),('review',[{}])])
def test_dual_provider_automatic_ownership_is_deferred(tmp_path,policy,rules):
    harness=Harness(tmp_path)
    harness.backend.settings.update(software_updates_enabled=True,software_update_policy=policy,software_app_rules=rules)
    result=harness.run()
    assert result['deferred'] is True and not harness.calls and not harness.reads


def test_review_only_primary_provider_can_coexist(tmp_path):
    harness=Harness(tmp_path)
    harness.backend.settings.update(software_updates_enabled=True,software_update_policy='review',software_app_rules=[])
    assert len(harness.run()['items'])==2


def test_manager_identity_change_skips_remaining_offer(tmp_path):
    harness=Harness(tmp_path)
    def install(args,**kwargs):
        harness.calls.append((args,kwargs));harness.exe.write_bytes(b'changed provider')
        return native(args)
    harness.service.installer=install
    result=harness.run()
    assert len(harness.calls)==1
    assert result['items'][0]['uncertain'] is False
    assert result['items'][-1]['status']=='skipped'


def test_closed_or_missing_lease_refused(tmp_path):
    harness=Harness(tmp_path)
    with pytest.raises(ValueError):harness.service.run(CONFIG,lease=RunLease(harness.backend.data_dir))
    assert not harness.calls


def test_cancel_before_first_offer_never_installs(tmp_path):
    harness=Harness(tmp_path);event=threading.Event();event.set()
    result=harness.run(cancel_event=event)
    assert result['cancelled'] is True and not harness.calls


@pytest.mark.parametrize('field,value', [('verified',False),('schema',True),('job_active_processes',False),('job_active_processes',1),
                                     ('output_eof',False),('supervisor_exit_code',1),('console_client_count',2),('target_exit_code',1)])
def test_incomplete_or_mistyped_receipts_refused(field,value):
    args=['C:/choco.exe','upgrade','alpha']
    result=native(args);result.console_receipt[field]=value
    with pytest.raises(RuntimeError):_native_receipt(result,args)


def test_receipt_arguments_must_match_exact_invocation():
    args=['C:/choco.exe','upgrade','alpha']
    result=native(args)
    with pytest.raises(RuntimeError):_native_receipt(result,args+['--force'])


def test_enhanced_outdated_code_two_is_read_only_success(monkeypatch):
    monkeypatch.setattr('optimus.additional_packages.subprocess.run',lambda *args,**kwargs:SimpleNamespace(returncode=2,stdout='alpha|1|2|false',stderr=''))
    assert _read_command(['C:/choco.exe','outdated'])=='alpha|1|2|false'
    with pytest.raises(RuntimeError):_read_command(['C:/choco.exe','--version'])


def test_hardlinked_executable_is_refused(tmp_path):
    import os
    executable=tmp_path/'choco.exe';executable.write_bytes(b'identity')
    try:os.link(executable,tmp_path/'other.exe')
    except OSError:pytest.skip('Filesystem does not support hardlinks.')
    with pytest.raises(ValueError):_ordinary_executable(executable)


def test_strict_installed_list_supports_case_insensitive_identity():
    assert _installed_version('Alpha|2.0','alpha')=='2.0'


def test_missing_provider_is_truthfully_unavailable(tmp_path):
    service=AdditionalPackages(Backend(tmp_path),discover=lambda:None,reader=lambda args:pytest.fail('No command expected'))
    assert service.scan()['available'] is False


def test_disabled_provider_never_discovers_manager(tmp_path):
    service=AdditionalPackages(Backend(tmp_path),discover=lambda:pytest.fail('No discovery expected'))
    assert service.run({'enabled':False},lease=None)['skipped'] is True


def test_additional_provider_progress_resets_each_item_and_ignores_vendor_percent_text(tmp_path):
    harness = Harness(tmp_path)
    original = harness.service.installer
    def install(arguments, **options):
        # Arbitrary package-script output has no trusted percentage contract.
        options['progress_callback']({'phase': 'install', 'percent': 71})
        return original(arguments, **options)
    harness.service.installer = install
    events = []
    result = harness.run(progress_callback=events.append)
    assert [row['status'] for row in result['items']] == ['completed', 'completed']
    assert [(event['item_progress']['key'], event['item_progress']['phase'], event['item_progress']['percent']) for event in events] == [
        (key, phase, percent) for key in ('additional:chocolatey:alpha', 'additional:chocolatey:beta')
        for phase, percent in [('install', None), ('verify', None), ('install', 100.0)]]
    assert [event['current'] for event in events] == [0, 0, 0, 1, 1, 1]
    assert all(event['total'] == 2 for event in events)
    count = len(events)
    for _, options in harness.calls:
        options['progress_callback']({'phase': 'install', 'percent': 88})
    assert len(events) == count


@pytest.mark.parametrize('problem', ['known_failure', 'restart', 'readback', 'wrong_version', 'unknown'])
def test_additional_progress_only_reaches100_after_successful_exact_readback(tmp_path, problem):
    harness = Harness(tmp_path, codes=(1603, 1603) if problem == 'known_failure' else (3010, 0) if problem == 'restart' else (0, 0), post_error=problem == 'readback')
    if problem == 'wrong_version':
        harness.installed = 'alpha|1.0\nbeta|3.0\n'
    if problem == 'unknown':
        harness.service.installer = lambda *args, **options: (_ for _ in ()).throw(ConsoleOutcomeError('Unknown native child'))
    events = []
    harness.run(progress_callback=events.append)
    assert events and events[0]['item_progress']['phase'] == 'install'
    assert not any(event['item_progress']['percent'] == 100 for event in events)


def test_additional_observer_errors_are_display_only_and_invalid_observer_never_installs(tmp_path):
    harness = Harness(tmp_path)
    def error(_):
        raise RuntimeError('Display observer failed')
    assert all(row['status'] == 'completed' for row in harness.run(progress_callback=error)['items'])
    count = len(harness.calls)
    with pytest.raises(ValueError):
        harness.run(progress_callback=42)
    assert len(harness.calls) == count
