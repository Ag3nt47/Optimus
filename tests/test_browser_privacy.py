from pathlib import Path
from contextlib import closing
import hashlib,json,sqlite3,tempfile,types,unittest
from unittest.mock import patch
import sys
try:
 import psutil
except ImportError:
 class FixturePsutilError(Exception): pass
 class FixtureAccessDenied(FixturePsutilError): pass
 sys.modules['psutil']=types.SimpleNamespace(process_iter=lambda fields:[],Error=FixturePsutilError,AccessDenied=FixtureAccessDenied)
source=Path(__file__).resolve().parents[1]/'src'/'optimus'/'browser_privacy.py'
module=types.ModuleType('browser_privacy_isolated')
exec(compile(source.read_text(encoding='ascii'),str(source),'exec'),module.__dict__)
BrowserPrivacy=module.BrowserPrivacy

def chrome_fixture(p):
 p.mkdir(parents=True)
 with closing(sqlite3.connect(p/'History')) as db:
  db.executescript('CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT); CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER); CREATE TABLE visit_source(id INTEGER,source INTEGER); CREATE TABLE keyword_search_terms(url_id INTEGER,term TEXT); CREATE TABLE downloads(id INTEGER PRIMARY KEY,target_path TEXT); CREATE TABLE downloads_url_chains(id INTEGER,chain_index INTEGER,url TEXT); INSERT INTO urls VALUES(1,"https://private-history.fixture"); INSERT INTO visits VALUES(1,1); INSERT INTO visit_source VALUES(1,1); INSERT INTO keyword_search_terms VALUES(1,"private-search.fixture"); INSERT INTO downloads VALUES(1,"fixture-download.bin"); INSERT INTO downloads_url_chains VALUES(1,0,"https://private-download.fixture");');db.commit()
 (p/'Network').mkdir()
 with closing(sqlite3.connect(p/'Network'/'Cookies')) as db:
  db.execute('CREATE TABLE cookies(host_key TEXT,name TEXT,value TEXT,encrypted_value BLOB)')
  db.executemany('INSERT INTO cookies VALUES(?,?,?,?)',[(h,'fixture-name','private-cookie.fixture',b'fixture-encrypted') for h in ['.example.com','sub.example.com','example.com','badexample.com','example.com.evil.test','other.test']]);db.commit()
 (p/'Bookmarks').write_text('{"fixture":"preserve-bookmark"}')
 with closing(sqlite3.connect(p/'Login Data')) as db:
  db.execute('CREATE TABLE logins(username_value TEXT,password_value BLOB)');db.execute('INSERT INTO logins VALUES(?,?)',('preserve-user',b'preserve-password'));db.commit()

def firefox_fixture(p):
 p.mkdir(parents=True)
 with closing(sqlite3.connect(p/'places.sqlite')) as db:
  db.executescript('CREATE TABLE moz_places(id INTEGER PRIMARY KEY,url TEXT,visit_count INTEGER,last_visit_date INTEGER); CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY,place_id INTEGER); CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY,fk INTEGER,title TEXT); INSERT INTO moz_places VALUES(1,"https://bookmarked.fixture",5,1234); INSERT INTO moz_places VALUES(2,"https://private-history.fixture",3,4567); INSERT INTO moz_historyvisits VALUES(1,1); INSERT INTO moz_historyvisits VALUES(2,2); INSERT INTO moz_bookmarks VALUES(1,1,"preserve-bookmark");');db.commit()
 with closing(sqlite3.connect(p/'cookies.sqlite')) as db:
  db.executescript('CREATE TABLE moz_cookies(host TEXT,name TEXT,value TEXT); INSERT INTO moz_cookies VALUES("example.com","fixture","private-cookie.fixture"); INSERT INTO moz_cookies VALUES("other.test","fixture","private-cookie.fixture");');db.commit()
 (p/'logins.json').write_text('{"fixture":"preserve-password"}');(p/'key4.db').write_bytes(b'preserve-password-key')

