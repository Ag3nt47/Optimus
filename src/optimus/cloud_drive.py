"""Personal OneDrive metadata review using a user-registered public OAuth client.

No cloud account is contacted until the user connects or requests an operation.
Tokens are encrypted for the current Windows user; no content is downloaded.
"""
from __future__ import annotations
import base64,ctypes,hashlib,json,os,re,tempfile,time,uuid
from pathlib import Path
from urllib import request,error,parse
from collections import deque

_SCOPES = 'Files.ReadWrite offline_access User.Read'
_GRAPH = 'https://graph.microsoft.com/v1.0'
_ITEM_SELECT = 'id,name,size,eTag,file,folder,parentReference,remoteItem,shared,package,root,sharepointIds'
_ID = re.compile(r'^[A-Za-z0-9!_.:\-]{1,256}$')


class CloudRequestError(RuntimeError):
    def __init__(self,status,code='request_failed'):
        self.status=int(status)
        self.code=code if re.fullmatch(r'[A-Za-z0-9_.\-]{1,128}',str(code)) else 'request_failed'
        super().__init__(f'Microsoft request failed ({self.status}, {self.code}). No credentials were logged.')


def _write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temp=tempfile.mkstemp(prefix='optimus-cloud-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


class DPAPITokenStore:
    """Only current-user DPAPI ciphertext is persisted, never a JSON token file."""
    def __init__(self,path):self.path=Path(path)
    @staticmethod
    def _crypt(data,decrypt=False):
        if os.name!='nt':raise RuntimeError('Encrypted cloud sign-in storage requires Windows DPAPI.')
        from ctypes import wintypes
        class Blob(ctypes.Structure):_fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
        buffer=ctypes.create_string_buffer(data)
        incoming=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)));out=Blob()
        crypt=ctypes.WinDLL('crypt32',use_last_error=True)
        free=ctypes.WinDLL('kernel32',use_last_error=True).LocalFree
        free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
        if decrypt:
            method=crypt.CryptUnprotectData
            method.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
            success=method(ctypes.byref(incoming),None,None,None,None,1,ctypes.byref(out))
        else:
            method=crypt.CryptProtectData
            method.argtypes=[ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
            success=method(ctypes.byref(incoming),'Optimus personal OneDrive',None,None,None,1,ctypes.byref(out))
        if not success:raise RuntimeError('Windows could not protect or unlock this account credential. Connect again under the same Windows account.')
        try:return ctypes.string_at(out.data,out.size)
        finally:free(ctypes.cast(out.data,ctypes.c_void_p))
    def load(self):
        if not self.path.exists():return None
        if self.path.is_symlink() or self.path.stat().st_size>256000:raise RuntimeError('Unexpected encrypted credential file.')
        return json.loads(self._crypt(self.path.read_bytes(),True).decode('utf-8'))
    def save(self,value):_write(self.path,self._crypt(json.dumps(value,separators=(',',':')).encode('utf-8')))
    def clear(self):self.path.unlink(missing_ok=True)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise CloudRequestError(code,'redirect_blocked')


def _transport(method,url,headers=None,data=None,timeout=10):
    payload=parse.urlencode(data).encode('ascii') if data is not None else None
    req=request.Request(url,data=payload,method=method,headers=headers or {})
    try:
        with request.build_opener(_NoRedirect()).open(req,timeout=timeout) as response:
            raw=response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024:raise RuntimeError('Microsoft metadata response exceeded the local safety limit.')
            return response.status,(json.loads(raw.decode('utf-8')) if raw else {})
    except error.HTTPError as failure:
        try:
            payload=json.loads(failure.read(256000).decode('utf-8'))
            code=payload.get('error','request_failed')
            if isinstance(code,dict):code=code.get('code','request_failed')
        except (ValueError,UnicodeError):code='request_failed'
        raise CloudRequestError(failure.code,code) from None
    except error.URLError:
        raise RuntimeError('Microsoft could not be reached securely. Check connectivity and try again.') from None


def _url(url,host):
    value=parse.urlsplit(url)
    if value.scheme!='https' or value.hostname!=host or value.port not in (None,443) or value.username or value.password or value.fragment:
        raise ValueError('An unexpected Microsoft endpoint was blocked.')
    return value


def _item_id(value):
    if not isinstance(value,str) or not _ID.fullmatch(value):raise ValueError('Invalid OneDrive item identity.')
    return parse.quote(value,safe='')


class CloudDrive:
    def __init__(self,data_dir,token_store=None,http=None):
        self.root=Path(data_dir)/'Cloud'
        self.config_path=self.root/'onedrive-config.json'
        self.store=token_store if token_store is not None else DPAPITokenStore(self.root/'onedrive.tokens.dpapi')
        self.http=http or _transport
        self._sessions={};self._plans={};self._tokens=None
        self.config=json.loads(self.config_path.read_text(encoding='utf-8')) if self.config_path.is_file() else {}
    def configure(self,client_id,tenant='consumers'):
        try:client=str(uuid.UUID(client_id))
        except (ValueError,TypeError,AttributeError):raise ValueError('Enter your own registered Microsoft application client ID.') from None
        if tenant not in ('consumers','common','organizations'):
            try:tenant=str(uuid.UUID(tenant))
            except (ValueError,TypeError,AttributeError):raise ValueError('Use consumers, common, organizations or a tenant GUID.') from None
        updated={'clientId':client,'tenant':tenant}
        if updated!=self.config:self.logout()
        self.config=updated;_write(self.config_path,json.dumps(updated,indent=2).encode('utf-8'))
        return self.status()
    def _load(self):
        if self._tokens is None:
            stored=self.store.load()
            if stored and stored.get('clientId')==self.config.get('clientId') and stored.get('tenant')==self.config.get('tenant'):self._tokens=stored
        return self._tokens
    def status(self):
        tokens=self._load() if self.config.get('clientId') else None
        return {'configured':bool(self.config.get('clientId')),'connected':bool(tokens and (tokens.get('accessToken') or tokens.get('refreshToken'))),'clientId':self.config.get('clientId',''),'tenant':self.config.get('tenant','consumers'),'provider':'Personal OneDrive','unsupported':['OneDrive for Business/SharePoint','Google Drive','Dropbox'],'note':'Use your own public-client app registration with personal Microsoft accounts, device-code flow enabled, and delegated Files.ReadWrite/User.Read permissions. Credentials stay encrypted for this Windows account.'}
    def logout(self):
        self.store.clear();self._tokens=None;self._sessions.clear();self._plans.clear()
        return {'connected':False,'note':'Removed only the local encrypted Optimus token. Microsoft account consent can be revoked separately.'}
    def _oauth(self,endpoint,data):
        if not self.config.get('clientId'):raise ValueError('Configure your own registered public-client ID before connecting.')
        url=f"https://login.microsoftonline.com/{self.config['tenant']}/oauth2/v2.0/{endpoint}"
        _url(url,'login.microsoftonline.com')
        _,body=self.http('POST',url,headers={'Content-Type':'application/x-www-form-urlencoded'},data=data,timeout=10)
        if not isinstance(body,dict):raise RuntimeError('Microsoft returned unexpected authentication metadata.')
        return body
    def begin_connect(self):
        if not self.config.get('clientId'):raise ValueError('Configure your own registered public-client ID before connecting.')
        body=self._oauth('devicecode',{'client_id':self.config['clientId'],'scope':_SCOPES})
        uri=body.get('verification_uri','');parsed=parse.urlsplit(uri)
        if parsed.scheme!='https' or parsed.hostname not in ('microsoft.com','www.microsoft.com','login.microsoftonline.com','login.live.com') or parsed.username or parsed.password:raise RuntimeError('An unexpected sign-in website was blocked.')
        if not isinstance(body.get('device_code'),str) or not isinstance(body.get('user_code'),str):raise RuntimeError('Microsoft did not return a valid device sign-in request.')
        expiry=max(1,min(int(body.get('expires_in',900)),1800));interval=max(5,int(body.get('interval',5)))
        session=uuid.uuid4().hex
        self._sessions={session:{'deviceCode':body['device_code'],'expires':time.time()+expiry,'interval':interval,'nextPoll':time.time()+interval}}
        return {'sessionId':session,'userCode':body['user_code'],'verificationUri':uri,'expiresIn':expiry,'interval':interval,'note':'Open the Microsoft sign-in page and enter this code. Optimus never asks for your Microsoft password.'}
    def _accept(self,body,previous=None):
        token=body.get('access_token')
        if not isinstance(token,str) or not token or str(body.get('token_type','Bearer')).lower()!='bearer':raise RuntimeError('Microsoft did not return a usable access token.')
        scopes={part.rsplit('/',1)[-1].lower() for part in str(body.get('scope','')).split()}
        if not {'files.readwrite','user.read'}<=scopes:raise RuntimeError('The registered app lacks the requested delegated file permissions. Reconnect after reviewing its configuration.')
        value={'clientId':self.config['clientId'],'tenant':self.config['tenant'],'accessToken':token,'refreshToken':body.get('refresh_token') or (previous or {}).get('refreshToken'),'expires':time.time()+max(1,min(int(body.get('expires_in',3600)),86400)),'scopes':sorted(scopes)}
        self.store.save(value);self._tokens=value
    def poll_connect(self,session_id):
        session=self._sessions.get(session_id)
        if not session:raise ValueError('This device sign-in session is no longer active.')
        if time.time()>=session['expires']:
            self._sessions.pop(session_id,None);return {'state':'expired'}
        if time.time()<session['nextPoll']:return {'state':'pending','retryAfter':max(1,int(session['nextPoll']-time.time()+1))}
        try:body=self._oauth('token',{'grant_type':'urn:ietf:params:oauth:grant-type:device_code','client_id':self.config['clientId'],'device_code':session['deviceCode']})
        except CloudRequestError as failure:
            if failure.code in ('authorization_pending','slow_down'):
                if failure.code=='slow_down':session['interval']+=5
                session['nextPoll']=time.time()+session['interval'];return {'state':'pending','retryAfter':session['interval']}
            self._sessions.pop(session_id,None)
            if failure.code in ('authorization_declined','access_denied','expired_token'):return {'state':'declined' if failure.code!='expired_token' else 'expired'}
            raise
        self._accept(body);self._sessions.pop(session_id,None)
        return {'state':'connected','status':self.status()}
    def _access(self):
        tokens=self._load()
        if not tokens:raise RuntimeError('Connect your personal OneDrive account first.')
        if tokens.get('expires',0)<time.time()+60:
            if not tokens.get('refreshToken'):raise RuntimeError('Your Microsoft sign-in expired. Connect again.')
            body=self._oauth('token',{'grant_type':'refresh_token','client_id':self.config['clientId'],'refresh_token':tokens['refreshToken'],'scope':_SCOPES})
            self._accept(body,tokens);tokens=self._tokens
        return tokens['accessToken']
    def _graph(self,method,path,extra_headers=None,timeout=10):
        url=path if path.startswith('https://') else _GRAPH+path
        parsed=_url(url,'graph.microsoft.com')
        if not parsed.path.startswith('/v1.0/me/drive'):raise ValueError('Only this account personal OneDrive endpoint is supported.')
        headers={'Authorization':'Bearer '+self._access(),'Accept':'application/json'}
        headers.update(extra_headers or {})
        return self.http(method,url,headers=headers,timeout=max(.1,min(10,timeout)))
    def _drive(self,timeout=10):
        _,drive=self._graph('GET','/me/drive?$select=id,driveType,name',timeout=timeout)
        if drive.get('driveType')!='personal' or not isinstance(drive.get('id'),str):raise ValueError('This feature supports only the signed-in account personal OneDrive, not business drives, shared sites or teams.')
        return drive
    @staticmethod
    def _owned(item,drive_id,parent_id=None,root=False):
        parent=item.get('parentReference') or {}
        if any(item.get(key) is not None for key in ('remoteItem','shared','package','sharepointIds')):return False
        if parent.get('driveId') not in (None,drive_id):return False
        if not root and parent.get('driveId')!=drive_id:return False
        if parent_id is not None and parent.get('id')!=parent_id:return False
        return isinstance(item.get('id'),str) and bool(_ID.fullmatch(item['id']))
    @staticmethod
    def _hash(item):
        hashes=(item.get('file') or {}).get('hashes') or {}
        sha=hashes.get('sha1Hash')
        if isinstance(sha,str) and re.fullmatch(r'[A-Fa-f0-9]{40}',sha):return 'sha1Hash',sha.lower()
        xor=hashes.get('quickXorHash')
        if isinstance(xor,str):
            try:
                if len(base64.b64decode(xor,validate=True))==20:return 'quickXorHash',xor
            except ValueError:pass
        return None
    @staticmethod
    def _snapshot(item,drive_id,path):
        hashed=CloudDrive._hash(item)
        size=item.get('size')
        etag=item.get('eTag')
        if not hashed or not isinstance(size,int) or isinstance(size,bool) or size<=0 or not isinstance(etag,str) or not etag or any(c in etag for c in '\r\n'):return None
        return {'id':item['id'],'name':str(item.get('name','')),'path':path,'size':size,'etag':etag,'parentId':item['parentReference']['id'],'driveId':drive_id,'algorithm':hashed[0],'hash':hashed[1],'selected':False}
    def scan(self,folder_id='root',max_items=5000,max_seconds=45):
        encoded=_item_id(folder_id)
        if not isinstance(max_items,int) or not 1<=max_items<=5000 or not 1<=max_seconds<=45:raise ValueError('Cloud metadata scans are limited to 5000 items and 45 seconds.')
        deadline=time.monotonic()+max_seconds
        self._access()
        if time.monotonic()>=deadline:raise RuntimeError('The metadata scan time limit elapsed during sign-in refresh. Try again.')
        drive=self._drive(timeout=deadline-time.monotonic());drive_id=drive['id']
        path='/me/drive/root' if folder_id=='root' else '/me/drive/items/'+encoded
        if time.monotonic()>=deadline:raise RuntimeError('The metadata scan time limit elapsed. Try again.')
        _,root=self._graph('GET',path+'?$select='+_ITEM_SELECT,timeout=deadline-time.monotonic())
        if 'folder' not in root or not self._owned(root,drive_id,root=(folder_id=='root')):raise ValueError('Select an owned folder in this personal drive; shared/remote folders are excluded.')
        queue=deque([(root['id'],str(root.get('name','Root')))])
        seen={root['id']};files=[];examined=0;skipped=0;complete=True
        while queue and examined<max_items and time.monotonic()<deadline:
            parent_id,parent_path=queue.popleft()
            children='/me/drive/items/'+_item_id(parent_id)+'/children'
            next_url=_GRAPH+children+'?$top=200&$select='+_ITEM_SELECT
            page_seen=set()
            while next_url and examined<max_items and time.monotonic()<deadline:
                if next_url in page_seen:raise RuntimeError('Microsoft pagination repeated a page; scan stopped.')
                page_seen.add(next_url)
                parsed=_url(next_url,'graph.microsoft.com')
                if parse.unquote(parsed.path)!=parse.unquote('/v1.0'+children):raise ValueError('A pagination link outside the selected personal folder was blocked.')
                _,page=self._graph('GET',next_url,timeout=deadline-time.monotonic())
                if not isinstance(page.get('value'),list):raise RuntimeError('Microsoft returned an unexpected folder listing.')
                for item in page['value']:
                    if examined>=max_items:complete=False;break
                    examined+=1
                    if not isinstance(item,dict) or not self._owned(item,drive_id,parent_id) or item['id'] in seen:skipped+=1;continue
                    seen.add(item['id']);display=parent_path+'/'+str(item.get('name',''))
                    if 'folder' in item:queue.append((item['id'],display));continue
                    if 'file' in item:
                        snapshot=self._snapshot(item,drive_id,display)
                        if snapshot:files.append(snapshot)
                        else:skipped+=1
                next_url=page.get('@odata.nextLink')
                if next_url is not None and not isinstance(next_url,str):raise RuntimeError('Microsoft returned an invalid pagination link.')
            if next_url:complete=False
        if queue or examined>=max_items or time.monotonic()>=deadline:complete=False
        by_hash={}
        for item in files:by_hash.setdefault((item['size'],item['algorithm'],item['hash']),[]).append(item)
        groups=[];items={}
        for key,members in by_hash.items():
            if len(members)<2:continue
            group_id=hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
            for member in members:member['groupId']=group_id;items[member['id']]=member
            groups.append({'groupId':group_id,'size':key[0],'hashAlgorithm':key[1],'copies':len(members),'potentialBytes':key[0]*(len(members)-1),'items':members})
        plan_id=uuid.uuid4().hex
        self._plans={plan_id:{'created':time.time(),'driveId':drive_id,'rootId':root['id'],'items':json.loads(json.dumps(items)),'removed':set()}}
        return {'planId':plan_id,'drive':{'id':drive_id,'name':drive.get('name','Personal OneDrive')},'folder':{'id':root['id'],'name':root.get('name','Root')},'groups':groups,'examined':examined,'skipped':skipped,'complete':complete,'expiresIn':900,'note':'Metadata-only duplicate candidates use equal size plus provider SHA1 or QuickXor hashes, not a downloaded byte comparison. Hash collisions remain possible. Shared/remote/package files are excluded. Select one file at a time; at least one revalidated copy must remain. Recycled files can be restored from the OneDrive website recycle bin; cloud quota may not fall until retention expires. Keeper checks are separate requests, so concurrent remote changes cannot be made atomic.'}
    def _scope_check(self,item,cached):
        parent=(item.get('parentReference') or {}).get('id');seen=set()
        for _ in range(64):
            if parent==cached['rootId']:return
            if not isinstance(parent,str) or parent in seen:break
            seen.add(parent)
            _,folder=self._graph('GET','/me/drive/items/'+_item_id(parent)+'?$select='+_ITEM_SELECT)
            if 'folder' not in folder or not self._owned(folder,cached['driveId']):break
            parent=(folder.get('parentReference') or {}).get('id')
        raise ValueError('A retained or selected file is no longer inside the reviewed folder scope. Scan again.')

    def recycle(self,plan):
        if not isinstance(plan,dict) or plan.get('confirmed') is not True:raise ValueError('Explicitly confirm the displayed cloud file before recycling it.')
        plan_id=plan.get('planId')
        if not isinstance(plan_id,str) or not re.fullmatch(r'[a-f0-9]{32}',plan_id):raise ValueError('Invalid cloud preview identity.')
        cached=self._plans.get(plan_id)
        if not cached or time.time()-cached['created']>900:raise ValueError('The cloud preview expired or belongs to another session. Scan again.')
        selected=plan.get('selectedIds')
        if not isinstance(selected,list) or len(selected)!=1 or not isinstance(selected[0],str):raise ValueError('Select exactly one reviewed duplicate file per confirmation; bulk deletion is not supported.')
        item_id=selected[0]
        item=cached['items'].get(item_id)
        if not item or item_id in cached['removed']:raise ValueError('Select an available duplicate from this preview.')
        drive=self._drive()
        if drive['id']!=cached['driveId']:raise ValueError('The connected personal drive changed. Scan again.')
        keepers=[value for key,value in cached['items'].items() if key!=item_id and key not in cached['removed'] and value['groupId']==item['groupId']]
        if not keepers:raise ValueError('The last observed copy cannot be recycled.')
        # Validate the retained copy first and the selected copy immediately before DELETE.
        for expected in (keepers[0],item):
            _,fresh=self._graph('GET','/me/drive/items/'+_item_id(expected['id'])+'?$select='+_ITEM_SELECT)
            if 'file' not in fresh or not self._owned(fresh,cached['driveId'],expected['parentId']):raise ValueError('A selected file moved, became shared, or left the scanned folder. Scan again.')
            self._scope_check(fresh,cached)
            snapshot=self._snapshot(fresh,cached['driveId'],expected['path'])
            if not snapshot or any(snapshot[key]!=expected[key] for key in ('id','name','size','etag','parentId','driveId','algorithm','hash')):raise ValueError('A file or its retained copy changed since preview; nothing was recycled.')
        status,_=self._graph('DELETE','/me/drive/items/'+_item_id(item_id),extra_headers={'If-Match':item['etag']})
        if status!=204:raise RuntimeError('Microsoft did not confirm recycling this file. Refresh the scan before retrying.')
        cached['removed'].add(item_id)
        return {'id':item_id,'name':item['name'],'state':'recycled','bytes':item['size'],'note':'Sent to the personal OneDrive recycle bin. Restore from the OneDrive website. Optimus did not empty the recycle bin or download file contents.'}
