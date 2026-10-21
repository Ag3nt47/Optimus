"""Disposable local fixtures and mocked backends; never invoke a recycle API."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from optimus import automated_files as module
from optimus.automation import CURRENT_SETUP_VERSION


class LocalBackend:
    def __init__(self, root):
        self.root=Path(root);self.actions=[];self.complete=True;self.before_action=None
        payload=b'identical fixture data'*100
        self.records=[]
        for index,days in enumerate((30,20,10)):
            file=self.root/('copy-'+str(index)+'.fixture');file.write_bytes(payload)
            when=time.time()-days*86400;os.utime(file,(when,when));info=file.stat()
            self.records.append({'FilePath':str(file),'Root':str(self.root),'ScanId':'a'*32,'Fingerprint':hashlib.sha256(payload).hexdigest(),
                                 'Length':len(payload),'LastWriteTimeUtcTicks':info.st_mtime_ns//100+module._TICKS_UNIX})
    def duplicate_scan(self,root):
        return {'Root':str(root),'ScanId':'a'*32,'Complete':self.complete,'Groups':[{'SHA256':self.records[0]['Fingerprint'],'Candidates':copy.deepcopy(self.records)}]}
    def duplicate_recycle_approved(self,record):
        if self.before_action:self.before_action(record)
        self.actions.append(record['FilePath'])
        return {'Recycled':True,'FilePath':record['FilePath']}


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='optimus-duplicate-policy-');self.addCleanup(self.temp.cleanup)
        self.backend=LocalBackend(self.temp.name)
        self.config={'setup_version':CURRENT_SETUP_VERSION,'duplicates_enabled':True,'duplicate_policy':'keep_oldest','duplicate_changes_approved':True,
                     'duplicate_roots':[self.temp.name],'duplicate_max_recycle':25,'duplicate_min_age_days':7}
    def test_keep_oldest_never_recycles_deterministic_keeper(self):
        result=module.run_local(self.backend,self.config)
        self.assertEqual(self.backend.actions,[item['FilePath'] for item in self.backend.records[1:]])
        self.assertEqual(result['recycled_count'],2)
        self.assertTrue(all(item['keeper']==self.backend.records[0]['FilePath'] for item in result['items']))
    def test_keep_newest_reverses_keeper_but_preserves_one(self):
        self.config['duplicate_policy']='keep_newest'
        result=module.run_local(self.backend,self.config)
        self.assertNotIn(self.backend.records[2]['FilePath'],self.backend.actions)
        self.assertEqual(result['recycled_count'],2)
    def test_equal_timestamps_have_deterministic_name_tiebreak(self):
        ticks=self.backend.records[0]['LastWriteTimeUtcTicks']
        for item in self.backend.records:item['LastWriteTimeUtcTicks']=ticks
        with patch.object(module,'_keeper_valid',return_value=True):
            module.run_local(self.backend,self.config)
        self.assertNotIn(self.backend.records[0]['FilePath'],self.backend.actions)
    def test_review_unapproved_or_old_setup_never_mutates(self):
        for changes in ({'duplicate_policy':'review'},{'duplicate_changes_approved':False},{'setup_version':CURRENT_SETUP_VERSION-1}):
            with self.subTest(changes=changes):
                result=module.run_local(self.backend,dict(self.config,**changes))
                self.assertTrue(result['review_only']);self.assertEqual(self.backend.actions,[])
    def test_incomplete_scan_fails_closed(self):
        self.backend.complete=False
        result=module.run_local(self.backend,self.config)
        self.assertEqual(self.backend.actions,[]);self.assertTrue(result['skips'])
    def test_changed_keeper_blocks_recycling(self):
        Path(self.backend.records[0]['FilePath']).write_bytes(b'changed keeper')
        result=module.run_local(self.backend,self.config)
        self.assertEqual(self.backend.actions,[]);self.assertTrue(result['skips'])
    def test_minimum_age_and_maximum_are_applied(self):
        result=module.run_local(self.backend,dict(self.config,duplicate_min_age_days=25))
        self.assertEqual(result['recycled_count'],0)
        result=module.run_local(self.backend,dict(self.config,duplicate_max_recycle=1))
        self.assertEqual(result['recycled_count'],1)
    def test_cancel_callback_prevents_the_next_shell_request(self):
        event=threading.Event()
        result=module.run_local(self.backend,self.config,event,lambda item:event.set())
        self.assertTrue(result['cancelled']);self.assertEqual(self.backend.actions,[])
    def test_callback_can_cancel_between_successful_items(self):
        event=threading.Event();self.backend.before_action=lambda record:event.set()
        result=module.run_local(self.backend,self.config,event)
        self.assertTrue(result['cancelled']);self.assertEqual(len(self.backend.actions),1)
    def test_scope_forgery_never_reaches_recycle(self):
        self.backend.records[1]['Root']=r'C:\Unexpected'
        result=module.run_local(self.backend,self.config)
        self.assertEqual(self.backend.actions,[]);self.assertEqual(result['failed_items'],1)
    def test_recycle_exception_is_not_replayed_or_followed_by_more(self):
        self.backend.duplicate_recycle_approved=lambda record:(_ for _ in ()).throw(RuntimeError('Fixture uncertain shell outcome'))
        result=module.run_local(self.backend,self.config)
        self.assertEqual(result['failed_items'],1);self.assertTrue(result['stopped_after_error'])
    def test_false_recycle_outcome_stops_remaining_batch(self):
        calls=[]
        self.backend.duplicate_recycle_approved=lambda record:calls.append(record) or {'Recycled':False}
        result=module.run_local(self.backend,self.config)
        self.assertEqual(len(calls),1);self.assertEqual(result['failed_items'],1);self.assertTrue(result['stopped_after_error'])
    def test_invalid_policy_and_limits_fail_before_scan(self):
        for changes in ({'duplicate_policy':'delete_all'},{'duplicate_max_recycle':True},{'duplicate_min_age_days':0}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):module.run_local(self.backend,dict(self.config,**changes))


class CloudRunnerTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        self.backend=type('Backend',(),{})()
        self.backend.cloud_status=lambda:{'connected':True}
        self.backend.cloud_approval_scope=lambda folder:{'drive_id':'drive','folder_id':folder}
        self.backend.cloud_scan=lambda folder:{'drive':{'id':'drive'},'folder':{'id':folder},'complete':True,'groups':[]}
        def recycle(plan,policy,**kwargs):
            self.calls.append((plan,policy,kwargs));return {'items':[],'skips':[],'failed_items':0,'recycled_count':1,'attempted_count':1,'verified_bytes':100}
        self.backend.cloud_recycle_approved=recycle
        self.config={'setup_version':CURRENT_SETUP_VERSION,'cloud_review_enabled':True,'cloud_policy':'keep_oldest','cloud_changes_approved':True,
                     'cloud_drive_id':'drive','cloud_folder_ids':['folder-one','folder-two'],'cloud_max_recycle':1,'cloud_min_age_days':7}
    def test_saved_scope_and_whole_run_cap_are_forwarded(self):
        result=module.run_cloud(self.backend,self.config)
        self.assertEqual(len(self.calls),1);self.assertEqual(result['recycled_count'],1)
        self.assertEqual(self.calls[0][1]['_verification_bytes_remaining'],256*1024*1024)
    def test_no_drive_or_concrete_folder_means_no_account_fallback(self):
        for change in ({'cloud_drive_id':''},{'cloud_folder_ids':[]},{'cloud_folder_ids':['root']}):
            with self.subTest(change=change):
                result=module.run_cloud(self.backend,dict(self.config,**change))
                self.assertTrue(result['needs_approval']);self.assertEqual(self.calls,[])
    def test_changed_or_disconnected_account_never_scans_or_recycles(self):
        self.backend.cloud_status=lambda:{'connected':False}
        result=module.run_cloud(self.backend,self.config)
        self.assertTrue(result['needs_approval']);self.assertEqual(self.calls,[])
        self.backend.cloud_status=lambda:{'connected':True}
        self.backend.cloud_approval_scope=lambda folder:{'drive_id':'another-drive','folder_id':folder}
        result=module.run_cloud(self.backend,self.config)
        self.assertTrue(result['needs_approval']);self.assertEqual(self.calls,[])
    def test_old_or_missing_approval_runs_review_only(self):
        result=module.run_cloud(self.backend,dict(self.config,cloud_changes_approved=False))
        self.assertTrue(result['review_only']);self.assertEqual(self.calls,[])
    def test_incomplete_cloud_scan_never_applies(self):
        self.backend.cloud_scan=lambda folder:{'drive':{'id':'drive'},'folder':{'id':folder},'complete':False}
        result=module.run_cloud(self.backend,self.config)
        self.assertTrue(result['skips']);self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
