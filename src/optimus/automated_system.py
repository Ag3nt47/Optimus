"""Saved exact startup approvals and bounded Windows Update driver policies.

Loading this module does not query Windows or change settings. Callers persist
only normalized descriptors collected from a fresh user-reviewed inventory.
"""
from __future__ import annotations
import copy
from datetime import datetime
import json
import re
import uuid
from .automation import CURRENT_SETUP_VERSION

_HEX64 = re.compile(r'^[0-9a-fA-F]{64}$')
_STARTUP_ID = re.compile(r'^(Registry|Shortcut)-[0-9a-fA-F]{64}$')
_STARTUP_FIELDS = ('Id','Name','Source','Command','Location','EntryType','Fingerprint','Hive','RegistryView','RegistryPath','ValueName','ValueKind','RawValue','FilePath','BackupId')
_DRIVER_TEXT_FIELDS = ('hardwareId','provider','driverClass','manufacturer','model','driverDate')


def _text(value, label, maximum=32768, *, empty=False):
    if not isinstance(value, str) or (not value and not empty) or len(value) > maximum or '\x00' in value:
        raise ValueError('Invalid ' + label + '.')
    return value


def _list(values, label, maximum=100):
    if type(values) is not list or len(values) > maximum:
        raise ValueError(label + ' must contain at most ' + str(maximum) + ' exact approvals.')
    if len(json.dumps(values, ensure_ascii=True, allow_nan=False)) > 1024 * 1024:
        raise ValueError(label + ' exceeds its saved approval limit.')
    return values


def _bing(record):
    text = ' '.join(str(record.get(key, '')) for key in ('Name','Command','FilePath','Location','name','title'))
    return 'bingwallpaper' in re.sub('[^a-z0-9]', '', text.casefold())


def startup_descriptor(record):
    if not isinstance(record, dict):
        raise ValueError('A startup approval must be an inventory record.')
    if record.get('Enabled') is not True or record.get('CanManage') is not True or record.get('Protected') is True or _bing(record):
        raise ValueError('Only enabled, manageable, unprotected startup entries can be approved; Bing Wallpaper is preserved.')
    result = {key: copy.deepcopy(record[key]) for key in _STARTUP_FIELDS if key in record}
    for key in ('Id','Name','Source','Command','Location','EntryType','Fingerprint'):
        _text(result.get(key), 'startup ' + key)
    if not _STARTUP_ID.fullmatch(result['Id']) or not _HEX64.fullmatch(result['Fingerprint']):
        raise ValueError('The startup record has no supported stable identity or fingerprint.')
    result['Fingerprint'] = result['Fingerprint'].upper()
    if result['EntryType'] == 'Registry':
        if not result['Id'].startswith('Registry-') or result.get('Hive') not in ('CurrentUser','LocalMachine') or result.get('RegistryView') not in ('Registry32','Registry64') or result.get('RegistryPath') != r'Software\Microsoft\Windows\CurrentVersion\Run':
            raise ValueError('Only exact supported Windows Run registrations can be approved.')
        for key in ('ValueName','ValueKind'):
            _text(result.get(key), 'startup ' + key)
        kind, raw = result['ValueKind'], result.get('RawValue')
        if kind in ('String','ExpandString'):
            _text(raw, 'raw startup command', empty=True)
        elif kind in ('DWord','QWord'):
            if type(raw) is not int:raise ValueError('Invalid raw startup number.')
        elif kind in ('Binary','None'):
            if type(raw) is not list or len(raw)>32768 or any(type(value) is not int or not 0<=value<=255 for value in raw):raise ValueError('Invalid raw startup bytes.')
        elif kind == 'MultiString':
            if type(raw) is not list or len(raw)>100 or any(not isinstance(value,str) or len(value)>32768 for value in raw):raise ValueError('Invalid raw startup strings.')
        else:raise ValueError('Unsupported raw startup value kind.')
    elif result['EntryType'] == 'Shortcut':
        if not result['Id'].startswith('Shortcut-') or not _text(result.get('FilePath'), 'startup shortcut').casefold().endswith('.lnk'):
            raise ValueError('Only exact Startup-folder shortcuts can be approved.')
    else:
        raise ValueError('Unsupported startup entry type.')
    result.update(Enabled=True, CanManage=True, Protected=False)
    if len(json.dumps(result, ensure_ascii=True, allow_nan=False)) > 65536:
        raise ValueError('The startup approval is too large.')
    return result


