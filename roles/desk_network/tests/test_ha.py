from unittest.mock import MagicMock,patch
import io,threading,tracemalloc
import re,subprocess,shutil,base64,gzip,hashlib,importlib.util,json,pathlib,sqlite3,tempfile,unittest
spec=importlib.util.spec_from_file_location('ha',str(pathlib.Path(__file__).resolve().parents[1]/'files/ha.py'));ha=importlib.util.module_from_spec(spec);spec.loader.exec_module(ha)
class Checks(unittest.TestCase):
 def setUp(self):
  # Existing protocol fixtures exercise child isolation on non-systemd CI hosts.
  # The production systemd runner/failure paths have separate budget tests.
  runner=patch.object(ha.checkpoint_budget,'execute',side_effect=lambda command,blob=None:subprocess.run(command,input=blob,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,timeout=60))
  runner.start();self.addCleanup(runner.stop)
 def test_native_api_namespace_reaches_authenticated_loopback_service(self):
  self.assertEqual(ha.native_route('10.10.10.11','/server/device/aa:bb:cc:dd:ee:ff?period=1'),('127.0.0.1',20212,'/device/aa:bb:cc:dd:ee:ff?period=1'))
  self.assertEqual(ha.native_route('10.10.10.11','/php/server/util.php'),('10.10.10.11',20211,'/php/server/util.php'))
  self.assertEqual(ha.native_route('10.10.10.11','/server.js'),('10.10.10.11',20211,'/server.js'))
  body=json.dumps({'columnName':'devAlertEvents','columnValue':0}).encode()
  self.assertTrue(ha.is_mutation('POST','/network/server/device/aa:bb:cc:dd:ee:ff/update-column',body))
 def test_presence_and_events_wait_for_native_cache_bootstrap(self):
  for page in ('presence.php','events.php'):
   raw=b'<html><head></head><body><script>\nlet tableRows=NaN;\nmain(); // Initialize page\nfunction main(){calls++;}\n</script></body></html>'
   result=ha.rewrite_text(raw,'text/html',path='/network/'+page).decode()
   script=re.search(r'<script>\n(.*?)</script>',result,re.S)[1]
   self.assertIn('callAfterAppInitialized(',script)
   if shutil.which('node'):
    setup="let calls=0,pending;function callAfterAppInitialized(callback){pending=callback;}const parTableRows='rows';function getCookie(){return '';}function getSetting(){return '20';};"
    check=";if(calls!==0)throw Error('API ran before bootstrap');pending();if(calls!==1)throw Error('initialization lost');"
    if page=='events.php':check+="if(tableRows!==20)throw Error('page size read before settings');"
    subprocess.run(['node','-e',setup+script+check],check=True,capture_output=True)
  raw=b'<html><head></head><body><script>\nmain();\n</script></body></html>'
  self.assertIn(b'\nmain();',ha.rewrite_text(raw,'text/html',path='/network/other.php'))
 def test_api_fallback_base_is_prefixed_before_settings_cache_is_ready(self):
  raw=b'function getApiBase(){let apiBase="/server";return apiBase.replace(/\\/$/, "");} const external="https://example.com/server";const prefixed="/network/server";'
  result=ha.rewrite_text(raw,'application/javascript',path='/network/js/api.js')
  self.assertIn(b'let apiBase="/network/server"',result)
  self.assertIn(b'https://example.com/server',result)
  self.assertNotIn(b'/network/network/',result)
  if shutil.which('node'):subprocess.run(['node','--check'],input=result,check=True,capture_output=True)
 def test_expandable_sidebar_parents_do_not_navigate(self):
  raw=b"""<html><head></head><body><a href="devices.php" class="logo">Logo</a><a href="devices.php" onclick="openUrl(['./devices.php', './deviceDetails.php'])">Devices</a><a href="settings.php" onclick="openUrl(['./settings.php'])">Settings</a><a href="devices.php#connected" onclick="forceLoadUrl('devices.php#connected')">Connected</a></body></html>"""
  result=ha.rewrite_text(raw,'text/html',path='/network/presence.php').decode()
  self.assertIn('<a href="#" role="button">Devices</a>',result)
  self.assertIn('<a href="#" role="button">Settings</a>',result)
  self.assertIn('<a href="devices.php" class="logo">Logo</a>',result)
  self.assertIn("forceLoadUrl('devices.php#connected')",result)
  self.assertNotIn('openUrl(',result)
 def test_cluster_controls_live_in_sidebar_without_extra_fixed_header(self):
  raw=b'<html><head></head><body class="fixed"><header class="main-header"></header><ul class="sidebar-menu" data-widget="tree"></ul><div id="settingsPage" class="content-wrapper spinnerTarget">Settings</div><script src="lib/treeviz/treeviz.iife.js"></script><script src="js/api.js?v=26.10.0"></script></body></html>'
  result=ha.rewrite_text(raw,'text/html',path='/network/settings.php').decode()
  self.assertNotIn('padding-top:44px',result);self.assertNotIn('top:44px',result)
  self.assertNotIn('desk-network-bar',result)
  self.assertIn('<ul class="sidebar-menu" data-widget="tree"><li id="desk-network-tools">',result)
  self.assertEqual(result.count('id="desk-network-tools"'),1)
  self.assertIn('<div id="settingsPage" class="content-wrapper spinnerTarget"><aside id="desk-settings-policy"',result)
  self.assertIn('treeviz.iife.js?desk=library-only-v1',result)
  self.assertIn('js/api.js?desk=base-v1&v=26.10.0',result)
 def test_sidebar_tab_helpers_are_guarded_on_foreign_pages(self):
  raw=b'<html><head></head><body><a onclick="setCache(1,2);initializeTabs()">System</a><a onclick="initializeTabs()">Tools</a><script>function initializeTabs(){}</script></body></html>'
  result=ha.rewrite_text(raw,'text/html',path='/network/workflows.php').decode()
  handlers=re.findall(r'onclick="([^"]*)"',result)
  self.assertEqual(len(handlers),2)
  self.assertTrue(all("typeof initializeTabs==='function'" in x for x in handlers))
  self.assertIn('function initializeTabs(){}',result)
  if shutil.which('node'):
   for handler in handlers:
    js="let calls=0;function setCache(){};"+handler+";function test(){function initializeTabs(){calls++};"+handler+"};test();if(calls!==1)throw Error('handler lost');"
    subprocess.run(['node','-e',js],check=True,capture_output=True)
 def test_treeviz_demo_removal_is_pinned_to_exact_asset(self):
  raw=b'(function(){window.Treeviz={create(){}};var ft=[{id:1,text_1:"Chaos"}];throw Error("demo");})();'
  path='/network/lib/treeviz/treeviz.iife.js?v=26.10.0'
  self.assertEqual(ha.rewrite_text(raw,'application/javascript',path=path),raw)
  with patch.object(ha,'sha',return_value='577d2115d592e0eaa8bfaa1080e7960d5719dc0028fa371fc866562b5a616818'):
   result=ha.rewrite_text(raw,'application/javascript',path=path)
   self.assertIn(b'window.Treeviz',result);self.assertNotIn(b'Chaos',result);self.assertNotIn(b'throw Error',result)
   self.assertEqual(ha.rewrite_text(raw,'application/javascript',path='/network/js/other.js'),raw)
   if shutil.which('node'):subprocess.run(['node','--check'],input=result,check=True,capture_output=True)
 def test_partition_and_restart_never_double_grant(self):
  with tempfile.TemporaryDirectory() as td:
   t=[100.];lease=ha.Lease(pathlib.Path(td)/'vote',lambda:t[0])
   with self.assertRaises(ha.Unavailable):lease.grant([1,'a'],'a')
   t[0]+=18;lease.grant([1,'a'],'a')
   with self.assertRaises(ha.Unavailable):lease.grant([2,'b'],'b')
   t[0]+=10;lease.grant([1,'a'],'a')
   t[0]+=10
   with self.assertRaises(ha.Unavailable):lease.grant([2,'b'],'b')
   # Voter restart loses RAM grants, so quarantine must outlast the old TTL.
   reboot=ha.Lease(pathlib.Path(td)/'vote',lambda:t[0])
   with self.assertRaises(ha.Unavailable):reboot.grant([2,'b'],'b')
   t[0]+=18;reboot.grant([2,'b'],'b')
   with self.assertRaises(ha.Unavailable):reboot.grant([1,'a'],'a')
 def fixture(self,root):
  data=root/'data';(data/'db').mkdir(parents=True);(data/'config').mkdir()
  (data/'config/app.conf').write_text("SCAN_SUBNETS=['--localnet']\nTIMEZONE='Europe/Berlin'\n")
  db=sqlite3.connect(data/'db/app.db');db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE devices(name TEXT)');db.execute("INSERT INTO devices VALUES ('test-device')");db.commit()
  return data,db
 def test_snapshot_worker_roundtrip_and_validation_fail_closed(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);data,db=self.fixture(root)
   # Include enough data to exercise compression and JSON/base64 in the child.
   db.execute('CREATE TABLE bulk(data BLOB)')
   db.execute('INSERT INTO bulk VALUES (zeroblob(6000000))');db.commit()
   tracemalloc.start()
   try:
    blob=ha.snapshot_worker('create',data=data,run=root/'ram')
    ha.snapshot_worker('validate',blob=blob)
    _,peak=tracemalloc.get_traced_memory()
    self.assertLess(peak,4*1024*1024,'Checkpoint scratch memory leaked into the controller')
   finally:tracemalloc.stop();db.close()
   self.assertTrue(ha.unpack(blob)[0].startswith(b'SQLite format 3\x00'))
   with self.assertRaises(subprocess.CalledProcessError):ha.snapshot_worker('validate',blob=b'bad checkpoint')
 def test_stage_validates_before_persisting_or_signing(self):
  with tempfile.TemporaryDirectory() as td:
   store=ha.Store(td,b'test',['a','b','c']);blob=b'not a checkpoint';meta={'revision':[1,'a',1],'digest':ha.sha(blob)}
   with self.assertRaises(subprocess.CalledProcessError):store.stage(meta,blob,'a')
   self.assertEqual(list(pathlib.Path(td).iterdir()),[])
 def test_snapshot_captures_wal_and_rejects_tampering(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);data,db=self.fixture(root);blob=ha.checkpoint(data,root/'ram');database,conf,workflows=ha.unpack(blob)
   restored=root/'restored.db';restored.write_bytes(database)
   with sqlite3.connect(restored) as c:self.assertEqual(c.execute('SELECT name FROM devices').fetchone()[0],'test-device')
   db.close();key=b'test-key';store=ha.Store(root/'store',key,['a','b','c']);meta={'revision':[1,'a',1],'digest':ha.sha(blob)}
   receipt=store.stage(meta,blob,'a');meta['certificate']=[receipt]
   with self.assertRaises(ha.Unavailable):store.commit(meta)
   meta['certificate'].append(store.receipt('b',meta));store.commit(meta)
   bad=dict(meta,digest='0'*64)
   with self.assertRaises(ha.Unavailable):store.commit(bad)
   with self.assertRaises(ha.Unavailable):store.stage(meta,blob+b'corrupt','c')
 def test_workflows_are_included_without_arbitrary_paths(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);data,db=self.fixture(root)
   self.assertIsNone(ha.unpack(ha.checkpoint(data,root/'ram'))[2])
   (data/'config/workflows.json').write_text('[{"name":"disabled test","enabled":"No"}]')
   blob=ha.checkpoint(data,root/'ram');self.assertEqual(json.loads(ha.unpack(blob)[2])[0]['name'],'disabled test')
   db.close()
   bad=json.loads(gzip.decompress(blob));bad['../../credential']='eA=='
   with self.assertRaises(ha.Unavailable):ha.unpack(gzip.compress(json.dumps(bad).encode()))
 def test_bound_decompression(self):
  with self.assertRaises(ha.Unavailable):ha.unpack(gzip.compress(b'x'*(ha.DBLIMIT*2+2)))
 def test_small_checkpoint_does_not_reserve_full_decompression_limit(self):
  raw=json.dumps({'database':base64.b64encode(b'SQLite format 3\x00'+b'x'*100000).decode(),'configuration':''}).encode()
  blob=gzip.compress(raw)
  tracemalloc.start()
  try:
   ha.unpack(blob);current,peak=tracemalloc.get_traced_memory()
   self.assertLess(peak,4*1024*1024,'Small checkpoint reserved the 128 MiB limit')
  finally:tracemalloc.stop()
 def test_bounded_reader_caps_each_read_and_rejects_overflow(self):
  class Stream(io.BytesIO):
   def read(self,n=-1):
    self_read_sizes.append(n);return super().read(n)
  self_read_sizes=[]
  self.assertEqual(ha.read_bounded(Stream(b'ok'),32*1024*1024),b'ok')
  self.assertLessEqual(max(self_read_sizes),65536)
  with self.assertRaises(ha.Unavailable):ha.read_bounded(io.BytesIO(b'over'),3)
 def test_checkpoint_closes_sqlite_handles_even_on_validation_failure(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);data,db=self.fixture(root);handles=[];connect=sqlite3.connect
   def opened(*args,**kwargs):
    c=connect(*args,**kwargs);handles.append(c);return c
   with patch.object(ha.sqlite3,'connect',side_effect=opened):ha.checkpoint(data,root/'ram')
   self.assertEqual(len(handles),2)
   for c in handles:
    with self.assertRaises(sqlite3.ProgrammingError):c.execute('select 1')
   handles.clear();(data/'config/workflows.json').write_text('invalid json')
   with patch.object(ha.sqlite3,'connect',side_effect=opened):
    with self.assertRaises(json.JSONDecodeError):ha.checkpoint(data,root/'ram')
   for c in handles:
    with self.assertRaises(sqlite3.ProgrammingError):c.execute('select 1')
   db.close()
 def test_restore_closes_integrity_check_database(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);data,db=self.fixture(root);blob=ha.checkpoint(data,root/'ram');db.close();handles=[];connect=sqlite3.connect
   def opened(*args,**kwargs):
    c=connect(*args,**kwargs);handles.append(c);return c
   with patch.object(ha.sqlite3,'connect',side_effect=opened),patch.object(ha.os,'chown'):
    ha.restore(blob,root/'target',root/'restore','test-token')
   self.assertEqual(len(handles),1)
   with self.assertRaises(sqlite3.ProgrammingError):handles[0].execute('select 1')
 def test_policy_survives_conf_from_old_owner(self):
  raw=ha.apply_policy(b"SCAN_SUBNETS=['10.77.0.0/24']\nNSLOOKUP_RUN='before_name_updates'\n",'test-token')
  self.assertNotIn(b'10.77.0.0',raw);self.assertIn(b'192.168.1.0/24 --interface=wlan0',raw);self.assertIn(b"NSLOOKUP_RUN='disabled'",raw)
 def test_scan_schedule_is_mutable_and_survives_restore_policy(self):
  conf=b"ARPSCAN_RUN_SCHD='*/1 * * * *'\nNSLOOKUP_RUN='before_name_updates'\n"
  saved=ha.apply_policy(conf,'test-token')
  self.assertEqual(ha.scan_schedule(saved),('*/1 * * * *',1))
  self.assertNotIn('ARPSCAN_RUN_SCHD',ha.POLICY)
  self.assertEqual(ha.scan_schedule(ha.apply_policy(b'', 'test-token')),('*/5 * * * *',5))
  self.assertEqual(ha.scan_schedule(b"ARPSCAN_RUN_SCHD='0 3 * * *'\n"),('0 3 * * *',0))
  self.assertEqual(ha.scan_schedule(b"ARPSCAN_RUN_SCHD=__import__('os').system('false')\n"),('',0))
 def test_saved_cron_survives_certified_checkpoint_and_restore(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);data,db=self.fixture(root)
   with (data/'config/app.conf').open('ab') as f:f.write(b"ARPSCAN_RUN_SCHD='*/1 * * * *'\n")
   blob=ha.checkpoint(data,root/'ram');db.close();store=ha.Store(root/'store',b'test',['a','b','c']);meta={'revision':[1,'a',1],'digest':ha.sha(blob)}
   meta['certificate']=[store.stage(meta,blob,'a'),store.receipt('b',meta)];store.commit(meta)
   with patch.object(ha.os,'chown'):ha.restore(store.blob(store.head['digest']),root/'promoted',root/'restore','test-token')
   self.assertEqual(ha.scan_schedule((root/'promoted/config/app.conf').read_bytes()),('*/1 * * * *',1))
 def test_managed_changes_are_rejected_before_native_save(self):
  def payload(key,value):return ha.urllib.parse.urlencode({'function':'savesettings','settings':json.dumps([['General',key,'string',value]])}).encode()
  path='/network/php/server/util.php'
  self.assertIsNone(ha.managed_settings_error(path,payload('ARPSCAN_RUN_SCHD','*/1 * * * *'),'test-token'))
  self.assertIsNone(ha.managed_settings_error(path,payload('UI_THEME','Dark'),'test-token'))
  self.assertIsNone(ha.managed_settings_error(path,payload('LOADED_PLUGINS',list(reversed(ha.POLICY['LOADED_PLUGINS']))),'test-token'))
  self.assertIsNone(ha.managed_settings_error(path,payload('SCAN_SUBNETS',ha.POLICY['SCAN_SUBNETS']),'test-token'))
  self.assertIn('SCAN_SUBNETS',ha.managed_settings_error(path,payload('SCAN_SUBNETS',['10.77.0.0/24']),'test-token'))
  self.assertIn('API_TOKEN',ha.managed_settings_error(path,payload('API_TOKEN','different'),'test-token'))
  self.assertIsNone(ha.managed_settings_error(path,payload('SETPWD_enable_password','False'),'test-token'))
  self.assertIn('Invalid',ha.managed_settings_error(path,b'function=savesettings&settings=bad','test-token'))
 def test_settings_page_explains_managed_controls_without_changing_other_pages(self):
  raw=b'<html><head></head><body></body></html>'
  settings=ha.rewrite_text(raw,'text/html','deskb1x','/network/settings.php')
  self.assertIn(b'Cluster-managed controls are marked and locked',settings)
  self.assertIn(b'/network/desk/settings-policy.js',settings)
  self.assertNotIn(b'desk-settings-policy',ha.rewrite_text(raw,'text/html','deskb1x','/network/devices.php'))
 def test_mutation_classification(self):
  self.assertTrue(ha.is_mutation('POST','/network/server/device',b'{}'))
  self.assertTrue(ha.is_mutation('GET','/network/php/server/util.php?function=savesettings',b''))
  self.assertTrue(ha.is_mutation('POST','/network/server/graphql',b'{"query":"mutation { deleteDevice }"}'))
  self.assertFalse(ha.is_mutation('POST','/network/server/graphql',b'{"query":"query { devices { name } }"}'))
  self.assertFalse(ha.is_mutation('GET','/network/devices.php',b''))
 def test_native_post_reads_do_not_checkpoint_or_bypass_unknown_writes(self):
  for route in ('/network/php/components/device_cards.php','/network/php/components/tile_cards.php'):
   self.assertFalse(ha.is_mutation('POST',route,b'{}'))
   self.assertTrue(ha.is_mutation('PUT',route,b'{}'))
   self.assertTrue(ha.is_mutation('POST',route+'/unknown',b'{}'))
  path='/network/server/dbquery/read'
  for sql in ('SELECT * FROM Devices',' SELECT DISTINCT devType FROM Devices'):
   body=json.dumps({'rawSql':base64.b64encode(sql.encode()).decode()}).encode()
   self.assertFalse(ha.is_mutation('POST',path,body))
  for sql in ('DELETE FROM Devices','UPDATE Devices SET devName=1','PRAGMA journal_mode=WAL','WITH x AS (SELECT 1) DELETE FROM Devices'):
   body=json.dumps({'rawSql':base64.b64encode(sql.encode()).decode()}).encode()
   self.assertTrue(ha.is_mutation('POST',path,body))
  for body in (b'bad',b'{}',b'{"rawSql":null}',b'{"rawSql":"!!"}'):
   self.assertTrue(ha.is_mutation('POST',path,body))
  self.assertTrue(ha.is_mutation('POST','/network/server/dbquery/write',b'{}'))
 def test_prefix_preserves_external_and_already_prefixed_urls(self):
  raw=b'<a href="/deviceDetails.php">Device</a><a href="/network/devices.php">N</a><a href="https://example.com/a">X</a>'
  result=ha.rewrite_text(raw,'text/html');self.assertIn(b'href="/network/deviceDetails.php"',result);self.assertNotIn(b'/network/network/',result);self.assertIn(b'https://example.com/a',result)
 def test_storage_reset_cannot_erase_sibling_ui_state(self):
  if not shutil.which('node'):self.skipTest('Node.js unavailable')
  page=ha.rewrite_text(b'<html><head></head><body></body></html>','text/html','deskb1x').decode()
  script=re.search(r'<head><script>(.*?)</script>',page).group(1)
  test="""const vm=require('vm'),assert=require('assert');
function store(initial){const s={...initial};Object.defineProperties(s,{getItem:{value:k=>s[k]??null},setItem:{value:(k,v)=>{s[k]=String(v)}},removeItem:{value:k=>{delete s[k]}}});return s;}
const local=store({'beszel':'keep','desk_network:old':'remove'}), session=store({'xiaozhi':'keep'});
const context={window:{localStorage:local,sessionStorage:session}};
vm.runInNewContext(SCRIPT,context);const isolated=context.window.deskNetworkLocalStorage;
isolated.clear();assert.equal(local.beszel,'keep');assert.equal(local['desk_network:old'],undefined);
isolated.setItem('key','value');assert.equal(isolated.getItem('key'),'value');assert.equal(isolated.length,1);
context.window.deskNetworkSessionStorage.clear();assert.equal(session.xiaozhi,'keep');
""".replace('SCRIPT',json.dumps(script))
  subprocess.run(['node','-e',test],check=True,capture_output=True)
 def test_peer_handshake_is_bounded_in_worker_not_accept_loop(self):
  server=object.__new__(ha.Server);server.tls_context=MagicMock();server.slots=MagicMock()
  connection=MagicMock();server.tls_context.wrap_socket.side_effect=TimeoutError()
  with patch.object(ha.http.server.ThreadingHTTPServer,'get_request',return_value=(connection,('10.10.10.11',1))):
   server.get_request()
  server.tls_context.wrap_socket.assert_not_called()
  server.process_request_thread(connection,('10.10.10.11',1))
  connection.settimeout.assert_any_call(2);server.slots.release.assert_called_once()
 def test_idle_probe_resets_are_quiet_but_other_errors_are_logged(self):
  server=object.__new__(ha.Server)
  with patch.object(ha.http.server.ThreadingHTTPServer,'handle_error') as log:
   try:raise ConnectionResetError()
   except ConnectionResetError:server.handle_error(None,None)
   log.assert_not_called()
   try:raise RuntimeError('unexpected')
   except RuntimeError:server.handle_error(None,None)
   log.assert_called_once()
 def test_static_browser_cache_excludes_application_data(self):
  config=(pathlib.Path(__file__).resolve().parents[1]/'templates/backend.cfg.j2').read_text()
  pattern=re.search(r'path_reg -i (\S+) ',config).group(1)
  for path in ('/network/js/api.js','/network/css/dark-patch.css','/network/lib/fontawesome/webfonts/fa.woff2','/network/img/logo.svg'):
   self.assertRegex(path,pattern)
  for path in ('/network/settings.php','/network/server/devices/search','/network/php/server/app_config.php','/network/api/table_settings.json','/dns/js/api.js'):
   self.assertNotRegex(path,pattern)
  self.assertIn('{ method GET HEAD }',config);self.assertIn('private, max-age=300',config);self.assertIn('{ status 200 }',config)
 def test_read_only_search_does_not_trigger_checkpoint(self):
  for endpoint in ('by-status','search'):
   self.assertFalse(ha.is_mutation('POST','/network/server/devices/'+endpoint,b'{}'))
 def test_streamed_checkpoint_blob_is_byte_identical_to_one_shot_encoding(self):
  chunk=3*65536
  with tempfile.TemporaryDirectory() as td:
   path=pathlib.Path(td)/'db'
   for size in (0,1,2,3,chunk-1,chunk,chunk+1,2*chunk+5,300001):
    data=(b'SQLite format 3\x00'+bytes(range(256))*(size//256+1))[:size]
    path.write_bytes(data)
    for conf,workflows in ((b"SCAN='x'\n",None),(b'',b'[]'),(b'\xff\x00conf',b'[{"id":1}]')):
     payload={'database':base64.b64encode(data).decode(),'configuration':base64.b64encode(conf).decode()}
     if workflows is not None:payload['workflows']=base64.b64encode(workflows).decode()
     expected=gzip.compress(ha.encoded(payload),compresslevel=1,mtime=0)
     self.assertEqual(ha.checkpoint_blob(path,conf,workflows),expected,(size,workflows))
 def test_checkpoint_streams_database_instead_of_holding_copies(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);data,db=self.fixture(root)
   db.execute('CREATE TABLE bulk(data BLOB)');db.execute('INSERT INTO bulk VALUES (zeroblob(12000000))');db.commit()
   tracemalloc.start()
   try:
    blob=ha.checkpoint(data,root/'ram');_,full=tracemalloc.get_traced_memory();tracemalloc.reset_peak()
    ha.checkpoint_blob(root/'data/db/app.db',b'c',None);_,encode=tracemalloc.get_traced_memory()
   finally:tracemalloc.stop();db.close()
   # Encoding no longer holds raw/base64/JSON copies of the 12 MB database (the previous encoder plus
   # self-check peaked at about 73 MiB here; the self-check alone still decompresses the whole payload).
   self.assertLess(encode,4*1024*1024)
   self.assertLess(full,56*1024*1024)
   self.assertTrue(ha.unpack(blob)[0].startswith(b'SQLite format 3\x00'))
 def test_unpack_validation_order_and_errors_are_unchanged(self):
  def pack(v):return gzip.compress(ha.encoded(v),compresslevel=1,mtime=0)
  good={'database':base64.b64encode(b'SQLite format 3\x00x').decode(),'configuration':base64.b64encode(b'c').decode()}
  self.assertEqual(ha.unpack(pack(good)),(b'SQLite format 3\x00x',b'c',None))
  for bad in ({'database':good['database']},dict(good,extra='x'),dict(good,database=base64.b64encode(b'nope').decode()),dict(good,workflows=base64.b64encode(b'{}').decode())):
   with self.assertRaises(ha.Unavailable):ha.unpack(pack(bad))
  with self.assertRaises(Exception):ha.unpack(pack(dict(good,database='***')))
 def test_scratch_dir_removed_after_success_and_after_worker_death(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);data,db=self.fixture(root);run=root/'ram'
   blob=ha.snapshot_worker('create',data=data,run=run)
   self.assertTrue(blob);self.assertEqual(list(run.iterdir()),[])
   def killed(command,blob=None):
    d=pathlib.Path(command[command.index('--run')+1]);(d/'sqlite-dead.db').write_bytes(b'x'*1024)
    raise subprocess.CalledProcessError(-9,command)
   with patch.object(ha.checkpoint_budget,'execute',side_effect=killed):
    with self.assertRaises(subprocess.CalledProcessError):ha.snapshot_worker('create',data=data,run=run)
   self.assertEqual(list(run.iterdir()),[]);db.close()
 def test_snapshot_rejects_stale_term_before_starting_worker(self):
  c=ha.Cluster.__new__(ha.Cluster);c.snaplock=threading.Lock();c.oplock=threading.RLock();c.term=[2,'a'];c.owned=lambda:True
  with patch.object(ha,'snapshot_worker') as worker:
   with self.assertRaises(ha.Unavailable):c.snapshot(expected_term=[1,'a'])
  worker.assert_not_called()
 def test_snapshot_rejects_term_change_and_lease_loss_during_worker(self):
  for change in ('term','lease'):
   c=ha.Cluster.__new__(ha.Cluster);c.snaplock=threading.Lock();c.oplock=threading.RLock();c.term=[1,'a'];c.owned=lambda:True
   c.root=pathlib.Path('/unused');c.run=c.root;c.store=MagicMock();c.fanout=MagicMock();c.last_snapshot=10.
   def changed(*args,**kwargs):
    if change=='term':c.term=[2,'a']
    else:c.owned=lambda:False
    return b'checkpoint'
   with patch.object(ha,'snapshot_worker',side_effect=changed):
    with self.assertRaises(ha.Unavailable):c.snapshot(expected_term=[1,'a'])
   c.fanout.assert_not_called();c.store.commit.assert_not_called();self.assertEqual(c.last_snapshot,10.)
class ClusterFixture:
 def cluster(self,local,heads=None,snapshot=None):
  c=ha.Cluster.__new__(ha.Cluster)
  ticks=iter([False,True])
  c.run=pathlib.Path(self.tmp);c.node='a';c.warm=False;c.local=local;c.term=[1,'a'];c.cooldown=0.;c._rebalance_retry=0.;c._rebalance=None;c._snap_backoff=0.
  c.last_snapshot=1000.;c.sync_state='synced';c.root=pathlib.Path(self.tmp);c.snaplock=threading.Lock();c.ready=True;c.last_health=1000.;c.lease=MagicMock();c.public=lambda:{}
  c.stop=MagicMock(is_set=lambda:next(ticks),wait=lambda t=None:None)
  c.refresh_local=lambda:None;c.fanout=MagicMock(return_value=heads or {});c.owned=lambda:True;c.fence=lambda:None
  c.kill_container=MagicMock();c.snapshot=MagicMock(side_effect=snapshot)
  return c
 def setUp(self):
  td=tempfile.TemporaryDirectory();self.tmp=td.name;self.addCleanup(td.cleanup)
  clock=patch.object(ha,'clock',return_value=1000.);clock.start();self.addCleanup(clock.stop)
 PEERS={'a':{},'b':{'eligible':True,'score':30}}
 TWO={'vip':{'xiaozhi':True,'dns':True},'homeassistant':{'state':'standby'}}
 def released(self,c):return any(not x.kwargs for x in c.kill_container.call_args_list)
class Rebalance(ClusterFixture,unittest.TestCase):
 def test_ha_counts_while_starting_active_or_checkpointing_only(self):
  for state,held in (('starting',True),('active',True),('checkpointing',True),('standby',False),(None,False)):
   self.assertEqual(ha.ha_held({'homeassistant':{'state':state}}),held)
  self.assertFalse(ha.ha_held({}))
 def test_score_penalises_node_holding_two_other_owners(self):
  c=self.cluster({'cpu':10,'vip':{'xiaozhi':True},'homeassistant':{'state':'checkpointing'}})
  self.assertEqual(c.score(),10+20+20+ha.OVERLOAD_PENALTY)
  c=self.cluster({'cpu':10,'vip':{'xiaozhi':True}})
  self.assertEqual(c.score(),10+20)
 def rebalance(self,c,rounds=2):
  ticks=iter([False]*rounds+[True]);c.stop=MagicMock(is_set=lambda:next(ticks),wait=lambda t=None:None)
  with patch.object(ha.Cluster,'renew') as renew,patch.object(ha.Cluster,'native_health',return_value=True):c.run_loop()
  return renew
 def finish(self,c):
  if c._rebalance:
   self.assertTrue(c._rebalance['done'].wait(2),'checkpoint worker did not finish')
 def test_rebalance_snapshots_releases_and_cools_down(self):
  c=self.cluster(self.TWO,self.PEERS)
  with self.assertLogs(ha.LOG,'INFO'):self.rebalance(c,1)
  self.finish(c);self.assertFalse(self.released(c))
  self.rebalance(c,1)
  c.snapshot.assert_called_once();self.assertTrue(self.released(c))
  self.assertIsNone(c.term);self.assertEqual(c.cooldown,1060.)
 def test_lease_keeps_renewing_while_rebalance_checkpoint_is_slow(self):
  gate=threading.Event();started=threading.Event();c=self.cluster(self.TWO,self.PEERS)
  def slow(**kwargs):started.set();gate.wait(5)
  c.snapshot=MagicMock(side_effect=slow)
  self.addCleanup(gate.set)
  c.last_snapshot=0.;c.try_snapshot=MagicMock()
  renew=self.rebalance(c,3)
  self.assertTrue(started.wait(2),'checkpoint did not start')
  self.assertEqual(renew.call_count,3,'loop must reach lease renewal every round, not block on the checkpoint')
  c.try_snapshot.assert_not_called()
  c.snapshot.assert_called_once();self.assertFalse(self.released(c));self.assertEqual(c.term,[1,'a'])
  gate.set();self.finish(c);self.rebalance(c,1);self.assertTrue(self.released(c))
 def test_slow_checkpoint_keeps_lease_alive_beyond_original_deadline(self):
  c=self.cluster(self.TWO,self.PEERS);gate=threading.Event();started=threading.Event();now=[1000.]
  c.boot_ready_seconds=1.;c.container_started=900.
  c.running=True;c.until=1010.;c.oplock=threading.RLock();c.owned=lambda:c.running and now[0]<c.until
  def slow(**kwargs):started.set();gate.wait(5)
  c.snapshot=MagicMock(side_effect=slow);self.addCleanup(gate.set)
  ticks=iter([False]*4+[True]);deadlines=[]
  def tick(delay):
   self.assertTrue(started.wait(2),'checkpoint did not start')
   now[0]+=4;deadlines.append(c.until)
   self.assertTrue(c.owned(),'checkpoint blocked renewal until the fencing deadline')
  c.stop=MagicMock(is_set=lambda:next(ticks),wait=tick)
  try:
   with patch.object(ha,'clock',side_effect=lambda:now[0]),patch.object(ha.Cluster,'native_health',return_value=True):c.run_loop()
   self.assertGreater(now[0],1010.);self.assertEqual(deadlines,[1010.,1014.,1018.,1022.])
   self.assertFalse(self.released(c));c.snapshot.assert_called_once()
  finally:gate.set();self.finish(c)
 def test_rebalance_keeps_lease_when_checkpoint_fails(self):
  c=self.cluster(self.TWO,self.PEERS,snapshot=ha.Unavailable('no quorum'))
  with self.assertLogs(ha.LOG,'INFO'):self.rebalance(c,1)
  self.finish(c)
  with self.assertLogs(ha.LOG,'WARNING'):self.rebalance(c,1)
  self.assertFalse(self.released(c));self.assertEqual(c.term,[1,'a'])
  self.assertEqual(c._rebalance_retry,1060.);self.assertEqual(c.cooldown,0.)
 def test_rebalance_does_not_hand_over_when_term_changed_during_checkpoint(self):
  c=self.cluster(self.TWO,self.PEERS)
  with self.assertLogs(ha.LOG,'INFO'):self.rebalance(c,1)
  self.finish(c);c.term=[2,'a']
  with self.assertLogs(ha.LOG,'WARNING'):self.rebalance(c,1)
  self.assertFalse(self.released(c));self.assertEqual(c._rebalance_retry,1060.)
 def test_rebalance_not_retried_before_deferral_expires(self):
  c=self.cluster(self.TWO,self.PEERS);c._rebalance_retry=1030.
  with patch.object(ha.Cluster,'renew'),patch.object(ha.Cluster,'native_health',return_value=True),self.assertNoLogs(ha.LOG,'WARNING'):c.run_loop()
  c.snapshot.assert_not_called();self.assertFalse(self.released(c))
 def test_no_rebalance_without_second_owner_or_with_loaded_peer(self):
  for local,peers in (({'vip':{'xiaozhi':True}},self.PEERS),(self.TWO,{'a':{},'b':{'eligible':True,'score':ha.OVERLOAD_PENALTY}}),(self.TWO,{'a':{},'b':{'eligible':False,'score':1}})):
   c=self.cluster(local,peers)
   with patch.object(ha.Cluster,'renew'),patch.object(ha.Cluster,'native_health',return_value=True),self.assertNoLogs(ha.LOG,'WARNING'):
    c.run_loop()
   self.assertIn('lease',[x.args[0] for x in c.fanout.call_args_list],'loop never reached the normal renewal path')
   c.snapshot.assert_not_called();self.assertFalse(self.released(c))
 def test_forced_drain_still_releases_when_checkpoint_fails(self):
  c=self.cluster({'admission':{'worker':True}},self.PEERS,snapshot=ha.Unavailable('no quorum'))
  c.run_loop()
  self.assertTrue(self.released(c));self.assertIsNone(c.term)
class RpcCapacity(unittest.TestCase):
 def test_checkpoint_executor_cannot_queue_ahead_of_lease_renewal(self):
  import concurrent.futures
  c=ha.Cluster.__new__(ha.Cluster);c.members={'a':'a','b':'b','c':'c'}
  c.pool=concurrent.futures.ThreadPoolExecutor(max_workers=1);c.checkpoint_pool=concurrent.futures.ThreadPoolExecutor(max_workers=3)
  release=threading.Event();started=[threading.Event() for _ in range(3)]
  def call(node,action,value,timeout):
   if action=='stage':started[list(c.members).index(node)].set();release.wait(3)
   return {'ok':True}
  c.call=call
  with concurrent.futures.ThreadPoolExecutor(max_workers=1) as runner:
   job=runner.submit(c.fanout,'stage',{},2)
   try:
    self.assertTrue(all(event.wait(1) for event in started))
    self.assertEqual(len(c.fanout('lease',{},.2)),3)
   finally:
    release.set();job.result(2);c.pool.shutdown();c.checkpoint_pool.shutdown()
class SnapshotBackoff(ClusterFixture,unittest.TestCase):
 def test_worker_failure_backs_off_exponentially_to_cap_and_resets(self):
  c=self.cluster({},snapshot=subprocess.CalledProcessError(137,'worker'))
  waits=[]
  for _ in range(6):
   with self.assertLogs(ha.LOG,'WARNING'):c.try_snapshot()
   waits.append(c.last_snapshot-(1000.-300))
  self.assertEqual(waits,[30.,60.,120.,240.,300.,300.])
  self.assertEqual(c.sync_state,'pending')
  c.snapshot=MagicMock(side_effect=None);c.try_snapshot()
  self.assertEqual(c._snap_backoff,0.)
 def test_non_worker_failure_keeps_fixed_retry(self):
  c=self.cluster({},snapshot=ha.Unavailable('needs two replicas'))
  with self.assertLogs(ha.LOG,'WARNING'):c.try_snapshot()
  self.assertEqual(c.last_snapshot,1000.-270);self.assertEqual(c._snap_backoff,0.)
if __name__=='__main__':unittest.main()