class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='optimus-browser-privacy-test-');self.root=Path(self.temp.name);self.profiles_root=self.root/'Profiles';self.chrome=self.profiles_root/'Default';self.firefox=self.profiles_root/'test.firefox';chrome_fixture(self.chrome);firefox_fixture(self.firefox)
  self.tool=BrowserPrivacy(self.root/'Data');self.tool._discover=lambda:[{'browser':'Google Chrome','name':'Default','path':str(self.chrome),'root':str(self.profiles_root),'family':'chromium','process':'chrome.exe'},{'browser':'Mozilla Firefox','name':'test.firefox','path':str(self.firefox),'root':str(self.profiles_root),'family':'firefox','process':'firefox.exe'}];self.tool._closed=lambda p:None
  profiles=self.tool.profiles();self.cid=profiles[0]['id'];self.fid=profiles[1]['id']
 def tearDown(self):self.temp.cleanup()
 def count(self,path,table):
  with closing(sqlite3.connect(path)) as db:return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
 def digest(self,path):return hashlib.sha256(path.read_bytes()).hexdigest()
 def apply(self,plan,**kw):return self.tool.apply(dict(planId=plan['planId'],confirmed=True,**kw))
 def test_01_scan_count_only_readonly(self):
  before=self.digest(self.chrome/'History');plan=self.tool.scan(self.cid,keep_domains=['example.com'])
  self.assertEqual(plan['counts'],{'history':1,'cookies':3,'downloads':1,'cookiesKept':3})
  text=json.dumps(plan)
  for private in ['private-history.fixture','private-cookie.fixture','private-search.fixture']:self.assertNotIn(private,text)
  self.assertEqual(before,self.digest(self.chrome/'History'));self.assertFalse(self.tool.backup_root.exists())
 def test_02_browser_blocks_before_db(self):
  del self.tool._closed
  with patch.object(module.psutil,'process_iter',return_value=[types.SimpleNamespace(info={'pid':999,'name':'chrome.exe'})]),patch.object(module.sqlite3,'connect') as connect:
   with self.assertRaises(RuntimeError):self.tool.scan(self.cid)
   connect.assert_not_called()
 def test_03_chromium_cleanup_backup_restore(self):
  passwords=self.digest(self.chrome/'Login Data');bookmarks=self.digest(self.chrome/'Bookmarks');plan=self.tool.scan(self.cid,keep_domains=['example.com']);result=self.apply(plan)
  self.assertEqual(result['state'],'applied')
  for t in ['visits','urls','downloads']:self.assertEqual(self.count(self.chrome/'History',t),0)
  self.assertEqual(self.count(self.chrome/'Network'/'Cookies','cookies'),3)
  self.assertEqual(passwords,self.digest(self.chrome/'Login Data'));self.assertEqual(bookmarks,self.digest(self.chrome/'Bookmarks'))
  self.assertEqual(len(self.tool.list_backups()),1);self.tool.restore_backup(result['backupId'],confirmed=True)
  self.assertEqual(self.count(self.chrome/'History','visits'),1);self.assertEqual(self.count(self.chrome/'Network'/'Cookies','cookies'),6)
  self.assertEqual(passwords,self.digest(self.chrome/'Login Data'));self.assertEqual(bookmarks,self.digest(self.chrome/'Bookmarks'))
 def test_04_firefox_bookmarks_places_passwords(self):
  passwords=self.digest(self.firefox/'logins.json');key=self.digest(self.firefox/'key4.db');plan=self.tool.scan(self.fid);self.assertEqual(plan['unsupported'][0]['category'],'downloads');self.apply(plan)
  self.assertEqual(self.count(self.firefox/'places.sqlite','moz_historyvisits'),0);self.assertEqual(self.count(self.firefox/'places.sqlite','moz_places'),2);self.assertEqual(self.count(self.firefox/'places.sqlite','moz_bookmarks'),1)
  with closing(sqlite3.connect(self.firefox/'places.sqlite')) as db:self.assertEqual(db.execute('SELECT visit_count,last_visit_date FROM moz_places').fetchall(),[(0,None),(0,None)])
  self.assertEqual(passwords,self.digest(self.firefox/'logins.json'));self.assertEqual(key,self.digest(self.firefox/'key4.db'))
 def test_05_download_metadata_only(self):
  downloaded=self.root/'fixture-download.bin';downloaded.write_bytes(b'keep actual download');self.apply(self.tool.scan(self.cid,categories=['downloads']))
  self.assertEqual(self.count(self.chrome/'History','downloads'),0);self.assertEqual(self.count(self.chrome/'History','visits'),1);self.assertEqual(downloaded.read_bytes(),b'keep actual download')
 def test_06_category_subset(self):
  cookies=self.digest(self.chrome/'Network'/'Cookies');self.apply(self.tool.scan(self.cid),selectedCategories=['history'])
  self.assertEqual(self.count(self.chrome/'History','visits'),0);self.assertEqual(self.count(self.chrome/'History','downloads'),1);self.assertEqual(cookies,self.digest(self.chrome/'Network'/'Cookies'))
 def test_07_confirmation_session(self):
  plan=self.tool.scan(self.cid)
  with self.assertRaises(ValueError):self.tool.apply({'planId':plan['planId']})
  with self.assertRaises(ValueError):BrowserPrivacy(self.root/'Other').apply({'planId':plan['planId'],'confirmed':True})
  self.assertEqual(self.count(self.chrome/'History','visits'),1)
 def test_08_expired_preview(self):
  plan=self.tool.scan(self.cid);self.tool._plans[plan['planId']]['created']-=1000
  with self.assertRaises(ValueError):self.apply(plan)
 def test_09_changed_db_blocks(self):
  plan=self.tool.scan(self.cid)
  with closing(sqlite3.connect(self.chrome/'History')) as db:db.execute('INSERT INTO urls VALUES(2,"https://new.fixture")');db.commit()
  with self.assertRaises(ValueError):self.apply(plan)
  self.assertEqual(self.count(self.chrome/'History','visits'),1)
 def test_10_new_data_blocks_restore(self):
  result=self.apply(self.tool.scan(self.cid))
  with closing(sqlite3.connect(self.chrome/'History')) as db:db.execute('INSERT INTO urls VALUES(2,"https://new.fixture")');db.commit()
  with self.assertRaises(ValueError):self.tool.restore_backup(result['backupId'],confirmed=True)
  self.assertEqual(self.count(self.chrome/'History','urls'),1)
 def test_11_unknown_schema(self):
  with closing(sqlite3.connect(self.chrome/'Network'/'Cookies')) as db:db.execute('ALTER TABLE cookies RENAME TO unsupported');db.commit()
  with self.assertRaises(ValueError):self.tool.scan(self.cid,categories=['cookies'])
  self.assertFalse(self.tool.backup_root.exists())
 def test_12_domain_validation_boundary(self):
  for bad in ['https://example.com','*.example.com',"x' OR 1=1 --",'example.com/path']:
   with self.assertRaises(ValueError):self.tool.scan(self.cid,categories=['cookies'],keep_domains=[bad])
  self.assertEqual(self.tool.scan(self.cid,categories=['cookies'],keep_domains=['.EXAMPLE.com'])['counts']['cookiesKept'],3)
 def test_13_pending_wal_blocks_before_db(self):
  (self.chrome/'History-wal').write_bytes(b'pending fixture')
  with patch.object(module.sqlite3,'connect') as connect:
   with self.assertRaises(RuntimeError):self.tool.scan(self.cid,categories=['history'])
   connect.assert_not_called()
 def test_14_backup_tamper(self):
  result=self.apply(self.tool.scan(self.cid,categories=['history']));backup=self.tool.backup_root/result['backupId']/'database-0.sqlite'
  with backup.open('ab') as stream:stream.write(b'tamper')
  with self.assertRaises(ValueError):self.tool.restore_backup(result['backupId'],confirmed=True)
 def test_15_caller_cannot_add_categories_paths(self):
  plan=self.tool.scan(self.cid,categories=['cookies'])
  with self.assertRaises(ValueError):self.apply(plan,selectedCategories=['history'])
  self.apply(plan,path='C:\\unapproved',categories=['history']);self.assertEqual(self.count(self.chrome/'History','visits'),1)
 def test_16_unknown_trigger(self):
  with closing(sqlite3.connect(self.chrome/'History')) as db:db.execute('CREATE TRIGGER bad AFTER DELETE ON visits BEGIN DELETE FROM downloads; END;');db.commit()
  with self.assertRaises(ValueError):self.tool.scan(self.cid,categories=['history'])
 def test_17_restore_confirmation(self):
  result=self.apply(self.tool.scan(self.cid,categories=['cookies']))
  with self.assertRaises(ValueError):self.tool.restore_backup(result['backupId'])
 def test_18_browser_restarts_before_delete(self):
  plan=self.tool.scan(self.cid,categories=['history'])
  with patch.object(self.tool,'_closed',side_effect=[None,RuntimeError('fixture restarted')]):
   with self.assertRaises(RuntimeError):self.apply(plan)
  self.assertEqual(self.count(self.chrome/'History','visits'),1);self.assertEqual(self.tool.list_backups()[0]['state'],'failed')

if __name__=='__main__':unittest.main(verbosity=2)