def validate_startup_approvals(values):
    result = [startup_descriptor(value) for value in _list(values, 'Startup approvals')]
    if len({value['Id'].casefold() for value in result}) != len(result):
        raise ValueError('Duplicate startup approvals are not allowed.')
    return result


def driver_descriptor(record):
    if not isinstance(record, dict):raise ValueError('A driver approval must be an inventory record.')
    try:identity=str(uuid.UUID(record.get('id')))
    except (ValueError,TypeError,AttributeError):raise ValueError('Invalid Windows Update driver identity.') from None
    revision=record.get('revision')
    if type(revision) is not int or not 0<=revision<=2147483647:raise ValueError('Invalid Windows Update revision.')
    result={'id':identity,'revision':revision,'title':_text(record.get('title'),'driver title',2048)}
    for key in _DRIVER_TEXT_FIELDS:
        if key in record and record[key] not in (None,''):
            result[key]=_text(record[key], 'driver '+key, 4096)
    for key in ('canRequestUserInput','eulaAccepted'):
        if key in record and record[key] is not None:
            if type(record[key]) is not bool:raise ValueError('Invalid driver '+key+'.')
            result[key]=record[key]
    if 'rebootBehavior' in record and record['rebootBehavior'] is not None:
        if type(record['rebootBehavior']) is not int or record['rebootBehavior'] not in (0,1,2):raise ValueError('Invalid driver reboot behavior.')
        result['rebootBehavior']=record['rebootBehavior']
    return result


def validate_driver_approvals(values):
    result=[driver_descriptor(value) for value in _list(values, 'Driver approvals')]
    if len({(value['id'],value['revision']) for value in result})!=len(result):raise ValueError('Duplicate driver approvals are not allowed.')
    return result


def _date(value):
    value=_text(value,'driver date',128)
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).date().isoformat()
    except ValueError:raise ValueError('Driver matching requires a known ISO driver date.') from None


def driver_device_rule(record):
    value=driver_descriptor(record)
    result={'hardwareId':value.get('hardwareId'),'provider':value.get('provider'),'class':value.get('driverClass'),
            'notOlderThanDriverDate':_date(value.get('driverDate'))}
    return _device_rule(result)


def _device_rule(value):
    if not isinstance(value,dict) or set(value)!= {'hardwareId','provider','class','notOlderThanDriverDate'}:raise ValueError('A driver device rule requires exact hardwareId, provider, class and driver date floor.')
    result={key:_text(value[key], 'driver device '+key,4096).strip() for key in ('hardwareId','provider','class')}
    if any(not value or any(char in value for char in '*?\r\n') for value in result.values()):raise ValueError('Driver device rules cannot be empty, wildcard or multiline matches.')
    result['notOlderThanDriverDate']=_date(value['notOlderThanDriverDate'])
    return result

def validate_driver_devices(values):
    result=[_device_rule(value) for value in _list(values, 'Driver device rules', maximum=2048)]
    if len({tuple(value[key].casefold() for key in ('hardwareId','provider','class')) for value in result})!=len(result):raise ValueError('Duplicate driver device rules are not allowed.')
    return result


def _cancel(event):return event is not None and event.is_set()


def _finish(policy,items,**extra):
    return {'policy':policy,'items':items,'failed_items':sum(value['status']=='failed' for value in items),'skipped_items':sum(value['status']=='skipped' for value in items),**extra}


