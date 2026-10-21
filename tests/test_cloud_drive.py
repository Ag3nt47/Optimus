from pathlib import Path
import copy,json,tempfile,types,unittest,time,base64
from urllib.parse import urlsplit,unquote
source=Path(__file__).resolve().parents[1]/'src'/'optimus'/'cloud_drive.py'
m=types.ModuleType('cloud_drive_fixture');exec(compile(source.read_text(encoding='ascii'),str(source),'exec'),m.__dict__)
CLIENT='11111111-2222-4333-8444-555555555555'
class Store:
 def __init__(self):self.value=None;self.clears=0
 def load(self):return copy.deepcopy(self.value)
 def save(self,value):self.value=copy.deepcopy(value)
 def clear(self):self.value=None;self.clears+=1
class HTTP:
 def __init__(self):
  self.calls=[];self.drive={'id':'drive-fixture','driveType':'personal','name':'Fixture personal drive'};self.oauth=[];self.next=None;self.delete_error=False;self.items={}
  for key in ('A','B'):
   self.items[key]={'id':key,'name':key+'.fixture','size':1024,'eTag':'etag-'+key,'file':{'hashes':{'sha1Hash':'A'*40}},'parentReference':{'id':'root-item','driveId':'drive-fixture'}}
 def __call__(self,method,url,headers=None,data=None,timeout=10):
  self.calls.append({'method':method,'url':url,'headers':headers or {},'data':data,'timeout':timeout})
  path=unquote(urlsplit(url).path)
  if path.endswith('/devicecode'):return 200,{'device_code':'private-device-fixture','user_code':'TEST-CODE','verification_uri':'https://microsoft.com/devicelogin','expires_in':900,'interval':5}
  if path.endswith('/token'):
   if self.oauth:
    response=self.oauth.pop(0)
    if isinstance(response,Exception):raise response
    return 200,response
   return 200,{'access_token':'private-access-fixture','refresh_token':'private-refresh-fixture','token_type':'Bearer','expires_in':3600,'scope':'Files.ReadWrite User.Read'}
  if path=='/v1.0/me/drive':return 200,copy.deepcopy(self.drive)
  if path in ('/v1.0/me/drive/root','/v1.0/me/drive/items/root-item'):return 200,{'id':'root-item','name':'Fixture root','folder':{},'root':{}}
  if path=='/v1.0/me/drive/items/root-item/children':
   result={'value':copy.deepcopy(list(self.items.values()))}
   if self.next:result['@odata.nextLink']=self.next
   return 200,result
  key=path.rsplit('/',1)[-1]
  if method=='GET' and key in self.items:return 200,copy.deepcopy(self.items[key])
  if method=='DELETE' and key in self.items:
   if self.delete_error:raise m.CloudRequestError(412,'preconditionFailed')
   assert headers['If-Match']==self.items[key]['eTag']
   del self.items[key];return 204,{}
  raise m.CloudRequestError(404,'itemNotFound')
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='optimus-cloud-fixture-');self.store=Store();self.http=HTTP();self.tool=m.CloudDrive(self.temp.name,token_store=self.store,http=self.http);self.tool.configure(CLIENT)
  self.tool._tokens={'clientId':CLIENT,'tenant':'consumers','accessToken':'private-access-fixture','refreshToken':'private-refresh-fixture','expires':time.time()+3600};self.store.save(self.tool._tokens)
 def tearDown(self):self.temp.cleanup()
 def deletes(self):return [c for c in self.http.calls if c['method']=='DELETE']
 def test_01_constructor_status_no_network(self):
  self.assertEqual(self.http.calls,[]);self.assertTrue(self.tool.status()['configured']);self.assertIn('Dropbox',self.tool.status()['unsupported'])
 def test_02_bad_configuration(self):
  for tenant in ['../token','https://evil.invalid','consumers/../common']:
   with self.assertRaises(ValueError):self.tool.configure(CLIENT,tenant)
  with self.assertRaises(ValueError):self.tool.configure('not-a-client-id')
  self.assertEqual(self.http.calls,[])
 def test_03_device_code_return_redacted(self):
  response=self.tool.begin_connect();text=json.dumps(response)
  self.assertIn('TEST-CODE',text);self.assertNotIn('private-device-fixture',text);self.assertNotIn('private-access-fixture',text)
  self.assertEqual(self.http.calls[-1]['data']['scope'],'Files.ReadWrite offline_access User.Read')
 def test_04_poll_rate_pending_slowdown_success(self):
  response=self.tool.begin_connect();session=self.tool._sessions[response['sessionId']];before=len(self.http.calls)
  self.assertEqual(self.tool.poll_connect(response['sessionId'])['state'],'pending');self.assertEqual(len(self.http.calls),before)
  session['nextPoll']=0;self.http.oauth=[m.CloudRequestError(400,'authorization_pending')];self.assertEqual(self.tool.poll_connect(response['sessionId'])['state'],'pending')
  session['nextPoll']=0;self.http.oauth=[m.CloudRequestError(400,'slow_down')];self.assertEqual(self.tool.poll_connect(response['sessionId'])['retryAfter'],10)
  session['nextPoll']=0;self.assertEqual(self.tool.poll_connect(response['sessionId'])['state'],'connected');self.assertEqual(self.store.value['accessToken'],'private-access-fixture')
  for path in Path(self.temp.name).rglob('*'):
   if path.is_file():self.assertNotIn('private-access-fixture',path.read_text())
 def test_05_logout_own_store(self):
  self.tool.begin_connect();self.tool.logout();self.assertIsNone(self.store.value);self.assertFalse(self.tool._sessions);self.assertFalse(self.tool.status()['connected'])
 def test_06_metadata_only_duplicate_scan(self):
  result=self.tool.scan();self.assertTrue(result['complete']);self.assertEqual(len(result['groups']),1);self.assertEqual(result['groups'][0]['hashAlgorithm'],'sha1Hash');self.assertEqual(result['groups'][0]['copies'],2)
  self.assertTrue(all(c['method']=='GET' for c in self.http.calls));self.assertTrue(all('/content' not in c['url'] for c in self.http.calls));self.assertFalse(any(i['selected'] for i in result['groups'][0]['items']))
 def test_07_business_blocked(self):
  self.http.drive['driveType']='business'
  with self.assertRaises(ValueError):self.tool.scan()
  self.assertEqual(len(self.http.calls),1)
 def test_08_shared_remote_excluded(self):
  self.http.items['A']['remoteItem']={};result=self.tool.scan();self.assertEqual(result['groups'],[])
  self.http.items['A'].pop('remoteItem');self.http.items['A']['shared']={};result=self.tool.scan();self.assertEqual(result['groups'],[])
 def test_09_crossdrive_excluded(self):
  self.http.items['A']['parentReference']['driveId']='other-drive';self.assertEqual(self.tool.scan()['groups'],[])
 def test_10_external_pagination_blocked(self):
  self.http.next='https://evil.invalid/v1.0/me/drive/items/root-item/children?page=2'
  with self.assertRaises(ValueError):self.tool.scan()
  self.assertTrue(all(urlsplit(c['url']).hostname=='graph.microsoft.com' for c in self.http.calls))
 def test_11_wrongfolder_pagination_blocked(self):
  self.http.next='https://graph.microsoft.com/v1.0/me/drive/items/other-folder/children?page=2'
  with self.assertRaises(ValueError):self.tool.scan()
 def test_12_item_cap(self):
  result=self.tool.scan(max_items=1);self.assertEqual(result['examined'],1);self.assertFalse(result['complete'])
 def test_13_confirm_required(self):
  plan=self.tool.scan()
  with self.assertRaises(ValueError):self.tool.recycle({'planId':plan['planId'],'selectedIds':['A']})
  self.assertEqual(self.deletes(),[])
 def test_14_no_bulk_or_lastcopy(self):
  plan=self.tool.scan()
  with self.assertRaises(ValueError):self.tool.recycle({'planId':plan['planId'],'selectedIds':['A','B'],'confirmed':True})
  self.tool.recycle({'planId':plan['planId'],'selectedIds':['A'],'confirmed':True})
  with self.assertRaises(ValueError):self.tool.recycle({'planId':plan['planId'],'selectedIds':['B'],'confirmed':True})
  self.assertEqual(len(self.deletes()),1)
 def test_15_etag_delete(self):
  plan=self.tool.scan();result=self.tool.recycle({'planId':plan['planId'],'selectedIds':['A'],'confirmed':True,'path':'/other-drive'})
  self.assertEqual(result['state'],'recycled');self.assertEqual(self.deletes()[0]['headers']['If-Match'],'etag-A');self.assertIn('/me/drive/items/A',self.deletes()[0]['url'])
 def test_16_stale_selected(self):
  plan=self.tool.scan();self.http.items['A']['eTag']='new-etag'
  with self.assertRaises(ValueError):self.tool.recycle({'planId':plan['planId'],'selectedIds':['A'],'confirmed':True})
  self.assertEqual(self.deletes(),[])
 def test_17_missing_keeper(self):
  plan=self.tool.scan();del self.http.items['B']
  with self.assertRaises(m.CloudRequestError):self.tool.recycle({'planId':plan['planId'],'selectedIds':['A'],'confirmed':True})
  self.assertEqual(self.deletes(),[])
 def test_18_drive_switch(self):
  plan=self.tool.scan();self.http.drive['id']='changed-drive'
  with self.assertRaises(ValueError):self.tool.recycle({'planId':plan['planId'],'selectedIds':['A'],'confirmed':True})
  self.assertEqual(self.deletes(),[])
 def test_19_expiry(self):
  plan=self.tool.scan();self.tool._plans[plan['planId']]['created']-=1000
  with self.assertRaises(ValueError):self.tool.recycle({'planId':plan['planId'],'selectedIds':['A'],'confirmed':True})
  self.assertEqual(self.deletes(),[])
 def test_20_precondition_failure(self):
  plan=self.tool.scan();self.http.delete_error=True
  with self.assertRaises(m.CloudRequestError):self.tool.recycle({'planId':plan['planId'],'selectedIds':['A'],'confirmed':True})
  self.assertFalse(self.tool._plans[plan['planId']]['removed'])
 def test_21_hash_labels(self):
  for item in self.http.items.values():item['file']['hashes']={'quickXorHash':base64.b64encode(b'x'*20).decode()}
  result=self.tool.scan();self.assertEqual(result['groups'][0]['hashAlgorithm'],'quickXorHash');self.assertIn('collisions',result['note'])
 def test_22_refresh_tokens_not_plaintext(self):
  self.tool._tokens['expires']=0;self.tool.scan();self.assertTrue(any(c['data'] and c['data'].get('grant_type')=='refresh_token' for c in self.http.calls))
  for path in Path(self.temp.name).rglob('*'):
   if path.is_file():self.assertNotIn('private-refresh-fixture',path.read_text())
 def test_23_redirect_and_urls(self):
  with self.assertRaises(m.CloudRequestError):m._NoRedirect().redirect_request(None,None,302,'redirect',{},'https://evil.invalid')
  for url in ['http://graph.microsoft.com/v1.0/me/drive','https://user@graph.microsoft.com/v1.0/me/drive','https://graph.microsoft.com.evil.invalid/v1.0/me/drive']:
   with self.assertRaises(ValueError):m._url(url,'graph.microsoft.com')
 def test_24_unknown_selection(self):
  plan=self.tool.scan()
  with self.assertRaises(ValueError):self.tool.recycle({'planId':plan['planId'],'selectedIds':['unknown'],'confirmed':True})
  self.assertEqual(self.deletes(),[])

 def test_25_nested_ancestry_scope(self):
  calls=[]
  def nested(method,url,headers=None,data=None,timeout=10):
   calls.append(url)
   if urlsplit(url).path.endswith('/root-item'):return 200,{'id':'root-item','name':'Root','folder':{},'root':{}}
   return 200,{'id':'nested-parent','name':'Folder','folder':{},'parentReference':{'id':'root-item','driveId':'drive-fixture'}}
  self.tool.http=nested
  self.tool._scope_check({'parentReference':{'id':'nested-parent'}},{'rootId':'root-item','driveId':'drive-fixture'})
  self.assertEqual(len(calls),2)
  with self.assertRaises(ValueError):self.tool._scope_check({'parentReference':{'id':'nested-parent'}},{'rootId':'different-root','driveId':'drive-fixture'})
 def test_26_no_config_connect_fails_without_network(self):
  self.tool.config={}
  with self.assertRaises(ValueError):self.tool.begin_connect()
  self.assertEqual(self.http.calls,[])
if __name__=='__main__':unittest.main(verbosity=2)
