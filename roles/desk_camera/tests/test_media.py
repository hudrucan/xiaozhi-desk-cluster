"""Focused offline checks for standby isolation, deadlines and ACL cleanup."""
import importlib.util,json,math,pathlib,sys,tempfile,types,unittest
ROOT=pathlib.Path(__file__).parent
if not (ROOT/'runtime.py').exists():ROOT=ROOT.parent/'files'
sys.path.insert(0,str(ROOT))
sys.modules.setdefault('lease',types.SimpleNamespace(clock=lambda:100))
sys.modules.setdefault('config_journal',types.SimpleNamespace(atomic=lambda *a:None,encoded=lambda v:b''))
def module(name):
 spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
sys.modules.setdefault('nats',types.SimpleNamespace())
sys.modules.setdefault('protocol',types.SimpleNamespace(pack=None,unpack=None,MAX=65536))
sys.modules['lease'].Lease=object
r=module('runtime');a=module('install_nats');c=module('controller')
class MediaTests(unittest.TestCase):
 def test_unowned_readiness_does_not_read_runtime_files(self):
  agent=c.Controller.__new__(c.Controller);agent.term=None
  self.assertFalse(agent.ready())
 def test_deadline(self):
  for until in (99,100,112,math.nan,math.inf,True,'110'):
   self.assertIsNone(r.generation({'until':until,'generation':'a'*32},100))
  self.assertEqual(r.generation({'until':110,'generation':'a'*32},100),'a'*32)
  self.assertIsNone(r.generation({'until':110,'generation':'x'*32},100))
 def test_warm_has_no_camera_sources(self):
  c={'node':'n','members':{'n':'10.0.0.1'},'lan_ip':'192.0.2.1','relay':{'user':'u','password':'p'},'streams':{'bed_hd':'rtsp://private'}}
  self.assertEqual(r.render(c,None)['streams'],{})
  self.assertEqual(r.render(c,'a'*32)['streams'],c['streams'])
  self.assertTrue(r.render(c,None)['api']['local_auth'])
 def test_camera_ports_and_empty_standbys_are_independent(self):
  base={'node':'n','members':{'n':'10.0.0.1'},'lan_ip':'192.0.2.1','relay':{'user':'u','password':'p'},'streams':{'a_hd':'rtsp://private'}}
  camera=dict(base,api_port=11984,rtsp_port=18654,webrtc_port=18655)
  self.assertEqual(r.render(camera,None)['streams'],{})
  self.assertEqual(r.render(camera,'a'*32)['api']['listen'],'10.0.0.1:11984')
  self.assertEqual(r.render(camera,'a'*32)['rtsp']['listen'],'10.0.0.1:18654')
  self.assertEqual(r.render(camera,'a'*32)['webrtc']['candidates'],['192.0.2.1:18655'])
  self.assertEqual(r.render(base,'b'*32)['api']['listen'],'10.0.0.1:1984')
 def test_placement_penalizes_other_live_camera_slots_only(self):
  with tempfile.TemporaryDirectory() as tmp:
   c.RUN=pathlib.Path(tmp);own=c.RUN/'tapo_c222_161';own.mkdir();other=c.RUN/'tapo_c222_163';other.mkdir()
   agent=c.Controller.__new__(c.Controller);agent.run_dir=own;agent.local={'cpu':12}
   (own/'permit.json').write_text(json.dumps({'until':110,'generation':'a'*32}))
   (other/'permit.json').write_text(json.dumps({'until':110,'generation':'b'*32}))
   (c.RUN/'permit.json').write_text(json.dumps({'until':99,'generation':'c'*32}))
   self.assertEqual(agent.placement_score(),112)
   (other/'permit.json').write_text(json.dumps({'until':99,'generation':'b'*32}))
   self.assertEqual(agent.placement_score(),12)
 def test_camera_runtime_paths_are_separate_and_legacy_path_is_preserved(self):
  self.assertEqual(c.paths({'workload':'tapo_c222_160'}),(c.ROOT,c.RUN))
  self.assertEqual(c.paths({'workload':'tapo_c222_161'}),(c.ROOT/'tapo_c222_161',c.RUN/'tapo_c222_161'))
  with self.assertRaises(ValueError):c.paths({'workload':'../camera'})
 def test_retire_only_owned_acl(self):
  old='authorization {\n  users: [\n'+ '    # BEGIN DESK REMOTE INTEGRATION USERS\nold\n    # END DESK REMOTE INTEGRATION USERS\n'+ '    {user: existing}\n  ]\n}\nroutes: [unchanged]\n'
  users={n:{'user':n,'password':'private'} for n in ('deskb1x','deskb2x','deskb3x')}
  new=a.candidate(old,users)
  self.assertNotIn('REMOTE INTEGRATION',new);self.assertIn('{user: existing}',new);self.assertIn('routes: [unchanged]',new)
  self.assertNotIn('desk.ha.remote',new);self.assertEqual(a.candidate(new,users),new)
if __name__=='__main__':unittest.main()
