import asyncio,importlib,json,pathlib,subprocess,sys,tempfile,time,types,unittest
from unittest.mock import patch,AsyncMock
FILES=pathlib.Path(__file__).resolve().parents[1]/'files'
sys.path.insert(0,str(FILES))
import arp,scan_protocol as p
from scan_service import Service
MAC='02:11:22:33:44:01';OWNER='deskb1x';WORKER='deskb2x'
def job():return dict(id='b'*32,owner=OWNER,term=[7,OWNER],worker=WORKER,plugin='arp',targets={},deadline=time.time()+18)
def rows():return {'192.168.1.1':[MAC,'Router'],'192.168.1.102':['02:11:22:33:44:02','']}
class ARP(unittest.TestCase):
 def setUp(self):
  root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
  state=patch.object(arp,'PRESENCE',pathlib.Path(root.name)/'presence.json');state.start();self.addCleanup(state.stop)
 def test_fixed_subnet_and_signed_results(self):
  j=job();p.validate_job(j,WORKER)
  for changes in ({'targets':{'192.168.2.1':MAC}},{'deadline':time.time()+25},{'plugin':'shell'}):
   with self.assertRaises(ValueError):p.validate_job(dict(j,**changes),WORKER)
  result=dict(id=j['id'],term=j['term'],worker=WORKER,state='ready',found=rows(),stats={'received':2})
  self.assertEqual(p.validate_result(result,j)[0],rows())
  for found in ({},{'192.168.1.8':[MAC,'']},{'192.168.1.1':[MAC,'bad|name']},{'192.168.1.1':[MAC,''],'192.168.1.2':[MAC,'']}):
   with self.assertRaises(ValueError):p.validate_result(dict(result,found=found),j)
 def test_scan_fixed_command_self_identity_and_vip_dedup(self):
  def command(argv,**kw):
   self.assertEqual(argv[0],'/usr/sbin/arp-scan');self.assertEqual(argv[-1],'192.168.1.0/24');self.assertEqual(kw['timeout'],12);self.assertNotIn('shell',kw);self.assertIn('--ouifile=/dev/null',argv);self.assertIn('--macfile=/dev/null',argv)
   kw['stdout'].write(b'192.168.1.1\t02:11:22:33:44:01\tRouter\n192.168.1.254\t02:11:22:33:44:02\tAlias\n')
  with patch('mdns.interface_address',return_value='192.168.1.102'),patch('arp.subprocess.run',side_effect=command),patch('arp.pathlib.Path.read_text',return_value='02:11:22:33:44:02\n'):
   found,stats=arp.scan()
  self.assertEqual(found,{ip:[row[0],''] for ip,row in rows().items()});self.assertEqual(stats['received'],2)
 def test_gateway_absence_timeout_and_command_failure_never_return_success(self):
  def empty(argv,**kw):kw['stdout'].write(b'192.168.1.115\t02:11:22:33:44:05\tPi\n')
  for effect in (empty,subprocess.TimeoutExpired('arp-scan',12),subprocess.CalledProcessError(1,'arp-scan')):
   with patch('mdns.interface_address',return_value='192.168.1.102'),patch('arp.subprocess.run',side_effect=effect),patch('arp.pathlib.Path.read_text',return_value='02:11:22:33:44:02'),self.assertRaises((ValueError,subprocess.SubprocessError)):arp.scan()
 def test_adapter_clears_stale_output_and_never_writes_database(self):
  with tempfile.TemporaryDirectory() as td,patch.dict('os.environ',{'DESK_NETWORK_DISTRIBUTED':'1'}):
   root=pathlib.Path(td);(root/'plugins').mkdir();result=root/'plugins/last_result.ARPSCAN.log';result.write_text('stale|success')
   with patch.dict(sys.modules,{'plugin_helper':None,'const':types.SimpleNamespace(logPath=td)}),patch('mdns.save_json'),patch('arp.lookup',side_effect=OSError('worker down')):
    self.assertEqual(arp.main(),1);self.assertEqual(result.read_text(),'')
   with patch.dict(sys.modules,{'plugin_helper':None,'const':types.SimpleNamespace(logPath=td)}),patch('mdns.save_json') as status,patch('arp.lookup',return_value=(rows(),{})):
    self.assertEqual(arp.main(),0)
    self.assertEqual(status.call_args.args[1]['held'],0)
   objects=[line.split('|') for line in result.read_text().splitlines()]
   self.assertEqual(len(objects),2);self.assertTrue(all(len(v)==13 and v[7]=='ARPSCAN' and v[5]=='192.168.1.0/24 --interface=wlan0' for v in objects))
   with patch.dict(sys.modules,{'plugin_helper':None,'const':types.SimpleNamespace(logPath=td)}),patch('mdns.save_json') as status,patch('arp.lookup',return_value=({'192.168.1.1':[MAC,'Router']},{})):
    self.assertEqual(arp.main(),0)
    self.assertEqual(status.call_args.args[1]['held'],1)
   objects=[line.split('|') for line in result.read_text().splitlines()]
   self.assertEqual(len(objects),2);self.assertEqual([objects[1][i] for i in (0,1,3)],['02:11:22:33:44:02','192.168.1.102','192.168.1.102'])

