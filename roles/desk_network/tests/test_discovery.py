import sys,pathlib,tempfile,sqlite3,time,json,unittest,datetime,threading,http.client,http.server,contextlib
from unittest.mock import patch,Mock
FILES=pathlib.Path(__file__).resolve().parents[1]/'files';sys.path.insert(0,str(FILES))
import discovery,ha
NOW=2000000000.;NODE='deskb1x';TERM=[8,NODE]
class Discovery(unittest.TestCase):
 def fixture(self,root,ago=20):
  db=root/'app.db'
  with contextlib.closing(sqlite3.connect(db)) as c:
   c.execute('CREATE TABLE Online_History(scanDate TEXT,onlineDevices INT,allDevices INT)');c.execute('INSERT INTO Online_History VALUES(?,?,?)',(datetime.datetime.fromtimestamp(NOW-ago,datetime.timezone.utc).isoformat(),12,20));c.commit()
  return db
 def test_freshness_survives_new_owner_from_native_database_only(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);db=self.fixture(root)
   v=discovery.status(db,None,root/'missing',NODE,TERM,1,now=NOW)
   self.assertEqual(v['inventory']['state'],'fresh');self.assertEqual(v['inventory']['age_seconds'],20);self.assertEqual(v['scans']['arp']['phase'],'idle')
   v=discovery.status(db,None,root/'missing','deskb2x',[9,'deskb2x'],1,now=NOW+300)
   self.assertEqual(v['inventory']['state'],'stale');self.assertEqual(v['inventory']['imported_at'],NOW-20)
 def test_unknown_is_not_fresh_and_missing_database_is_never_created(self):
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td)/'none';self.assertEqual(discovery.inventory(p,1,NOW)['state'],'unknown');self.assertFalse(p.exists())
 def test_importing_only_completes_after_native_history(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);db=self.fixture(root);(root/'desk-arp-status.json').write_text(json.dumps({'state':'ready','started_at':NOW-10,'finished_at':NOW-5,'devices':12,'worker':'deskb3x','wall_seconds':4.2}))
   value=discovery.status(db,root,root/'none',NODE,TERM,1,now=NOW)
   self.assertEqual(value['scans']['arp']['phase'],'importing')
   with contextlib.closing(sqlite3.connect(db)) as c:c.execute('UPDATE Online_History SET scanDate=?',(datetime.datetime.fromtimestamp(NOW-1,datetime.timezone.utc).isoformat(),));c.commit()
   value=discovery.status(db,root,root/'none',NODE,TERM,1,now=NOW)
   self.assertEqual(value['scans']['arp']['phase'],'complete');self.assertEqual(value['scans']['arp']['worker'],'deskb3x')
 def test_old_term_and_stuck_jobs_do_not_show_running_forever(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);db=self.fixture(root);p=root/'dispatch'
   p.write_text(json.dumps({'owner':NODE,'term':[7,NODE],'scans':{'arp':{'phase':'running','at':NOW}}}))
   self.assertEqual(discovery.status(db,None,p,NODE,TERM,1,now=NOW)['scans']['arp']['phase'],'idle')
   p.write_text(json.dumps({'owner':NODE,'term':TERM,'scans':{'arp':{'phase':'running','at':NOW-60,'started_at':NOW-60}}}))
   v=discovery.status(db,None,p,NODE,TERM,1,now=NOW)['scans']['arp'];self.assertEqual(v['phase'],'failed')
 def test_queued_scan_ignores_earlier_result_and_reports_timeout(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);db=self.fixture(root);(root/'desk-arp-status.json').write_text(json.dumps({'state':'ready','started_at':NOW-60,'finished_at':NOW-55}))
   v=discovery.status(db,root,root/'none',NODE,TERM,1,queued_at=NOW-3,now=NOW)
   self.assertEqual(v['scans']['arp']['phase'],'queued')
   v=discovery.status(db,None,root/'none',NODE,TERM,1,queued_at=NOW-130,now=NOW)
   self.assertEqual(v['scans']['arp']['phase'],'failed')
 def test_cached_names_and_unavailable_scans_preserve_history(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);db=self.fixture(root)
   (root/'desk-arp-status.json').write_text(json.dumps({'state':'unavailable','started_at':NOW-5,'finished_at':NOW-1,'reason':'no_eligible_workers'}))
   (root/'desk-mdns-status.json').write_text(json.dumps({'state':'ready','started_at':NOW-5,'finished_at':NOW-1,'queried':0,'resolved':3}))
   v=discovery.status(db,root,root/'none',NODE,TERM,1,now=NOW)
   self.assertEqual(v['scans']['arp']['phase'],'failed');self.assertIn('eligible',v['scans']['arp']['message']);self.assertEqual(v['scans']['mdns']['phase'],'cached');self.assertEqual(v['inventory']['age_seconds'],20)
 def test_bounded_output_rejects_untrusted_labels_and_nonfinite_fields(self):
  v=discovery.observation({'worker':'<script>','reason':'password=secret','at':float('nan'),'devices':-1,'wall_seconds':float('inf')},NOW)
  self.assertEqual(v,{})
 def test_native_changes_are_read_only_and_mac_based(self):
  with tempfile.TemporaryDirectory() as td:
   db=pathlib.Path(td)/'app.db'
   with contextlib.closing(sqlite3.connect(db)) as c:c.execute('CREATE TABLE Devices(devMac TEXT,devLastIP TEXT)');c.execute('INSERT INTO Devices VALUES(?,?)',('02:00:00:00:00:01','192.168.1.10'));c.commit()
   rows={'192.168.1.20':['02:00:00:00:00:01',''],'192.168.1.30':['02:00:00:00:00:02','']}
   self.assertEqual(discovery.changes(db,rows),{'new_devices':1,'ip_changes':1})
class ScanAction(unittest.TestCase):
 def test_double_click_queues_only_once_and_cross_origin_is_rejected(self):
  class Response:
   status=200
   def __enter__(self):return self
   def __exit__(self,*a):pass
  class Cluster:
   members={NODE:'127.0.0.1'};c={'api_token':'fake'};ready=True;term=TERM;scan_queued_at=0.;scan_cache_at=0.;scan_lock=threading.Lock();until=0
   opener=Mock();opener.open.return_value=Response()
   def owned(self):return True
   def renew(self,*args):return True
   def fanout(self,*a):return {'a':True,'b':True}
   def scan_status(self):return {'scans':{'arp':{'phase':'queued' if self.scan_queued_at else 'idle'}}}
  cluster=Cluster();server=http.server.ThreadingHTTPServer(('127.0.0.1',0),ha.UI);server.cluster=cluster
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   def request(origin='http://192.168.1.186'):
    with contextlib.closing(http.client.HTTPConnection(*server.server_address,timeout=2)) as c:
     c.request('POST','/network/desk/scan',b'{}',{'X-Desk-Action':'scan','Origin':origin});r=c.getresponse();r.read();return r.status
   self.assertEqual(request(),202);self.assertEqual(request(),202);self.assertEqual(cluster.opener.open.call_count,1);self.assertEqual(request('https://elsewhere.example'),403)
  finally:server.shutdown();server.server_close();thread.join(2)
