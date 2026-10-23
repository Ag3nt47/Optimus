"""Self-update uses synthetic archives, injected network/processes and temp state."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

from optimus import self_update as module
from optimus.automation import RunLease


def pe(machine=0x8664):
    value = bytearray(128)
    value[:2] = b'MZ'; struct.pack_into('<I',value,60,64)
    value[64:70] = b'PE\0\0' + struct.pack('<H',machine)
    return bytes(value)


def archive(version='0.11.7', *, extra=None, executable=None):
    target = io.BytesIO()
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as writer:
        writer.writestr('Optimus/Optimus.exe',pe() if executable is None else executable)
        writer.writestr('Optimus/source/pyproject.toml','[project]\nversion="' + version + '"\n')
        writer.writestr('Optimus/source/src/optimus/app.py',"FLAGS = ['--self-update-worker','--self-update-resume']\n")
        if extra:
            for name,data in extra: writer.writestr(name,data)
    raw=target.getvalue()
    # The Windows zip writer normalizes backslashes before storage. Restore
    # literal same-length malicious names in both headers for this fixture.
    for name,_ in extra or []:
        if '\\' in name:raw=raw.replace(name.replace('\\','/').encode(),name.encode())
    return raw


def release(binary=None,version='0.11.7'):
    binary = archive(version) if binary is None else binary
    digests = {name:hashlib.sha256(binary if name == 'Optimus-Windows-x64.zip' else name.encode()).hexdigest() for name in module.ASSETS}
    sums = ''.join(digests[name] + '  ' + name + '\n' for name in sorted(digests)).encode()
    data = {'Optimus-Windows-x64.zip':binary,'SHA256SUMS.txt':sums}
    assets = []
    for index,name in enumerate(('Optimus-Windows-x64.zip','SHA256SUMS.txt'),1):
        assets.append({'id':index,'name':name,'state':'uploaded','size':len(data[name]),
            'digest':'sha256:' + hashlib.sha256(data[name]).hexdigest(),'url':module.API + '/releases/assets/' + str(index),
            'browser_download_url':module.WEB + '/releases/download/v' + version + '/' + name})
    return {'id':17,'tag_name':'v' + version,'url':module.API + '/releases/17','html_url':module.WEB + '/releases/tag/v' + version,
            'draft':False,'prerelease':False,'published_at':'2026-10-23T00:00:00Z','assets':assets}, data


class Transport:
    def __init__(self,binary=None):
        self.release,self.data = release(binary)
        self.requests = []
    def get_json(self,url,**kwargs):
        self.requests.append(url)
        assert url in (module.API + '/releases/latest',module.API + '/releases/17')
        return copy.deepcopy(self.release)
    def download(self,asset,destination,**kwargs):
        self.requests.append(asset['api_url'])
        Path(destination).write_bytes(self.data[asset['name']])
        if kwargs.get('progress_callback'):
            kwargs['progress_callback']({'phase':'download','description':asset['name'],'bytes_received':asset['size'],'bytes_total':asset['size']})


class Native:
    def __init__(self,executable):
        self.identity = {'pid':1234,'created_at':100.0,'exe':str(executable)}
        self.living = False
        self.others = []
        self.launches = []
    def current(self): return copy.deepcopy(self.identity)
    def alive(self,identity):
        assert identity == self.identity
        return self.living
    def other_instances(self): return self.others
    def launch(self,executable,arguments,**kwargs):
        self.launches.append((str(executable),list(arguments),kwargs))
        return {'started':True,'pid':2000 + len(self.launches)}


@pytest.fixture
def updater(tmp_path):
    base = tmp_path / 'Optimus'
    installation = base / 'Releases' / '0.11.6'; installation.mkdir(parents=True)
    data = base / 'Data'; data.mkdir()
    preferences = {'setup_version':2,'automation_mode':'startup','custom_choice':True,
                   **{'retained_' + str(index):index for index in range(88)}}
    raw = (json.dumps(preferences,indent=3) + '\n').encode()
    (data / 'preferences.json').write_bytes(raw)
    locator = (json.dumps({'schema':1,'data_dir':str(data)},indent=1) + '\n').encode()
    (installation / 'Optimus.location.json').write_bytes(locator)
    (installation / 'Optimus.exe').write_bytes(pe())
    native = Native(installation / 'Optimus.exe')
    transport = Transport()
    launchers = SimpleNamespace(retarget=Mock(return_value={'status':'completed','outcomeKnown':True,'running':False,
        'changed':[],'manual_action_required':False,'preferences_written':False}))
    service = module.SelfUpdater(SimpleNamespace(data_dir=data),installation=installation,current_version='0.11.6',
        transport=transport,native=native,system='win32',machine='AMD64',owner='fixture-owner',clock=lambda:1000,
        launchers=launchers,key_codec=lambda raw,decrypt:raw)
    return SimpleNamespace(service=service,transport=transport,native=native,launchers=launchers,base=base,
                           data=data,installation=installation,raw=raw,locator=locator,preferences=preferences)


def staged(p):
    check = p.service.check()
    assert check['status'] == 'available'
    return p.service.stage(check['offer'],confirmed=True)


def handoff(p,cleanup=True):
    value = staged(p)
    prepared = p.service.prepare_handoff(value['stage_id'],cleanup_approved=cleanup,
        approved_settings=p.preferences if cleanup else None)
    assert prepared['worker_command'] == [str(p.installation / 'Optimus.exe'),'--self-update-worker',value['stage_id']]
    assert p.service.dispatch_worker(value['stage_id'])['status'] == 'dispatched'
    return value['stage_id']


def new_service(p):
    return module.SelfUpdater(SimpleNamespace(data_dir=p.data),installation=p.base / 'Releases' / '0.11.7',
        current_version='0.11.7',system='win32',machine='AMD64',owner='fixture-owner',clock=lambda:1000,
        key_codec=lambda raw,decrypt:raw)


def test_constructor_is_inert_and_readonly_check_does_not_create_update_state(updater):
    p=updater
    assert not p.transport.requests and not p.service.directory.exists()
    result=p.service.check()
    assert result['status']=='available' and result['automatic_install_supported'] is True
    assert p.transport.requests==[module.API + '/releases/latest']
    assert not p.service.directory.exists() and not p.native.launches


@pytest.mark.parametrize('system,machine,name',[
    ('win32','AMD64','Optimus-Windows-x64.zip'),('darwin','arm64','Optimus-macOS-arm64.zip'),
    ('darwin','x86_64','Optimus-macOS-x64.zip'),('linux','x86_64','Optimus-Linux-x86_64.tar.gz')])
def test_exact_public_asset_mapping(system,machine,name):
    assert module.asset_name(system,machine)==name


@pytest.mark.parametrize('system,machine',[('linux','arm64'),('win32','x86'),('unknown','x86_64')])
def test_unsupported_architecture_makes_no_network_request(updater,system,machine):
    p=updater;p.service.system,p.service.machine=system,machine
    assert p.service.check()['status']=='unavailable' and not p.transport.requests


@pytest.mark.parametrize('system,machine,name',[('darwin','arm64','Optimus-macOS-arm64.zip'),
    ('darwin','x86_64','Optimus-macOS-x64.zip'),('linux','x86_64','Optimus-Linux-x86_64.tar.gz')])
def test_native_platforms_offer_correct_manual_download_without_install_claim(updater,system,machine,name):
    p=updater;p.service.system,p.service.machine=system,machine
    asset=p.transport.release['assets'][0]
    asset['name']=name;asset['browser_download_url']=module.WEB + '/releases/download/v0.11.7/' + name
    result=p.service.check()
    assert result['status']=='available' and result['automatic_install_supported'] is False
    assert result['offer']['asset']['name']==name
    with pytest.raises(ValueError,match='managed'):
        p.service.stage(result['offer'],confirmed=True)
    assert not p.native.launches


@pytest.mark.parametrize('field,value',[('draft',True),('prerelease',True),('tag_name','v0.11.7-beta'),
    ('url','https://api.github.com/repos/elsewhere/Optimus/releases/17'),('html_url','http://github.com/Ag3nt47/Optimus')])
def test_wrong_repo_unstable_or_non_tls_release_is_not_an_offer(updater,field,value):
    p=updater;p.transport.release[field]=value
    assert p.service.check()['status']=='unavailable'


@pytest.mark.parametrize('version',['0.11.6','0.11.5','0.9.99'])
def test_current_or_older_public_release_never_downgrades(updater,version):
    p=updater;p.transport.release,_=release(version=version)
    assert p.service.check()['status']=='up_to_date'
    assert not p.service.directory.exists()


@pytest.mark.parametrize('field,value',[('id',True),('size',True),('digest','sha256:bad'),('state','new'),
    ('url','https://api.github.com/repos/elsewhere/Optimus/releases/assets/1'),
    ('browser_download_url','https://evil.example/Optimus.zip')])
def test_asset_metadata_is_exactly_bound(updater,field,value):
    p=updater;p.transport.release['assets'][0][field]=value
    assert p.service.check()['status']=='unavailable'


def test_yes_stages_complete_side_by_side_without_preferences_or_running_app_changes(updater):
    p=updater;result=staged(p)
    assert result['status']=='staged' and len(result['stage_id'])==32
    assert not (p.base / 'Releases' / '0.11.7').exists() and not p.native.launches
    payload=p.service.directory / result['stage_id'] / 'payload'
    assert (payload / 'Optimus.location.json').read_bytes()==p.locator
    assert (p.data / 'preferences.json').read_bytes()==p.raw and len(json.loads(p.raw))==91
    assert (p.installation / 'Optimus.exe').read_bytes()==pe()
    if os.name!='nt':
        import stat
        assert stat.S_IMODE((p.service.directory / 'owner.key').stat().st_mode)==0o600


@pytest.mark.parametrize('confirmed',[False,None,1,'yes'])
def test_no_or_implicit_consent_cannot_stage(updater,confirmed):
    p=updater;offer=p.service.check()['offer'];p.transport.requests=[]
    with pytest.raises(ValueError,match='Choose Yes'):p.service.stage(offer,confirmed=confirmed)
    assert not p.transport.requests and not p.service.directory.exists()


def test_offer_refresh_cannot_expand_to_changed_asset_without_new_yes(updater):
    p=updater;offer=p.service.check()['offer'];p.transport.release['assets'][0]['id']=9
    p.transport.release['assets'][0]['url']=module.API + '/releases/assets/9'
    with pytest.raises(ValueError,match='identity changed'):p.service.stage(offer,confirmed=True)
    assert not p.service.directory.exists()


@pytest.mark.parametrize('identity',['../../other',None,True,-1])
def test_malformed_offer_id_is_rejected_before_network(updater,identity):
    with pytest.raises(ValueError):updater.service.stage({'release_id':identity},confirmed=True)
    assert not updater.transport.requests


def test_digest_and_checksum_disagreement_prevents_extraction(updater):
    p=updater
    sums=p.transport.data['SHA256SUMS.txt'].replace(hashlib.sha256(p.transport.data['Optimus-Windows-x64.zip']).hexdigest().encode(),b'a'*64)
    p.transport.data['SHA256SUMS.txt']=sums
    p.transport.release['assets'][1].update(size=len(sums),digest='sha256:' + hashlib.sha256(sums).hexdigest())
    with pytest.raises(ValueError,match='disagree'):staged(p)
    assert not list(p.service.directory.glob('*/payload/Optimus.exe'))


def test_modified_download_fails_independent_github_digest_readback(updater):
    p=updater;offer=p.service.check()['offer']
    p.transport.data['Optimus-Windows-x64.zip']+=b'tampered'
    with pytest.raises(ValueError):p.service.stage(offer,confirmed=True)
    assert not p.native.launches


@pytest.mark.parametrize('path',['Optimus/../escape.exe','/Optimus/absolute','Optimus/a:stream',
    r'Optimus\bad.exe','Optimus/CON.txt','Optimus/trailing.','Optimus/Data/preferences.json','Optimus/Optimus.location.json'])
def test_unsafe_or_private_state_archive_entries_never_escape(updater,path):
    p=updater;p.transport=Transport(archive(extra=[(path,b'bad')])) ;p.service.transport=p.transport
    with pytest.raises(ValueError):staged(p)
    assert (p.data / 'preferences.json').read_bytes()==p.raw and not (p.base / 'Releases' / '0.11.7').exists()


def test_symlink_and_casefold_duplicates_are_refused(updater):
    for kind in ('link','duplicate'):
        p=updater;target=io.BytesIO()
        with zipfile.ZipFile(target,'w') as writer:
            if kind=='link':
                entry=zipfile.ZipInfo('Optimus/link');entry.external_attr=(0o120777 << 16);writer.writestr(entry,'outside')
            else:
                writer.writestr('Optimus/File.dll',b'one');writer.writestr('Optimus/file.dll',b'two')
        p.service.transport=Transport(target.getvalue())
        with pytest.raises(ValueError):staged(p)


@pytest.mark.parametrize('limit,value',[('_MAX_FILES',2),('_MAX_EXPANDED',10)])
def test_zip_bomb_limits_are_checked_before_extract(updater,monkeypatch,limit,value):
    monkeypatch.setattr(module,limit,value)
    with pytest.raises(ValueError,match='bounds'):staged(updater)


def test_central_directory_bomb_is_rejected_before_zipfile_allocates(tmp_path,monkeypatch):
    raw=bytearray(archive());offset=raw.rfind(b'PK\x05\x06')
    struct.pack_into('<H',raw,offset+8,module._MAX_FILES+1)
    struct.pack_into('<H',raw,offset+10,module._MAX_FILES+1)
    path=tmp_path / 'bomb.zip';path.write_bytes(raw)
    payload=tmp_path / 'payload';payload.mkdir()
    opened=Mock(side_effect=AssertionError('Central directory must not be parsed'))
    monkeypatch.setattr(module.zipfile,'ZipFile',opened)
    with pytest.raises(ValueError,match='directory'):module.extract_windows(path,payload)
    opened.assert_not_called()


@pytest.mark.parametrize('binary',[b'not a native executable',pe(0xAA64)])
def test_wrong_platform_executable_is_not_staged(updater,binary):
    p=updater;p.service.transport=Transport(archive(executable=binary))
    with pytest.raises(ValueError):staged(p)


def test_staged_file_tamper_invalidates_handoff(updater):
    p=updater;stage=staged(p)
    (p.service.directory / stage['stage_id'] / 'payload' / 'Optimus.exe').write_bytes(pe()+b'changed')
    with pytest.raises(ValueError,match='staged native files'):p.service.prepare_handoff(stage['stage_id'])
    assert not p.native.launches


def test_configuration_change_and_first_setup_cannot_gain_cleanup_approval(updater):
    p=updater;stage=staged(p)
    with pytest.raises(ValueError,match='explicitly approved'):
        p.service.prepare_handoff(stage['stage_id'],cleanup_approved=True,approved_settings={})
    (p.data / 'preferences.json').write_bytes(p.raw+b' ')
    with pytest.raises(ValueError,match='configuration'):p.service.prepare_handoff(stage['stage_id'])


def test_exact_update_waits_and_preserves_old_version_then_consumes_clean_once(updater):
    p=updater;identity=handoff(p)
    result=p.service.finish_handoff(identity)
    assert result['status']=='launched' and result['started'] is True
    target=p.base / 'Releases' / '0.11.7'
    assert target.is_dir() and p.installation.is_dir()
    assert (target / 'Optimus.location.json').read_bytes()==p.locator
    assert (p.data / 'preferences.json').read_bytes()==p.raw
    assert p.native.launches[-1]==(str(target / 'Optimus.exe'),['--self-update-resume',identity],{'background':False})
    assert p.launchers.retarget.call_count==1
    accepted=new_service(p).consume_resume(identity)
    assert accepted['status']=='accepted' and accepted['consumed'] is True and accepted['cleanup_approved'] is True
    assert accepted['settings']==p.preferences
    with pytest.raises(FileExistsError):new_service(p).consume_resume(identity)
    assert (p.data / 'preferences.json').read_bytes()==p.raw


def test_setup_resume_never_approves_cleanup(updater):
    p=updater
    p.preferences['setup_version']=0;p.raw=json.dumps(p.preferences).encode();(p.data / 'preferences.json').write_bytes(p.raw)
    identity=handoff(p,cleanup=False);p.service.finish_handoff(identity)
    result=new_service(p).consume_resume(identity)
    assert result['cleanup_approved'] is False and result['settings']['setup_version']==0


@pytest.mark.parametrize('kind',['owner','expired','preferences','executable','wrong_version','signature'])
def test_resume_tampering_or_scope_changes_cannot_start_cleanup(updater,kind):
    p=updater;identity=handoff(p);p.service.finish_handoff(identity);service=new_service(p)
    if kind=='owner':service.owner='other-owner'
    elif kind=='expired':service.clock=lambda:999999
    elif kind=='preferences':(p.data / 'preferences.json').write_bytes(p.raw+b' ')
    elif kind=='executable':(service.installation / 'Optimus.exe').write_bytes(pe()+b'changed')
    elif kind=='wrong_version':service.installation=p.installation;service.current_version='0.11.6'
    else:
        path=p.service.directory / identity / 'ready.json';value=json.loads(path.read_text());value['value']['cleanup_approved']=False;path.write_text(json.dumps(value))
    with pytest.raises((ValueError,OSError)):service.consume_resume(identity)
    assert not (p.service.directory / identity / 'resume-consumed.json').exists()


def test_dispatch_and_worker_are_each_permanently_at_most_once(updater):
    p=updater;identity=handoff(p)
    with pytest.raises(FileExistsError):p.service.dispatch_worker(identity)
    assert len(p.native.launches)==1
    p.service.finish_handoff(identity)
    with pytest.raises(FileExistsError):p.service.finish_handoff(identity)
    assert len(p.native.launches)==2


@pytest.mark.parametrize('phase',['stage','prepared','dispatched','ready'])
def test_durable_cancellation_prevents_late_handoff_or_cleanup_resume(updater,phase):
    p=updater
    result=staged(p);identity=result['stage_id']
    if phase!='stage':p.service.prepare_handoff(identity,cleanup_approved=True,approved_settings=p.preferences)
    if phase in ('dispatched','ready'):p.service.dispatch_worker(identity)
    if phase=='ready':assert p.service.finish_handoff(identity)['status']=='launched'
    assert p.service.cancel_handoff(identity)['revoked'] is True
    assert p.service.cancel_handoff(identity)['revoked'] is True
    if phase=='stage':
        with pytest.raises(RuntimeError,match='cancelled'):p.service.prepare_handoff(identity)
    elif phase=='prepared':
        with pytest.raises(RuntimeError,match='cancelled'):p.service.dispatch_worker(identity)
    elif phase=='dispatched':
        with pytest.raises(RuntimeError,match='cancelled'):p.service.finish_handoff(identity)
    else:
        with pytest.raises(RuntimeError,match='cancelled'):new_service(p).consume_resume(identity)
    assert not (p.service.directory / identity / 'resume-consumed.json').exists()


def test_cancel_arriving_during_dispatch_is_seen_before_worker_actions(updater):
    p=updater;value=staged(p);identity=value['stage_id']
    p.service.prepare_handoff(identity,cleanup_approved=True,approved_settings=p.preferences)
    def launch_and_cancel(*args,**kwargs):
        p.service.cancel_handoff(identity)
        return {'started':True,'pid':5678}
    p.native.launch=launch_and_cancel
    assert p.service.dispatch_worker(identity)['status']=='dispatched'
    with pytest.raises(RuntimeError,match='cancelled'):p.service.finish_handoff(identity)
    assert not (p.base / 'Releases' / '0.11.7').exists()


def test_signed_records_cannot_be_substituted_between_protocol_phases(updater):
    p=updater;identity=handoff(p);p.service.finish_handoff(identity)
    folder=p.service.directory / identity
    (folder / 'ready.json').write_bytes((folder / 'handoff.json').read_bytes())
    with pytest.raises(ValueError,match='phase'):new_service(p).consume_resume(identity)


def test_future_setup_schema_requires_fresh_setup_not_old_cleanup_consent(updater,monkeypatch):
    p=updater;identity=handoff(p);p.service.finish_handoff(identity)
    monkeypatch.setattr(module,'CURRENT_SETUP_VERSION',3)
    result=new_service(p).consume_resume(identity)
    assert result['cleanup_approved'] is False and result['settings']['setup_version']==2


def test_changed_launch_locator_or_missing_executable_is_manual_before_yes(updater):
    p=updater;(p.installation / 'Optimus.exe').unlink()
    result=p.service.check()
    assert result['status']=='available' and result['automatic_install_supported'] is False


def test_expired_stage_never_dispatches_or_extends_approval(updater):
    p=updater;value=staged(p);p.service.clock=lambda:2801
    with pytest.raises(ValueError,match='expired'):p.service.prepare_handoff(value['stage_id'])
    assert not p.native.launches


@pytest.mark.parametrize('phase',['stage','prepared','dispatched','ready'])
def test_expired_consent_can_only_be_durably_revoked(updater,phase):
    p=updater;identity=staged(p)['stage_id']
    if phase!='stage':p.service.prepare_handoff(identity,cleanup_approved=True,approved_settings=p.preferences)
    if phase in ('dispatched','ready'):p.service.dispatch_worker(identity)
    if phase=='ready':assert p.service.finish_handoff(identity)['status']=='launched'
    launch_count=len(p.native.launches)
    p.service.clock=lambda:2801
    assert p.service.cancel_handoff(identity)=={'status':'cancelled','revoked':True,'cleanup_approved':False}
    assert p.service.cancel_handoff(identity)['revoked'] is True
    revoked=json.loads((p.service.directory / identity / 'revoked.json').read_bytes())['value']
    assert revoked['expires_at']==2800 and revoked['cleanup_approved'] is False
    with pytest.raises(ValueError,match='expired'):p.service.prepare_handoff(identity)
    if phase!='stage':
        with pytest.raises(ValueError,match='expired'):p.service.dispatch_worker(identity)
    if phase=='ready':
        resumed=new_service(p);resumed.clock=lambda:2801
        with pytest.raises(ValueError,match='expired'):resumed.consume_resume(identity)
    assert len(p.native.launches)==launch_count
    assert not (p.service.directory / identity / 'resume-consumed.json').exists()


def test_live_gui_is_waited_for_without_killing_or_new_launch(updater,monkeypatch):
    p=updater;identity=handoff(p);p.native.living=True
    state={'clock':0}
    monkeypatch.setattr(module.time,'monotonic',lambda:state['clock'])
    p.service.sleep=lambda _:state.update(clock=state['clock']+1)
    result=p.service.finish_handoff(identity)
    assert result['status']=='needs_review' and result['started'] is False
    assert len(p.native.launches)==1 and not (p.base / 'Releases' / '0.11.7').exists()


def test_busy_maintenance_lease_does_not_publish_or_launch(updater,monkeypatch):
    p=updater;identity=handoff(p);state={'clock':0}
    monkeypatch.setattr(module.time,'monotonic',lambda:state['clock'])
    p.service.sleep=lambda _:state.update(clock=state['clock']+1)
    with RunLease(p.data):result=p.service.finish_handoff(identity)
    assert result['status']=='needs_review' and len(p.native.launches)==1


def test_other_gui_or_unknown_launcher_outcome_keeps_resume_unavailable(updater):
    p=updater;identity=handoff(p);p.native.others=[999]
    result=p.service.finish_handoff(identity)
    assert result['status']=='needs_review' and len(p.native.launches)==1
    assert not (p.service.directory / identity / 'ready.json').exists()


def test_unknown_launcher_outcome_preserves_old_data_and_never_blindly_restarts(updater):
    p=updater;identity=handoff(p)
    p.launchers.retarget.return_value.update(outcomeKnown=False)
    result=p.service.finish_handoff(identity)
    assert result['status']=='needs_review' and len(p.native.launches)==1
    assert (p.data / 'preferences.json').read_bytes()==p.raw
    assert (p.installation / 'Optimus.location.json').read_bytes()==p.locator
    assert not (p.service.directory / identity / 'ready.json').exists()


def test_release_modified_during_launcher_integration_is_never_executed(updater):
    p=updater;identity=handoff(p)
    def alter(*args,**kwargs):
        (Path(args[1]).parent / 'extra.dll').write_bytes(b'changed after publication')
        return {'outcomeKnown':True,'running':False,'status':'completed'}
    p.launchers.retarget.side_effect=alter
    assert p.service.finish_handoff(identity)['status']=='needs_review'
    assert len(p.native.launches)==1


def test_native_launch_uses_only_exact_argv_and_no_shell_or_elevation(monkeypatch,tmp_path):
    process=Mock(pid=123)
    popen=Mock(return_value=process);monkeypatch.setattr(module.subprocess,'Popen',popen)
    native=module.NativeUpdateProcess()
    executable=tmp_path / 'Optimus.exe'
    assert native.launch(executable,['--self-update-worker','a'*32],background=True)['started'] is True
    assert popen.call_args.args[0]==[str(executable),'--self-update-worker','a'*32]
    assert popen.call_args.kwargs['shell'] is False and 'runas' not in str(popen.call_args)
    monkeypatch.setenv('OPTIMUS_SOURCE_WORKSPACE','edited-source')
    with pytest.raises(ValueError,match='overrides'):native.launch(executable,[])
    assert popen.call_count==1


@pytest.mark.parametrize('url',['http://github.com/x','https://github.com.evil.example/x','https://user@github.com/x','https://github.com:444/x'])
def test_redirects_never_leave_tls_allowlist(url):
    with pytest.raises(ValueError):module._tls(url)


def test_public_metadata_trickle_has_absolute_time_budget(monkeypatch):
    state={'clock':0}
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def geturl(self):return module.API + '/releases/latest'
        def read1(self,size):state['clock']+=16;return b' '
    transport=module.GitHubTransport();transport._open=Mock(return_value=Response())
    monkeypatch.setattr(module.time,'monotonic',lambda:state['clock'])
    with pytest.raises(RuntimeError,match='bounded'):transport.get_json(module.API + '/releases/latest')
    assert state['clock']==32


def test_transport_digest_size_progress_and_no_token_headers_are_verified(tmp_path):
    content=b'fixture download'
    class Response(io.BytesIO):
        def geturl(self):return 'https://release-assets.githubusercontent.com/fixture?token=never-persist'
    transport=module.GitHubTransport();transport._open=Mock(return_value=Response(content))
    progress=[]
    asset={'name':'Optimus-Windows-x64.zip','api_url':module.API + '/releases/assets/1','size':len(content),'sha256':hashlib.sha256(content).hexdigest()}
    transport.download(asset,tmp_path / 'fixture.zip',progress_callback=progress.append)
    assert progress[-1]['bytes_received']==len(content) and progress[-1]['bytes_total']==len(content)
    assert 'token' not in json.dumps(progress)


def test_worker_main_has_only_known_cli_exit_codes(monkeypatch):
    from optimus import filesystem
    monkeypatch.setattr(filesystem,'default_data_root',lambda:Path('fixture-only'))
    monkeypatch.setattr(module,'SelfUpdater',Mock(return_value=SimpleNamespace(finish_handoff=lambda _: {'status':'launched'})))
    assert module.worker_main('a'*32)==0
    monkeypatch.setattr(module,'SelfUpdater',Mock(side_effect=ValueError('fixture refusal')))
    assert module.worker_main('a'*32)==2
