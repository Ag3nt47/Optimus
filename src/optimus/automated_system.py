"""Saved exact startup approvals and bounded Windows Update driver policies.

Loading this module does not query Windows or change settings. Callers persist
only normalized descriptors collected from a fresh user-reviewed inventory.
"""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
import re
import time
import uuid
from .automation import CURRENT_SETUP_VERSION
from .item_progress import BoundItemProgress, accepts_progress_callback, normalize_item_progress
from .navigation import WorkItem, cursor_for, navigation_skip, local_skip, can_fail_forward, shared_update_refusal

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


def run_drivers(backend,config,cancel_event=None,item_callback=None,*,prepared_callback=None,before_install=None):
    """Discover immediately, then optionally wait before any backup or install.

    The owner keeps its exclusive installer gate until this call returns. The
    preparation callback fires once, including on read-only/failed paths; neither
    callback is evidence that an installer has started.
    """
    notified=False
    latest_evidence={}
    def prepared(**values):
        nonlocal notified
        if notified:return
        notified=True
        state={'operation':'driver','scan_complete':False,'candidate_count':0,'review_only':True}
        state.update(values)
        if prepared_callback is not None:prepared_callback(state)
    try:
        result = _run_drivers(backend,config,cancel_event,item_callback,prepared=prepared,
                              before_install=before_install,phased=prepared_callback is not None or before_install is not None,
                              latest_evidence=latest_evidence)
        if 'driver_scan' not in result:
            from .driver_coverage import driver_scan_evidence
            proof = driver_scan_evidence(None)
            proof.update(status='not_started', errors=[], summary='Driver search did not run. '
                         + str(result.get('reason') or 'Cancelled before driver discovery.'))
            result['driver_scan'] = proof
        return result
    except Exception as error:
        if latest_evidence:
            try:error.driver_scan=dict(latest_evidence)
            except (AttributeError,TypeError):pass
        raise
    finally:
        prepared()