def run_startup(backend,config,cancel_event=None,item_callback=None):
    policy=config.get('startup_policy','review')
    if config.get('startup_review_enabled') is not True:return _finish(policy,[],skipped=True,review_only=True,reason='Startup maintenance is off; saved approvals are inactive.')
    if policy not in ('review','disable_selected'):raise ValueError('Unsupported startup policy.')
    if policy!='review' and config.get('setup_version')!=CURRENT_SETUP_VERSION:raise ValueError('Complete the current setup before automatic startup changes.')
    approvals=validate_startup_approvals(config.get('startup_approved_entries',[]))
    if policy=='disable_selected' and (config.get('startup_changes_approved') is not True or not approvals):raise ValueError('Startup changes require explicit selected-entry approval.')
    if _cancel(cancel_event):return _finish(policy,[],cancelled=True)
    fresh=backend.startup_list()
    if not isinstance(fresh,list):raise RuntimeError('Invalid fresh startup inventory.')
    if policy=='review':return _finish(policy,[],review_only=True,entries=fresh)
    items=[]
    for index,approved in enumerate(approvals):
        if _cancel(cancel_event):break
        binding={'id':approved['Id'],'name':approved['Name'],'fingerprint':approved['Fingerprint']}
        matches=[value for value in fresh if isinstance(value,dict) and value.get('Id')==approved['Id']]
        reason=None
        if len(matches)!=1:reason='The approved entry is missing or ambiguous.'
        elif matches[0].get('Enabled') is False:reason='The approved entry is already disabled; no repeated change was made.'
        else:
            try:
                current=startup_descriptor(matches[0])
                if current!=approved:reason='The startup identity, command, source, location or fingerprint changed; approve it again.'
            except ValueError:reason='The entry is now protected, unmanageable or has changed identity.'
        if reason:
            items.append({'binding':binding,'status':'skipped','reason':reason});continue
        if item_callback:item_callback({'operation':'startup','current':index+1,'index':index+1,'total':len(approvals),'description':approved['Name'],'binding':binding})
        if _cancel(cancel_event):break
        try:
            outcome=backend.startup_set(matches[0],False)
            items.append({'binding':binding,'status':'applied','result':outcome})
        except Exception as error:items.append({'binding':binding,'status':'failed','error':str(error)})
    return _finish(policy,items,cancelled=_cancel(cancel_event),review_only=False,note='Only approved unchanged entries were disabled with existing startup backups; no entry was deleted.')


