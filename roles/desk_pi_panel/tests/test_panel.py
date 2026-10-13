import copy
import sys
import ctypes
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).parents[1]/'files'))

spec=importlib.util.spec_from_file_location('panel',Path(__file__).parents[1]/'files/panel.py')
panel=importlib.util.module_from_spec(spec);spec.loader.exec_module(panel)

def fixture():
    return {'protocol':'desk-cluster-panel-v1','state':'ready','age_ms':1000,'observed_at':'2026-10-13T08:00:00Z',
        'owners':{'dns':{'state':'known','node':'deskb2x'},'xiaozhi':{'state':'known','node':'deskb1x'}},
        'dns':{'mode':'adguard'},'events':[],
        'nodes':[{'id':n,'ip':'192.168.1.'+str(101+i),'online':True,'age_ms':1000,'jobs':{'asr':0,'llm_vlm':0,'tts':0},'components':{},'configuration':{},'admission_paused':{},'vip':{},'cpu_percent':10,'memory_percent':30,'temperature_c':42} for i,n in enumerate(panel.MEMBERS)]}

class PanelTests(unittest.TestCase):
    def test_only_expected_panel_membership_is_accepted(self):
        v=fixture();self.assertEqual(panel.validate(v),v)
        v['nodes'].reverse()
        with self.assertRaises(ValueError):panel.validate(v)
    def test_invalid_metadata_never_reaches_renderer(self):
        for change in (lambda v:v['dns'].update(mode=[]),lambda v:v['owners'].update(dns=[]),lambda v:v['nodes'][0]['jobs'].update(tts=99),lambda v:v.update(events=[{'node':'deskb1x','message':'x'*257}])):
            v=fixture();change(v)
            with self.assertRaises(ValueError):panel.validate(v)
    def test_cached_metadata_becomes_unknown_without_new_api_reply(self):
        v=fixture();self.assertTrue(panel.freshness(v,10,15)[0]);self.assertFalse(panel.freshness(v,10,30)[0])
        v['state']='unavailable';self.assertFalse(panel.freshness(v,10,11)[0])
    def test_node_age_can_expire_before_aggregate_age(self):
        v=fixture();n=v['nodes'][0];n['age_ms']=19000
        live,elapsed=panel.freshness(v,10,12)
        self.assertTrue(live);self.assertFalse(panel.node_live(n,live,elapsed))
    def test_touch_axis_mapping_is_configurable_and_clamped(self):
        r=SimpleNamespace(min=100,max=4000)
        self.assertEqual(panel.touch_point(100,4000,r,r,{}),(0,319))
        self.assertEqual(panel.touch_point(100,4000,r,r,{'swap_axes':True,'invert_x':True}),(0,0))
        self.assertEqual(panel.touch_point(-100,5000,r,r,{}),(0,319))
    def test_display_rotation_and_touch_use_the_same_orientation(self):
        r=SimpleNamespace(min=0,max=4095)
        self.assertEqual(panel.touch_point(0,0,r,r,{'rotation':180}),(479,319))
        self.assertEqual(panel.touch_point(4095,4095,r,r,{'rotation':180}),(0,0))
    def test_affine_calibration_maps_raw_points_then_mount_rotation(self):
        r=SimpleNamespace(min=0,max=4095)
        config={'touch_transform':[0,-0.12,480,0.08,0,0]}
        self.assertEqual(panel.touch_point(1000,2000,r,r,config),(240,80))
        config['rotation']=180
        self.assertEqual(panel.touch_point(1000,2000,r,r,config),(239,239))
    def test_stale_owner_is_not_retained_when_only_that_node_expires(self):
        renderer=panel.Renderer.__new__(panel.Renderer);v=fixture();v['nodes'][1]['age_ms']=21000
        self.assertEqual(renderer.owner(v,'dns',True,0),'Unconfirmed')
    def test_framebuffer_layout_rejects_wrong_format(self):
        v=[0]*40;v[0],v[1],v[6]=480,320,16;v[8:10]=11,5;v[11:13]=5,6;v[14:16]=0,5
        panel.validate_framebuffer(tuple(v),960)
        for field,value in ((0,320),(6,32),(4,1),(8,0)):
            bad=v.copy();bad[field]=value
            with self.assertRaises(ValueError):panel.validate_framebuffer(tuple(bad),960)
        with self.assertRaises(ValueError):panel.validate_framebuffer(tuple(v),1920)

if __name__=='__main__':unittest.main()
