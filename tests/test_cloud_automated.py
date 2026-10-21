"""Mock personal-drive content verification; no account traffic or live recycling."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit,unquote
from optimus import cloud_drive as module
from optimus.automation import CURRENT_SETUP_VERSION

CLIENT='11111111-2222-4333-8444-555555555555'
class Store:
    def load(self):return None
    def save(self,value):pass
    def clear(self):pass
class HTTP:
    def __init__(self):
        self.calls=[];self.drive={'id':'drive-fixture','driveType':'personal','name':'Fixture drive'};self.items={};self.contents={};self.fail_delete=False;self.root_shared=False
        for index,key in enumerate(('A','B','C')):
            self.items[key]={'id':key,'name':key+'.fixture','size':4,'eTag':'tag-'+key,'lastModifiedDateTime':f'202{index}-01-01T00:00:00Z',
                             'file':{'hashes':{'quickXorHash':base64.b64encode(b'x'*20).decode()}},'parentReference':{'id':'folder-fixture','driveId':'drive-fixture'}}
            self.contents[key]=b'same'
    def __call__(self,method,url,headers=None,data=None,timeout=10):
        self.calls.append({'method':method,'url':url,'headers':headers or {}})
        path=unquote(urlsplit(url).path)
        if path=='/v1.0/me/drive':return 200,copy.deepcopy(self.drive)
        if path in ('/v1.0/me/drive/root','/v1.0/me/drive/items/folder-fixture'):
            root={'id':'folder-fixture','name':'Fixture root','folder':{},'root':{}}
            if self.root_shared:root['shared']={}
            return 200,root
        if path.endswith('/folder-fixture/children'):return 200,{'value':copy.deepcopy(list(self.items.values()))}
        key=path.rsplit('/',1)[-1]
        if method=='GET' and key in self.items:
            result=copy.deepcopy(self.items[key])
            if 'downloadUrl' in url:result['@microsoft.graph.downloadUrl']='https://fixture.files.1drv.com/'+key+'?private-fixture-url-token'
            return 200,result
        if method=='DELETE' and key in self.items:
            if self.fail_delete:raise RuntimeError('Fixture HTTP error at signed URL private-fixture-url-token')
            if headers.get('If-Match')!=self.items[key]['eTag']:raise module.CloudRequestError(412,'preconditionFailed')
            del self.items[key];return 204,{}
        raise module.CloudRequestError(404,'itemNotFound')

class CloudPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='optimus-cloud-policy-');self.addCleanup(self.temp.cleanup)
        self.http=HTTP();self.hash_calls=[]
        def digest(url,size,budget,event):
            key=urlsplit(url).path.rsplit('/',1)[-1];self.hash_calls.append(key)
            value=self.http.contents[key]
            budget['used']+=len(value);budget['remaining']-=len(value)
            return hashlib.sha256(value).hexdigest()
        self.tool=module.CloudDrive(self.temp.name,token_store=Store(),http=self.http,content_hasher=digest)
        self.tool.config={'clientId':CLIENT,'tenant':'consumers'}
        self.tool._tokens={'accessToken':'private-access-fixture','expires':time.time()+3600}
        self.policy={'setup_version':CURRENT_SETUP_VERSION,'cloud_review_enabled':True,'cloud_policy':'keep_oldest','cloud_changes_approved':True,
                     'cloud_drive_id':'drive-fixture','cloud_folder_ids':['folder-fixture'],'cloud_max_recycle':25,'cloud_min_age_days':7}
    def plan(self):return self.tool.scan('folder-fixture')
    def deleted(self):return [call for call in self.http.calls if call['method']=='DELETE']
    def test_explicit_root_approval_resolves_concrete_owned_id(self):
        scope=self.tool.approval_scope('root')
        self.assertEqual(scope['folder_id'],'folder-fixture');self.assertEqual(scope['drive_id'],'drive-fixture')
        self.assertEqual(self.plan()['folder']['id'],'folder-fixture')
    def test_unconnected_scope_verification_never_starts_authentication(self):
        self.tool._tokens=None
        with self.assertRaises(ValueError):self.tool.approval_scope('root')
        self.assertEqual(self.http.calls,[])
    def test_saved_policy_recycles_only_sha256_verified_nonkeeper_files(self):
        result=self.tool.recycle_approved(self.plan(),self.policy)
        self.assertEqual(result['recycled_count'],2);self.assertEqual(result['verified_bytes'],12)
        self.assertEqual(set(self.http.items),{'A'})
        self.assertEqual(self.hash_calls,['A','B','C'])
        self.assertTrue(all(call['headers']['If-Match'].startswith('tag-') for call in self.deleted()))
        self.assertNotIn('private-fixture-url-token',json.dumps(result))
    def test_keep_newest_preserves_newest_exact_copy(self):
        result=self.tool.recycle_approved(self.plan(),dict(self.policy,cloud_policy='keep_newest'))
        self.assertEqual(result['recycled_count'],2);self.assertEqual(set(self.http.items),{'C'})
    def test_metadata_hash_collision_does_not_authorize_content_delete(self):
        self.http.contents['B']=b'evil';self.http.contents['C']=b'also'
        result=self.tool.recycle_approved(self.plan(),self.policy)
        self.assertEqual(self.deleted(),[]);self.assertEqual(result['recycled_count'],0)
        self.assertEqual(len(result['skips']),2)
    def test_setup_and_saved_approval_are_independently_required(self):
        plan=self.plan()
        for changes in ({'setup_version':CURRENT_SETUP_VERSION-1},{'cloud_changes_approved':False},{'cloud_policy':'review'}):
            result=self.tool.recycle_approved(plan,dict(self.policy,**changes))
            self.assertTrue(result['needs_approval']);self.assertEqual(self.deleted(),[])
        self.assertEqual(self.hash_calls,[])
    def test_wrong_bound_drive_or_folder_never_downloads_or_deletes(self):
        plan=self.plan()
        for changes in ({'cloud_drive_id':'wrong-drive'},{'cloud_folder_ids':['another-folder']}):
            result=self.tool.recycle_approved(plan,dict(self.policy,**changes))
            self.assertTrue(result['needs_approval']);self.assertEqual(self.deleted(),[])
        self.assertEqual(self.hash_calls,[])
    def test_changed_account_after_scan_requires_new_scope(self):
        plan=self.plan();self.http.drive['id']='another-drive'
        result=self.tool.recycle_approved(plan,self.policy)
        self.assertTrue(result['needs_approval']);self.assertEqual(self.hash_calls,[])
    def test_changed_keeper_or_selected_etag_blocks_recycle(self):
        plan=self.plan();self.http.items['A']['eTag']='changed'
        result=self.tool.recycle_approved(plan,self.policy)
        self.assertTrue(result['skips']);self.assertEqual(self.deleted(),[])
    def test_shared_or_moved_items_fail_revalidation(self):
        plan=self.plan();self.http.items['B']['shared']={}
        result=self.tool.recycle_approved(plan,self.policy)
        self.assertEqual(self.deleted(),[]);self.assertTrue(result['skips'])
    def test_missing_dates_and_recent_files_are_not_recycled(self):
        self.http.items['B'].pop('lastModifiedDateTime')
        result=self.tool.recycle_approved(self.plan(),self.policy)
        self.assertEqual(self.deleted(),[]);self.assertTrue(result['skips'])
    def test_partial_scan_cannot_be_forged_complete_by_caller(self):
        plan=self.tool.scan('folder-fixture',max_items=1);plan['complete']=True
        result=self.tool.recycle_approved(plan,self.policy)
        self.assertEqual(self.deleted(),[]);self.assertTrue(result['skips'])
    def test_count_cap_keeps_deterministic_keeper(self):
        result=self.tool.recycle_approved(self.plan(),dict(self.policy,cloud_max_recycle=1))
        self.assertEqual(result['recycled_count'],1);self.assertIn('A',self.http.items)
    def test_content_budget_is_bounded_before_extra_download(self):
        result=self.tool.recycle_approved(self.plan(),dict(self.policy,_verification_bytes_remaining=7))
        self.assertEqual(self.hash_calls,['A']);self.assertEqual(self.deleted(),[]);self.assertEqual(result['verified_bytes'],4)
    def test_per_file_64mib_limit_does_not_download(self):
        for item in self.http.items.values():item['size']=65*1024*1024
        result=self.tool.recycle_approved(self.plan(),self.policy)
        self.assertEqual(self.hash_calls,[]);self.assertEqual(self.deleted(),[]);self.assertTrue(result['skips'])
    def test_cancellation_callback_prevents_delete_and_claims_cancelled(self):
        event=threading.Event()
        result=self.tool.recycle_approved(self.plan(),self.policy,event,lambda item:event.set())
        self.assertTrue(result['cancelled']);self.assertEqual(self.deleted(),[])
    def test_untrusted_download_endpoint_never_reaches_hasher(self):
        original=self.tool._graph
        def graph(*args,**kwargs):
            status,item=original(*args,**kwargs)
            if isinstance(item,dict) and '@microsoft.graph.downloadUrl' in item:item['@microsoft.graph.downloadUrl']='https://evil.invalid/private-token'
            return status,item
        self.tool._graph=graph
        result=self.tool.recycle_approved(self.plan(),self.policy)
        self.assertEqual(self.hash_calls,[]);self.assertEqual(self.deleted(),[]);self.assertNotIn('private-token',json.dumps(result))
    def test_uncertain_delete_stops_remaining_actions_without_url_disclosure(self):
        self.http.fail_delete=True
        result=self.tool.recycle_approved(self.plan(),self.policy)
        self.assertEqual(len(self.deleted()),1);self.assertEqual(result['failed_items'],1)
        self.assertTrue(result['stopped_after_error']);self.assertNotIn('private-fixture-url-token',json.dumps(result))
    def test_root_becoming_shared_after_scan_blocks_download_and_recycle(self):
        plan=self.plan();self.http.root_shared=True
        result=self.tool.recycle_approved(plan,self.policy)
        self.assertTrue(result['needs_approval']);self.assertEqual(self.deleted(),[]);self.assertEqual(self.hash_calls,[])
    def test_root_sharing_change_after_progress_callback_still_blocks_delete(self):
        def progress(item):self.http.root_shared=True
        result=self.tool.recycle_approved(self.plan(),self.policy,item_callback=progress)
        self.assertEqual(self.deleted(),[]);self.assertTrue(result['stopped_after_error'])
    def test_temporary_urls_are_never_persisted_in_plans_or_files(self):
        self.tool.recycle_approved(self.plan(),self.policy)
        self.assertNotIn('private-fixture-url-token',json.dumps(self.tool._plans,default=list))
        for path in Path(self.temp.name).rglob('*'):
            if path.is_file():self.assertNotIn('private-fixture-url-token',path.read_text())


class StreamTests(unittest.TestCase):
    def budget(self):return {'used':0,'remaining':256*1024*1024,'deadline':time.monotonic()+20}
    def response(self,payload=b'same'):
        class Response(io.BytesIO):
            status=200;headers={'Content-Length':str(len(payload))}
            def geturl(self):return 'https://fixture.files.1drv.com/private-token'
        return Response(payload)
    def test_streams_sha256_without_bearer_cookie_or_following_redirects(self):
        response=self.response();requests=[]
        def receive(_,req,timeout):requests.append(req);return response
        opener=type('Opener',(),{'open':receive})()
        with patch.object(module.request,'build_opener',return_value=opener) as build:
            result=module._stream_sha256('https://fixture.files.1drv.com/private-token',4,self.budget())
        self.assertEqual(result,hashlib.sha256(b'same').hexdigest())
        self.assertIsInstance(build.call_args.args[0],module._NoRedirect)
        self.assertFalse(requests[0].has_header('Authorization'))
        self.assertFalse(requests[0].has_header('Cookie'))
    def test_wrong_host_scheme_credentials_or_port_are_blocked(self):
        for url in ('http://fixture.files.1drv.com/a','https://fixture.files.1drv.com.evil.invalid/a','https://user@fixture.files.1drv.com/a','https://fixture.files.1drv.com:444/a'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):module._content_url(url)
    def test_failed_network_exception_redacts_temporary_url(self):
        opener=type('Opener',(),{'open':lambda *args,**kwargs:(_ for _ in ()).throw(OSError('https://fixture.files.1drv.com/private-token'))})()
        with patch.object(module.request,'build_opener',return_value=opener):
            with self.assertRaises(RuntimeError) as caught:module._stream_sha256('https://fixture.files.1drv.com/private-token',4,self.budget())
        self.assertNotIn('private-token',str(caught.exception))
    def test_cancelled_or_truncated_stream_never_verifies(self):
        event=threading.Event();event.set()
        opener=type('Opener',(),{'open':lambda _,req,timeout:self.response()})()
        with patch.object(module.request,'build_opener',return_value=opener):
            with self.assertRaises(module.CloudVerificationCancelled):module._stream_sha256('https://fixture.files.1drv.com/a',4,self.budget(),event)
        response=self.response(b'bad');response.headers={'Content-Length':'4'}
        opener=type('Opener',(),{'open':lambda _,req,timeout:response})()
        with patch.object(module.request,'build_opener',return_value=opener):
            with self.assertRaises(RuntimeError):module._stream_sha256('https://fixture.files.1drv.com/a',4,self.budget())


if __name__=='__main__':unittest.main()
