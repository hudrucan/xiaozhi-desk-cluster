"""Standby timing, immediate shutdown and unchanged majority lease semantics."""
import asyncio,json,pathlib,subprocess,sys,tempfile,types,unittest
import jinja2,yaml
from unittest.mock import AsyncMock,Mock,patch
BASE=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(BASE/'desk_camera/files'));sys.path.insert(0,str(BASE/'desk_homeassistant/files'))
sys.modules.setdefault('nats',types.ModuleType('nats'))
import controller
from lease import Unavailable

class Cadence(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.agent=controller.Controller.__new__(controller.Controller)
  a=self.agent;a.c={};a.node='n1';a.fleet=None;a.term=None;a.until=0;a.generation=None;a.heads={};a.stop=asyncio.Event();a.run_dir=pathlib.Path(self.tmp.name)
  a.playback=types.SimpleNamespace(active=lambda:False)
  self.now=[100.];self.timer=patch.object(controller,'clock',lambda:self.now[0]);self.timer.start();self.addCleanup(self.timer.stop)
  a.lease=controller.Lease(a.run_dir/'vote.json',clock=lambda:self.now[0]);self.now[0]=120.
 def test_node_with_any_owner_or_local_lease_keeps_all_loops_fast(self):
  a=self.agent;self.assertEqual(a.reconcile_interval(),5)
  owner=Mock(node='n2',term=[1,'n2']);owner.owned.return_value=True
  a.fleet=types.SimpleNamespace(agents=[a,owner]);self.assertEqual(a.reconcile_interval(),2)
  a.fleet=None;a.term=[1,a.node];a.until=130;self.assertEqual(a.reconcile_interval(),2)
  a.term=None;a.lease.grant([2,a.node],a.node);self.assertEqual(a.reconcile_interval(),2)
 def test_custom_interval_and_deadline_wake(self):
  a=self.agent
  for value,expected in [(3,3),(2,2),(5,5),(0,5),(6,5),('bad',5),(float('nan'),5),(float('inf'),5)]:
   a.c['standby_interval_seconds']=value;self.assertEqual(a.reconcile_interval(),expected)
  a.lease.quarantine=121.25;self.assertEqual(a.reconcile_interval(),1.25)
  a.lease.quarantine=100;a.heads={'peer':{'lease':{'remaining':.5}}};self.assertEqual(a.reconcile_interval(),.5)
 def test_active_playback_stays_fast(self):
  self.agent.playback.active=lambda:True;self.assertEqual(self.agent.reconcile_interval(),2)
 def test_role_sets_configured_interval_preserves_other_fields_and_is_idempotent(self):
  tasks=yaml.safe_load((BASE/'desk_camera/tasks/main.yml').read_text())
  task=next(t for t in tasks if t.get('name','').startswith('Set progressive playback and standby'))
  defaults=yaml.safe_load((BASE/'desk_camera/defaults/main.yml').read_text())
  self.assertEqual(defaults['desk_camera_standby_interval_seconds'],5)
  env=jinja2.Environment();env.filters.update(bool=bool,to_json=json.dumps,ternary=lambda value,yes,no:yes if value else no)
  path=self.agent.run_dir/'config.json';original={'key':'kept','cameras':[{'workload':'cam','progressive':False,'streams':{'kept':'private'}}]};path.write_text(json.dumps(original))
  code=env.from_string(task['ansible.builtin.command']['argv'][2]).render(desk_camera_progressive=True,desk_camera_standby_interval_seconds=3).replace('/etc/desk-camera/config.json',str(path))
  first=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=True)
  self.assertEqual(first.stdout.strip(),'changed');updated=json.loads(path.read_bytes());self.assertEqual(updated['key'],'kept');self.assertEqual(updated['cameras'][0]['streams'],original['cameras'][0]['streams']);self.assertEqual(updated['cameras'][0]['standby_interval_seconds'],3)
  inode=path.stat().st_ino;second=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=True);self.assertEqual(second.stdout.strip(),'unchanged');self.assertEqual(path.stat().st_ino,inode)
 async def test_stop_interrupts_standby_and_initial_delay_and_revokes_permit(self):
  for delay in (0,8):
   a=self.agent;a.stop=asyncio.Event();a.reconcile=AsyncMock();sleeping=asyncio.Event();original=a.wait
   async def wait(seconds):
    if seconds>=5:sleeping.set()
    await original(seconds)
   a.wait=wait;task=asyncio.create_task(a.run(delay))
   await asyncio.wait_for(sleeping.wait(),.5);a.stop.set();await asyncio.wait_for(task,.2)
   self.assertEqual(a.reconcile.await_count,0 if delay else 1)
   self.assertEqual(json.loads((a.run_dir/'permit.json').read_bytes()),{'until':0,'generation':None})
   a.wait=original
 def test_identical_standby_permit_keeps_inode_but_repairs_deleted_or_changed_file(self):
  a=self.agent
  with patch.object(controller,'atomic',wraps=controller.atomic) as write:
   a.permit();inode=(a.run_dir/'permit.json').stat().st_ino;a.permit();a.permit()
   self.assertEqual(write.call_count,1);self.assertEqual((a.run_dir/'permit.json').stat().st_ino,inode)
   (a.run_dir/'permit.json').write_text('{"until":999,"generation":"stale"}');a.permit();self.assertEqual(write.call_count,2)
   (a.run_dir/'permit.json').unlink();a.permit();self.assertEqual(write.call_count,3)
   a.term=[1,a.node];a.until=130;a.generation='fresh';a.permit();self.assertEqual(write.call_count,4)
   a.until=132;a.permit();self.assertEqual(write.call_count,5)
   a.until=0;a.permit();self.assertEqual(write.call_count,6);self.assertIsNone(json.loads((a.run_dir/'permit.json').read_bytes())['generation'])
 async def test_majority_and_quarantine_still_gate_takeover_within_twenty_seconds(self):
  a=self.agent;a.c={'definition':'same'};a.started_at=0;a.cooldown=0;a.refresh=Mock();a.eligible=lambda:True;a.public=lambda:{}
  # A restarted voter cannot grant before its original 17-second quarantine.
  self.now[0]=100.;a.lease=controller.Lease(a.run_dir/'vote.json',clock=lambda:self.now[0])
  async def fanout(action,value=None):
   if action=='status':return {n:{'owned':False,'lease':a.lease.status(),'eligible':True,'score':i,'definition':'same'} for i,n in enumerate(['n1','n2','n3'])}
   try:return {n:a.lease.grant(value['term'],a.node) for n in ['n1','n2']}
   except Unavailable:return {}
  a.fanout=fanout
  await a.reconcile();self.assertFalse(a.owned());self.assertEqual(a.reconcile_interval(),5)
  self.now[0]=115.;self.assertEqual(a.reconcile_interval(),2)
  self.now[0]=117.;await a.reconcile();self.assertTrue(a.owned());self.assertEqual(a.until,127.)
  self.assertEqual(a.lease.status()['remaining'],15.)
  # A single acknowledgement cannot acquire ownership after expiry.
  a.until=0;a.term=None
  async def minority(action,value=None):
   if action=='status':return {n:{'owned':False,'lease':{'holder':None,'promised':[1,'n1']},'eligible':True,'score':i,'definition':'same'} for i,n in enumerate(['n1','n2'])}
   return {'n1':{}}
  a.fanout=minority;await a.reconcile();self.assertFalse(a.owned())

if __name__=='__main__':unittest.main()