def run_drivers(backend,config,cancel_event=None,item_callback=None):
    policy=config.get('driver_policy','review')
    if config.get('driver_updates_enabled') is not True:return _finish(policy,[],skipped=True,review_only=True,reason='Driver maintenance is off; saved approvals are inactive.')
    if policy not in ('review','install_approved','install_matching'):raise ValueError('Unsupported driver policy.')
    if policy!='review' and config.get('setup_version')!=CURRENT_SETUP_VERSION:raise ValueError('Complete the current setup before automatic driver changes.')
    approvals=validate_driver_approvals(config.get('driver_approved_updates',[]))
    rules=validate_driver_devices(config.get('driver_approved_devices',[]))
    if policy!='review':
        if config.get('driver_changes_approved') is not True or config.get('driver_update_agreements_accepted') is not True:raise ValueError('Driver changes and license agreements require explicit saved approval.')
        if not (approvals if policy=='install_approved' else rules):raise ValueError('Choose exact driver updates or device/provider/class rules before automation.')
        if not backend.is_admin():return _finish(policy,[],skipped=True,reason='Administrator access is required; no elevation or driver action was started.')
        readiness = getattr(backend, 'update_disk_readiness', None)
        if callable(readiness):
            space = readiness()
            if not isinstance(space, dict) or space.get('ready') is not True:
                return _finish(policy, [], skipped=True, reason='System-drive installation space is insufficient or unverified; driver updates were deferred.', disk_readiness=space)
    if _cancel(cancel_event):return _finish(policy,[],cancelled=True)
    scan=backend.driver_updates_scan()
    if not isinstance(scan,dict) or type(scan.get('resultCode')) is not int or scan['resultCode']!=2 or not isinstance(scan.get('updates'),list):
        return _finish(policy,[{'binding':{'stage':'driver-scan'},'status':'failed','error':'Windows Update did not return a complete successful fresh driver scan.','result':scan}],skipped=True,scan=scan)
    if policy=='review':return _finish(policy,[],review_only=True,scan=scan)
    fresh=[]
    for value in scan['updates']:
        try:fresh.append(driver_descriptor(value))
        except ValueError:continue
    counts={}
    for value in fresh:
        key=(value['id'],value['revision']);counts[key]=counts.get(key,0)+1
    items=[];selected=[]
    if policy=='install_approved':
        for approved in approvals:
            matches=[value for value in fresh if (value['id'],value['revision'])==(approved['id'],approved['revision'])]
            if len(matches)!=1 or any(matches[0].get(key)!=value for key,value in approved.items() if key not in ('eulaAccepted','canRequestUserInput','rebootBehavior')):
                items.append({'binding':approved,'status':'skipped','reason':'The approved update or its title/device/provider metadata changed or is no longer applicable.'})
            else:selected.append(matches[0])
    else:
        for value in fresh:
            try:rule=driver_device_rule(value)
            except ValueError:continue
            matches=[approved for approved in rules if all(rule[key].casefold()==approved[key].casefold() for key in ('hardwareId','provider','class'))]
            if matches:
                floor=matches[0]['notOlderThanDriverDate']
                if rule['notOlderThanDriverDate'] < floor:
                    items.append({'binding':value,'status':'skipped','reason':'The offered driver predates the approved device-rule date floor.'})
                else:
                    value=dict(value,approvalMode='matching',notOlderThanDriverDate=floor)
                    selected.append(value)
    eligible=[]
    for value in selected:
        if counts[(value['id'],value['revision'])]!=1 or value.get('canRequestUserInput') is not False or value.get('rebootBehavior') not in (0,2):
            items.append({'binding':value,'status':'skipped','reason':'Ambiguous, interactive, unknown, or restart-requiring updates are excluded from unattended installation.'})
        else:eligible.append(value)
    if not eligible or _cancel(cancel_event):return _finish(policy,items,cancelled=_cancel(cancel_event),review_only=False)
    backup=backend.driver_backup_automatic()
    if not isinstance(backup,dict) or type(backup.get('exit_code')) is not int or backup['exit_code']!=0 or not isinstance(backup.get('destination'),str) or not backup['destination']:
        return _finish(policy,items+[{'status':'failed','binding':{'stage':'driver-backup'},'result':backup,'error':'A successful third-party driver backup is required before installation.'}],backup=backup)
    processed=0
    for index,value in enumerate(eligible):
        if _cancel(cancel_event):break
        if item_callback:item_callback({'operation':'driver','current':index+1,'index':index+1,'total':len(eligible),'description':value['title'],'binding':value})
        if _cancel(cancel_event):break
        processed=index+1
        try:
            outcome=backend.driver_update_apply_unattended(value,accept_eula=True)
            success=(isinstance(outcome,dict) and type(outcome.get('resultCode')) is int and outcome['resultCode']==2
                     and outcome.get('hresult',0)==0 and outcome.get('aggregateResultCode',2)==2 and outcome.get('aggregateHresult',0)==0)
            items.append({'binding':value,'status':'applied' if success else 'failed','result':outcome})
            if not success or outcome.get('restartNeeded') is True:break
        except Exception as error:
            items.append({'binding':value,'status':'failed','error':str(error),'uncertain':True})
            break
    return _finish(policy,items,cancelled=_cancel(cancel_event),review_only=False,backup=backup,deferred=eligible[processed:],note='Only approved applicable update identities or exact device/provider/class rules were used. No automatic reboot or elevation was requested.')