def _run_drivers(backend,config,cancel_event,item_callback,*,prepared,before_install,phased,latest_evidence):
    navigation=cursor_for(backend,'driver_updates','Driver updates')
    policy=config.get('driver_policy','review')
    if config.get('driver_updates_enabled') is not True:return _finish(policy,[],skipped=True,review_only=True,reason='Driver maintenance is off; saved approvals are inactive.')
    if policy not in ('review','install_approved','install_matching'):raise ValueError('Unsupported driver policy.')
    if policy!='review' and config.get('setup_version')!=CURRENT_SETUP_VERSION:raise ValueError('Complete the current setup before automatic driver changes.')
    approvals=validate_driver_approvals(config.get('driver_approved_updates',[]))
    rules=validate_driver_devices(config.get('driver_approved_devices',[]))
    if policy!='review':
        if config.get('driver_changes_approved') is not True or config.get('driver_update_agreements_accepted') is not True:raise ValueError('Driver changes and license agreements require explicit saved approval.')
        if not (approvals if policy=='install_approved' else rules):raise ValueError('Choose exact driver updates or device/provider/class rules before automation.')
        if not phased:
            if not backend.is_admin():return _finish(policy,[],skipped=True,reason='Administrator access is required; no elevation or driver action was started.')
            readiness = getattr(backend, 'update_disk_readiness', None)
            if callable(readiness):
                space = readiness()
                if not isinstance(space, dict) or space.get('ready') is not True:
                    return _finish(policy, [], skipped=True, reason='System-drive installation space is insufficient or unverified; driver updates were deferred.', disk_readiness=space)
    approved_scope=copy.deepcopy({key:config.get(key) for key in ('setup_version','driver_updates_enabled','driver_policy',
        'driver_changes_approved','driver_update_agreements_accepted','driver_approved_updates','driver_approved_devices')})
    if _cancel(cancel_event):return _finish(policy,[],cancelled=True)
    from .driver_coverage import driver_scan_evidence
    scan_started_utc=datetime.now(timezone.utc).isoformat()
    scan_clock=time.monotonic()
    discovery=WorkItem(navigation,'driver:discovery','Search Windows Update for applicable driver updates',external=cancel_event)
    discovery_state={'operation':'driver','discovery_progress':True,'phase':'scan',
        'description':'Search Windows Update and inspect present devices'}
    if callable(item_callback):
        try:
            item_callback(dict(discovery_state,item_progress=normalize_item_progress({
                'key':'driver:discovery','label':'Driver discovery','phase':'scan','percent':None})))
        except Exception:
            pass  # Read-only progress observation does not alter scan outcomes.
    discovery_reporter=BoundItemProgress(item_callback,'driver:discovery','Driver discovery',discovery_state)
    try:
        options={}
        if callable(item_callback) and accepts_progress_callback(backend.driver_updates_scan):
            options['progress_callback']=discovery_reporter
        scan=backend.driver_updates_scan(**options)
    except Exception as error:
        discovery.finish('failed',message=str(error))
        # Preserve the existing preparation/exception contract. Owners can save
        # bounded evidence without mistaking this failed call for an empty scan.
        failed=driver_scan_evidence({'updates': [], 'resultCode': 4,
                                    'source': 'Windows Update', 'error': str(error)})
        failed.update(started_utc=scan_started_utc,
                      completed_utc=datetime.now(timezone.utc).isoformat(),
                      duration_seconds=max(0,time.monotonic()-scan_clock))
        try:error.driver_scan=failed
        except (AttributeError,TypeError):pass
        raise
    finally:
        discovery_reporter.close()
    skipped_discovery=discovery.requested()
    discovery.finish('cancelled' if skipped_discovery else 'completed',mutation_started=False)
    scan_completed_utc=datetime.now(timezone.utc).isoformat()
    scan_duration=max(0,time.monotonic()-scan_clock)
    def proof(eligible_count=None):
        value=driver_scan_evidence(scan,eligible_count=eligible_count)
        # The native WUA search times are authoritative. If its bridge failed
        # before returning them, preserve the timing of this attempted call.
        if not value['started_utc']:value['started_utc']=scan_started_utc
        if not value['completed_utc']:value['completed_utc']=scan_completed_utc
        if value['duration_seconds'] is None:value['duration_seconds']=scan_duration
        latest_evidence.clear()
        latest_evidence.update(value)
        return value
    evidence=proof()
    def finish(items, **extra):
        return _finish(policy,items,scan=scan,driver_scan=dict(evidence),**extra)
    if skipped_discovery:
        return finish([],skipped=True,review_only=True,navigation_cancelled=True,navigation_cancelled_items=1,
            installer_started=False,outcomeKnown=True,running=False,
            reason='The driver discovery item was skipped; its completed search cannot authorize installation.')
    if not evidence['complete']:
        return finish([{'binding':{'stage':'driver-scan'},'status':'failed','error':'Windows Update did not return a complete successful fresh driver scan.','result':scan,
            'installer_started':False,'outcomeKnown':True}],skipped=True,installer_started=False,outcomeKnown=True)
    if policy=='review':
        prepared(scan_complete=True)
        return finish([],review_only=True)
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
        # CanRequestUserInput describes a handler's capability, not whether a
        # quiet installation must prompt. The native bridge establishes and
        # rechecks IUpdateInstaller2.ForceQuiet with source prompts disabled;
        # an unsupported quiet handler returns a reviewable failure instead of
        # falling back to interaction. Unknown metadata still fails closed.
        if counts[(value['id'],value['revision'])]!=1 or type(value.get('canRequestUserInput')) is not bool or value.get('rebootBehavior') not in (0,2):
            items.append({'binding':value,'status':'skipped','reason':'Ambiguous updates, unknown interaction metadata, and mandatory-restart updates are excluded from unattended installation.'})
        else:eligible.append(value)
    evidence=proof(eligible_count=len(eligible))
    prepared(scan_complete=True,candidate_count=len(eligible),review_only=False)
    if not eligible or _cancel(cancel_event):return finish(items,cancelled=_cancel(cancel_event),review_only=False,installer_started=False,outcomeKnown=True)
    def deferred(reason,**extra):
        return finish(items,skipped=True,review_only=False,deferred=eligible,
            cancelled=_cancel(cancel_event),reason=reason,stop_reason=reason,
            installer_started=False,outcomeKnown=True,**extra)
    if before_install is not None:
        checkpoint=WorkItem(navigation,'driver:checkpoint','Wait for the driver installer checkpoint',external=cancel_event,kind='check')
        permitted=before_install({'operation':'driver','scan_complete':True,'candidate_count':len(eligible),'review_only':False}) is True
        skipped_checkpoint=checkpoint.requested()
        checkpoint.finish('cancelled' if skipped_checkpoint else 'completed',mutation_started=False)
        if skipped_checkpoint:
            return dict(deferred('Driver installation was skipped at your request.'),cancelled=_cancel(cancel_event),
                        navigation_cancelled=True,navigation_cancelled_items=1)
        if not permitted:
            return deferred('The maintenance or exclusive installer gate did not permit driver changes.')
    if _cancel(cancel_event):return deferred('Cancelled before driver backup or installation.')
    if phased:
        if {key:config.get(key) for key in approved_scope}!=approved_scope:
            return deferred('Saved driver approvals changed while waiting; review the current scope before installing.')
        if not backend.is_admin():return deferred('Administrator access is required; no elevation or driver action was started.')
        readiness=getattr(backend,'update_disk_readiness',None)
        if callable(readiness):
            space=readiness()
            if not isinstance(space,dict) or space.get('ready') is not True:
                return deferred('System-drive installation space is insufficient or unverified; driver updates were deferred.',disk_readiness=space)
        if _cancel(cancel_event):return deferred('Cancelled before driver backup or installation.')
        if {key:config.get(key) for key in approved_scope}!=approved_scope:
            return deferred('Saved driver approvals changed; review the current scope before installing.')
    backup_item=WorkItem(navigation,'driver:backup','Back up installed driver packages needed for these updates',external=cancel_event,kind='check')
    try:
        targeted=getattr(backend,'driver_backup_for_updates',None)
        backup=(targeted(eligible,cancel_event=backup_item.cancel_event,item_callback=item_callback)
                if callable(targeted) else backend.driver_backup_automatic())
    except Exception as error:
        backup_item.finish('failed',mutation_started=False,message=str(error))
        error.installer_started=False
        error.outcomeKnown=True
        error.running=False
        raise
    skipped_backup=backup_item.requested()
    backup_item.finish('cancelled' if skipped_backup else 'completed',mutation_started=False)
    if skipped_backup:
        return dict(deferred('Driver backup was stopped or skipped at your request; no driver installer started.'),
            backup=backup,cancelled=_cancel(cancel_event),navigation_cancelled=True,navigation_cancelled_items=1)
    targeted_result=isinstance(backup,dict) and isinstance(backup.get('backed_updates'),list) and isinstance(backup.get('blocked_updates'),list)
    if targeted_result:
        def binding_key(row):
            if not isinstance(row,dict) or not isinstance(row.get('id'),str) or type(row.get('revision')) is not int:return None
            return row['id'].casefold(),row['revision']
        selected_keys=[binding_key(value) for value in eligible]
        backed=[binding_key(row) for row in backup['backed_updates']]
        blocked={binding_key(row):row for row in backup['blocked_updates'] if binding_key(row) is not None}
        allowed=[]
        for value in eligible:
            key=binding_key(value)
            if key is not None and backed.count(key)==1 and key not in blocked:
                allowed.append(value)
            else:
                reason=str(blocked.get(key,{}).get('reason') or 'This exact offered driver has no verified matching installed-package backup.')
                items.append({'binding':value,'status':'skipped','reason':reason,
                    'result':{'installer_started':False,'outcomeKnown':True,'running':False,'skipped':True,'note':reason}})
        eligible=allowed
        # A complete exact per-offer refusal is a known no-install outcome.
        # Do not invent an additional failed driver when every real offer was
        # deliberately skipped because its recovery package was unavailable.
        # Malformed, failed, contradictory and incomplete backup receipts still
        # take the failure path below; none can authorize an installer.
        blocked_keys=[binding_key(row) for row in backup['blocked_updates']]
        if (not eligible and not backup['backed_updates'] and backup.get('status')=='blocked'
                and type(backup.get('exit_code')) is int and backup['exit_code']==1
                and isinstance(backup.get('destination'),str) and backup['destination']
                and not backup.get('cancelled') and not backup.get('error')
                and None not in blocked_keys and len(blocked_keys)==len(set(blocked_keys))
                and set(blocked_keys)==set(selected_keys)
                and all(isinstance(row.get('reason'),str) and row['reason'] for row in backup['blocked_updates'])):
            return finish(items,backup=backup,cancelled=_cancel(cancel_event),review_only=False,
                installer_started=False,outcomeKnown=True,running=False,backup_unavailable=True,
                note='Every applicable approved offer was skipped because its complete recovery backup could not be verified. No driver installer started; the per-offer reasons remain available.')
    if ((not isinstance(backup,dict) or type(backup.get('exit_code')) is not int or backup['exit_code']!=0)
            or not isinstance(backup,dict) or not isinstance(backup.get('destination'),str) or not backup['destination']):
        return finish(items+[{'status':'failed','binding':{'stage':'driver-backup'},'result':{
            'installer_started':False,'outcomeKnown':True,'running':False,
            'note':str(backup.get('reason') or backup.get('error') or 'Driver-package backup failed.')[:2000] if isinstance(backup,dict) else 'Invalid driver backup receipt.'},'error':'A successful third-party driver backup is required before installation.',
            'installer_started':False,'outcomeKnown':True}],backup=backup,error=str(backup.get('error') or backup.get('reason') or backup.get('note') or 'The driver-package backup did not establish success.')[:4000] if isinstance(backup,dict) else 'Invalid driver backup receipt.',installer_started=False,outcomeKnown=True)
    processed=0
    for index,value in enumerate(eligible):
        if _cancel(cancel_event):break
        if phased and {key:config.get(key) for key in approved_scope}!=approved_scope:break
        work=WorkItem(navigation,'driver:update:'+value['id']+':'+str(value['revision']),value['title'],external=cancel_event,kind='mutation')
        progress_key='driver:update:'+value['id']+':'+str(value['revision'])
        progress_state={'operation':'driver','current':index+1,'index':index+1,'total':len(eligible),'description':value['title'],'binding':value}
        if item_callback:item_callback(dict(progress_state,item_progress=normalize_item_progress({
            'key':progress_key,'label':value['title'],'phase':'checking','percent':None})))
        if _cancel(cancel_event):
            work.finish('cancelled',mutation_started=False)
            break
        if phased and {key:config.get(key) for key in approved_scope}!=approved_scope:
            work.finish('skipped',mutation_started=False)
            break
        processed=index+1
        reporter=BoundItemProgress(item_callback,progress_key,value['title'],progress_state)
        try:
            if work.requested():
                outcome=navigation_skip()
            else:
                work.installing()
                options={'accept_eula':True}
                if callable(item_callback) and accepts_progress_callback(backend.driver_update_apply_unattended):
                    options['progress_callback']=reporter
                outcome=backend.driver_update_apply_unattended(value,**options)
            if shared_update_refusal(outcome):outcome=dict(outcome,shared_barrier=True)
            success=(isinstance(outcome,dict) and type(outcome.get('resultCode')) is int and outcome['resultCode']==2
                     and outcome.get('hresult',0)==0 and outcome.get('aggregateResultCode',2)==2 and outcome.get('aggregateHresult',0)==0)
            items.append({'binding':value,'status':'cancelled' if isinstance(outcome,dict) and outcome.get('navigation_cancelled') is True else 'applied' if success else 'failed','result':outcome})
            work.finish(items[-1]['status'],result=items[-1],mutation_started=outcome.get('installer_started',success) if isinstance(outcome,dict) else None,
                outcome_known=isinstance(outcome,dict) and outcome.get('outcomeKnown') is True and outcome.get('running') is False,
                message=outcome.get('note') if isinstance(outcome,dict) else None)
            if local_skip(outcome):continue
            if not success and can_fail_forward(outcome):continue
            if (not success or outcome.get('restartNeeded') is True or shared_update_refusal(outcome)
                    or outcome.get('outcomeKnown') is False or outcome.get('running') is True):break
        except Exception as error:
            known=(getattr(error,'installer_started',None) is False and getattr(error,'outcomeKnown',None) is True)
            receipt={'installer_started':False,'outcomeKnown':True,'running':False,
                'requiresReview':True,'failure_scope':getattr(error,'failure_scope','shared'),
                'failureKind':getattr(error,'failureKind','preflight_failed'),'note':str(error)[:2000]} if known else {
                'installer_started':getattr(error,'installer_started',None),'outcomeKnown':False,'running':getattr(error,'running',None)}
            if shared_update_refusal(receipt):receipt['shared_barrier']=True
            items.append({'binding':value,'status':'failed','error':str(error),'uncertain':not known,'result':receipt})
            work.finish('failed',result=items[-1],mutation_started=not known,outcome_known=known,message=str(error))
            if can_fail_forward(receipt):continue
            break
        finally:
            reporter.close()
    lifecycle={'installer_started':False,'outcomeKnown':True,'running':False} if processed==0 else {}
    if processed:
        outcomes=[row.get('result') for row in items[-processed:]]
        if all(isinstance(outcome,dict) and outcome.get('outcomeKnown') is True
               and outcome.get('running') is False and type(outcome.get('installer_started')) is bool
               for outcome in outcomes):
            lifecycle={'installer_started':any(outcome['installer_started'] for outcome in outcomes),
                       'outcomeKnown':True,'running':False}
    return finish(items,cancelled=_cancel(cancel_event),review_only=False,backup=backup,deferred=eligible[processed:],
        navigation_cancelled_items=sum(isinstance(row.get('result'),dict) and row['result'].get('navigation_cancelled') is True for row in items),
        **lifecycle,note='Only approved applicable update identities or exact device/provider/class rules were used. No automatic reboot or elevation was requested.')