class Presence(unittest.TestCase):
 def setUp(self):
  root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup);self.path=pathlib.Path(root.name)/'presence.json'
  env=patch.dict('os.environ',{'DESK_NETWORK_ARP_GRACE_SECONDS':'600'});env.start();self.addCleanup(env.stop)
 def apply(self,value,now):return arp.presence(value,self.path,now)
 def test_five_minute_hold_expiry_and_new_mac_immediate(self):
  self.assertEqual(self.apply(rows(),1000)[0],rows())
  router={'192.168.1.1':[MAC,'Router']}
  found,stats=self.apply(router,1300);self.assertEqual(found['192.168.1.102'],rows()['192.168.1.102']);self.assertEqual(stats['held'],1)
  self.assertEqual(self.apply(router,1600)[1]['held'],1)
  found,stats=self.apply(router,1660);self.assertEqual(found,router);self.assertEqual(stats['held'],0)
  self.assertNotIn('02:11:22:33:44:02',json.loads(self.path.read_text())['entries'])
  new=dict(router,**{'192.168.1.103':['02:11:22:33:44:03','']});self.assertEqual(self.apply(new,1661)[0],new)
 def test_real_sighting_refreshes_time_ip_and_current_ip_wins(self):
  self.apply(rows(),1000)
  moved={'192.168.1.1':[MAC,'Router'],'192.168.1.105':rows()['192.168.1.102']}
  self.apply(moved,1500);found,stats=self.apply({'192.168.1.1':[MAC,'Router']},1800)
  self.assertIn('192.168.1.105',found);self.assertNotIn('192.168.1.102',found);self.assertEqual(stats['held'],1)
  replacement={'192.168.1.1':[MAC,'Router'],'192.168.1.105':['02:11:22:33:44:06','']}
  self.assertEqual(self.apply(replacement,1801)[0],replacement)
 def test_corrupt_unreadable_future_and_unsafe_entries_ignored(self):
  for raw in ('broken','[]','{"version":1,"entries":[]}', '['*2000):
   self.path.write_text(raw);self.assertEqual(self.apply(rows(),1000)[0],rows())
  value={'version':1,'entries':{'02:11:22:33:44:03':['192.168.1.103',1001],'02:11:22:33:44:04':['192.168.1.104',float('nan')],'bad':['192.168.1.105',999],'02:11:22:33:44:06':['8.8.8.8',999]}}
  self.path.write_text(json.dumps(value));self.assertEqual(self.apply(rows(),1000)[0],rows())
  with patch.object(pathlib.Path,'open',side_effect=PermissionError):
   found,stats=self.apply(rows(),1000)
  self.assertEqual(found,rows());self.assertEqual(stats['grace_state'],'ready') # Unreadable old state need not prevent replacement.
 def test_size_count_limits_and_atomic_write_failure(self):
  self.path.write_bytes(b' '* (arp.MAX_STATE_BYTES+1));self.assertEqual(self.apply(rows(),1000)[0],rows())
  with patch.object(arp,'MAX_MACS',3):
   self.apply(rows(),1000)
   new={'192.168.1.103':['02:11:22:33:44:03',''],'192.168.1.104':['02:11:22:33:44:04','']}
   found,stats=self.apply(new,1100);self.assertEqual(len(found),3);self.assertEqual(stats['held'],1)
   entries=json.loads(self.path.read_text())['entries'];self.assertEqual(len(entries),3)
   entries['02:11:22:33:44:05']=['192.168.1.105',1100];self.path.write_text(json.dumps({'version':1,'entries':entries}))
   self.assertEqual(self.apply(rows(),1200)[1]['held'],0)
  before=self.path.read_bytes()
  with patch('arp.os.replace',side_effect=OSError('write failed')):
   found,stats=self.apply(rows(),1250)
  self.assertEqual(found,rows());self.assertEqual(stats['grace_state'],'unavailable');self.assertEqual(self.path.read_bytes(),before)
  self.assertEqual(list(self.path.parent.glob('.desk-arp-presence-*')),[])
 def test_configurable_grace_and_zero_disables_hold(self):
  self.apply(rows(),1000)
  with patch.dict('os.environ',{'DESK_NETWORK_ARP_GRACE_SECONDS':'60'}):self.assertEqual(self.apply({},1061)[0],{})
  self.apply(rows(),1100)
  with patch.dict('os.environ',{'DESK_NETWORK_ARP_GRACE_SECONDS':'0'}):self.assertEqual(self.apply({},1101)[0],{})
class Dispatch(unittest.IsolatedAsyncioTestCase):
 async def test_arp_uses_selected_subject_and_rechecks_owner(self):
  s=Service({'node':OWNER,'key':'fake','members':{}});s.owner=AsyncMock(return_value=[7,OWNER]);s.worker_status=AsyncMock(side_effect=[(40,OWNER),(10,WORKER),(20,'deskb3x')])
  async def request(node,j,kind,timeout):
   self.assertEqual(node,WORKER);self.assertEqual(j['plugin'],'arp');self.assertEqual(j['targets'],{});self.assertEqual(timeout,18.1)
   return dict(id=j['id'],term=j['term'],worker=node,state='ready',found=rows(),stats={'received':2})
  s.request=request
  with patch('scan_service.save_json'):result=await s.dispatch({},'arp')
  self.assertEqual(result['stats']['worker'],WORKER);self.assertEqual(s.owner.await_count,3)
 async def test_failed_arp_does_not_return_empty_success(self):
  s=Service({'node':OWNER,'key':'fake','members':{}});s.owner=AsyncMock(return_value=[7,OWNER]);s.worker_status=AsyncMock(side_effect=[(40,OWNER),(10,WORKER),(20,'deskb3x')]);s.request=AsyncMock(return_value={'state':'unavailable'})
  with patch('scan_service.asyncio.sleep',new_callable=AsyncMock),self.assertRaises(p.Unavailable):await s.dispatch({},'arp')
  self.assertEqual(s.request.await_count,2)
