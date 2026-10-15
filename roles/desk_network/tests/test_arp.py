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
  class Objects:
   def __init__(self,path):self.path=pathlib.Path(path);self.rows=[]
   def add_object(self,**kw):self.rows.append(kw)
   def write_result_file(self):self.path.write_text('\n'.join(json.dumps(row) for row in self.rows))
  with tempfile.TemporaryDirectory() as td,patch.dict('os.environ',{'DESK_NETWORK_DISTRIBUTED':'1'}):
   root=pathlib.Path(td);(root/'plugins').mkdir();result=root/'plugins/last_result.ARPSCAN.log';result.write_text('stale|success')
   with patch.dict(sys.modules,{'plugin_helper':types.SimpleNamespace(Plugin_Objects=Objects),'const':types.SimpleNamespace(logPath=td)}),patch('mdns.save_json'),patch('arp.lookup',side_effect=OSError('worker down')):
    self.assertEqual(arp.main(),1);self.assertEqual(result.read_text(),'')
   with patch.dict(sys.modules,{'plugin_helper':types.SimpleNamespace(Plugin_Objects=Objects),'const':types.SimpleNamespace(logPath=td)}),patch('mdns.save_json'),patch('arp.lookup',return_value=(rows(),{})):
    self.assertEqual(arp.main(),0)
   objects=[json.loads(line) for line in result.read_text().splitlines()]
   self.assertEqual(len(objects),2);self.assertTrue(all(v['extra']=='ARPSCAN' and v['watched3']=='192.168.1.0/24 --interface=wlan0' for v in objects))
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
