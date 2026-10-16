import importlib.util,pathlib,unittest
p=pathlib.Path(__file__).resolve().parents[2]/'desk_node_control/files/node_control.py'
spec=importlib.util.spec_from_file_location('node_control',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Checks(unittest.TestCase):
 def setUp(self):
  self.c=object.__new__(m.Control);self.c.node='deskb1x'
  self.views={n:{'metadata':{'homeassistant':{'state':'standby','ready':False,'sync':'synced','checkpoint_revision':2}}} for n in ('deskb1x','deskb2x','deskb3x')}
  self.views['deskb2x']['metadata']['homeassistant'].update(state='active',ready=True)
 def test_handoff_complete_and_matching_survivors_allows_drain(self):self.assertTrue(self.c.homeassistant_drained(self.views))
 def test_unflushed_core_or_stale_survivor_blocks_reboot(self):
  self.views['deskb1x']['metadata']['homeassistant']['state']='checkpointing';self.assertFalse(self.c.homeassistant_drained(self.views))
  self.views['deskb1x']['metadata']['homeassistant']['state']='standby';self.views['deskb3x']['metadata']['homeassistant']['checkpoint_revision']=1;self.assertFalse(self.c.homeassistant_drained(self.views))
 def test_missing_core_metadata_and_no_ready_peer_block_reboot(self):
  self.views['deskb2x']['metadata']['homeassistant']['ready']=False;self.assertFalse(self.c.homeassistant_drained(self.views))
  self.views['deskb2x']['metadata']['homeassistant']['ready']=True;self.views['deskb3x']['metadata']['homeassistant']=None;self.assertFalse(self.c.homeassistant_drained(self.views))
if __name__=='__main__':unittest.main()
