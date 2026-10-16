import copy
import sys
import ctypes
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
import json
import threading
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).parents[1]/'files'))

spec=importlib.util.spec_from_file_location('panel',Path(__file__).parents[1]/'files/panel.py')
panel=importlib.util.module_from_spec(spec);spec.loader.exec_module(panel)

def fixture():
    return {'protocol':'desk-cluster-panel-v1','state':'ready','age_ms':1000,'observed_at':'2026-10-13T08:00:00Z',
        'owners':{'dns':{'state':'known','node':'deskb2x'},'xiaozhi':{'state':'known','node':'deskb1x'}},
        'dns':{'mode':'adguard'},'events':[],
        'nodes':[{'id':n,'ip':'192.168.1.'+str(101+i),'online':True,'age_ms':1000,'jobs':{'asr':0,'llm_vlm':0,'tts':0},'components':{},'configuration':{},'admission_paused':{},'vip':{},'cpu_percent':10,'memory_percent':30,'temperature_c':42} for i,n in enumerate(panel.MEMBERS)]}

def network_fixture():
    v=fixture();v['owners']['network']={'state':'known','node':'deskb1x'}
    for i,n in enumerate(v['nodes']):
        n['network']={'state':'active' if i==0 else 'standby','sync':'synced','age_ms':1000,
            'subnet':'192.168.1.0/24','scan_minutes':1 if i==0 else 0,'scan_schedule':'*/1 * * * *' if i==0 else '',
            'voter_ready':True,'revision':{'term':33,'node':'deskb1x','sequence':40}}
    return v

def homeassistant_fixture():
    v=fixture();v['owners']['homeassistant']={'state':'known','node':'deskb3x'}
    for i,n in enumerate(v['nodes']):
        n['homeassistant']={'state':'active' if i==2 else 'standby','sync':'synced','ready':i==2,
            'checkpoint_revision':9,'pending_disk_changes':False if i==2 else None,'age_ms':1000,'checkpoint_age_ms':60000}
    return v

class PanelTests(unittest.TestCase):
    def test_homeassistant_metadata_is_optional_and_validated(self):
        panel.validate(fixture());panel.validate(homeassistant_fixture())
        for change in ({'age_ms':float('nan')},{'ready':1},{'checkpoint_revision':True},{'pending_disk_changes':1},{'state':'standby','ready':True}):
            v=homeassistant_fixture();v['nodes'][2]['homeassistant'].update(change)
            with self.assertRaises(ValueError):panel.validate(v)
    def test_homeassistant_roles_expire_and_standby_counts_match_revision(self):
        v=homeassistant_fixture();r=panel.Renderer.__new__(panel.Renderer);n=v['nodes'][2]
        self.assertIn('HA',r.overview_roles(n,v,True,0));self.assertEqual(r.owner(v,'homeassistant',True,0),'deskb3x')
        self.assertEqual(panel.homeassistant_detail(n,v,True,0)['replicas'],3)
        v['nodes'][0]['homeassistant']['checkpoint_revision']=8
        self.assertEqual(panel.homeassistant_detail(n,v,True,0)['replicas'],2)
        self.assertEqual(panel.homeassistant_detail(v['nodes'][0],v,True,0)['label'],'Pending')
        self.assertEqual(panel.homeassistant_detail(v['nodes'][0],v,True,0)['revision'],'r8 · latest r9')
        n['homeassistant']['pending_disk_changes']=True
        self.assertEqual(panel.homeassistant_detail(n,v,True,0)['label'],'Ready · pending')
        self.assertNotIn('HA',r.overview_roles(n,v,True,14000));self.assertEqual(r.owner(v,'homeassistant',True,14000),'Unconfirmed')
        self.assertEqual(panel.homeassistant_detail(n,v,True,14000)['label'],'Unavailable')
    def test_homeassistant_owner_with_unready_core_is_not_green(self):
        v=homeassistant_fixture();n=v['nodes'][2];n['homeassistant']['ready']=False
        detail=panel.homeassistant_detail(n,v,True,0)
        self.assertEqual(detail['label'],'Not ready');self.assertFalse(detail['ok'])
    def test_homeassistant_conflicting_owner_is_not_reported_ready(self):
        v=homeassistant_fixture();v['owners']['homeassistant']={'state':'conflict','node':None};v['nodes'][1]['homeassistant'].update(state='starting',ready=False)
        r=panel.Renderer.__new__(panel.Renderer);self.assertEqual(r.owner(v,'homeassistant',True,0),'Conflict')
        self.assertIn('HA !',r.overview_roles(v['nodes'][2],v,True,0))
        self.assertEqual(panel.homeassistant_detail(v['nodes'][2],v,True,0)['label'],'Conflict')
        self.assertFalse(panel.homeassistant_detail(v['nodes'][2],v,True,0)['ok'])

    def test_fetch_interval_change_wakes_existing_worker_without_starting_another(self):
        model=panel.Model(60);first=threading.Event();second=threading.Event()
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps(fixture()).encode()
        calls=[]
        def request(*args,**kwargs):
            calls.append(kwargs)
            if len(calls)==1:first.set()
            else:second.set();model.stop()
            return response
        model.opener=Mock();model.opener.open.side_effect=request
        worker=threading.Thread(target=model.poll,args=('http://192.168.1.100/api/cluster/panel',),daemon=True)
        worker.start()
        try:
            self.assertTrue(first.wait(1))
            model.set_interval(1)
            self.assertTrue(second.wait(1))
        finally:model.stop();worker.join(1)
        self.assertFalse(worker.is_alive());self.assertEqual(len(calls),2)
        self.assertTrue(all(call['timeout']==3 for call in calls))
        self.assertTrue(model.snapshot()[2])
    def test_fetch_worker_bounds_reject_invalid_intervals(self):
        model=panel.Model()
        for value in (0,61,True,1.0,'5',None):
            with self.assertRaises(ValueError):model.set_interval(value)
            with self.assertRaises(ValueError):panel.Model(value)
        self.assertEqual(model.interval,5)
    def test_timeout_dropdown_opens_at_current_value_and_can_select_every_option(self):
        menu=panel.TimeoutSelect()
        for seconds,_ in panel.TIMEOUT_OPTIONS:
            menu.open(seconds)
            index=next(i for i,(value,_) in enumerate(menu.items) if value==seconds)
            point=(40,menu.box[1]+index*menu.row_height-menu.offset+menu.row_height//2)
            self.assertTrue(panel.inside(point,menu.box))
            menu.begin(point);self.assertEqual(menu.end(point),seconds)
            menu.close();self.assertFalse(menu.opened)
    def test_timeout_dropdown_swipe_does_not_select_and_reaches_both_ends(self):
        menu=panel.TimeoutSelect();menu.open(0)
        menu.begin((40,250));menu.move((40,70))
        self.assertEqual(menu.offset,menu.max_offset)
        self.assertIsNone(menu.end((40,70)))
        self.assertTrue(menu.opened)
        menu.begin((40,250));self.assertEqual(menu.end((40,250)),1800)
        menu.begin((40,130));menu.move((40,300))
        self.assertEqual(menu.offset,0)
        self.assertIsNone(menu.end((40,300)))
        menu.begin((40,130));self.assertEqual(menu.end((40,130)),0)
    def test_timeout_dropdown_dismissal_cancels_pending_selection(self):
        menu=panel.TimeoutSelect();menu.open(600)
        menu.begin((40,210));menu.close()
        self.assertFalse(menu.opened);self.assertIsNone(menu.end((40,210)))
        menu.open(600)
        for point in ((28,25),(40,85),(40,300)):
            menu.begin(point);self.assertIsNone(menu.end(point))
    def test_settings_taps_open_only_the_touched_row(self):
        menu=panel.SettingsMenu()
        menu.begin((30,80));self.assertEqual(menu.end((33,82)),'calibration')
        menu.begin((30,150));self.assertEqual(menu.end((30,150)),'timeout')
        menu.begin((30,230));self.assertEqual(menu.end((30,230)),'fetch')
        menu.begin((30,272));self.assertIsNone(menu.end((30,272)))
        # Small motion across a divider must not open the adjacent row.
        menu.begin((30,120));self.assertIsNone(menu.end((30,125)))
    def test_settings_scroll_reaches_later_rows_and_clamps_at_both_ends(self):
        items=tuple((str(i),'Item','Details') for i in range(6))
        menu=panel.SettingsMenu(items)
        menu.begin((50,200));self.assertTrue(menu.move((50,56)))
        self.assertIsNone(menu.end((50,56)))
        menu.begin((50,90));self.assertEqual(menu.end((50,90)),'2')
        menu.begin((50,200));menu.move((50,-200))
        self.assertEqual(menu.offset,menu.max_offset)
        # Releasing a drag over bottom navigation must not select a list item.
        self.assertIsNone(menu.end((50,300)))
        menu.begin((50,100));menu.move((50,500));self.assertEqual(menu.offset,0)
        self.assertIsNone(menu.end((50,500)))
    def test_settings_drag_is_consumed_even_when_list_fits_or_returns_to_start(self):
        menu=panel.SettingsMenu()
        menu.begin((30,80));menu.move((30,40));menu.move((30,80))
        self.assertEqual(menu.offset,0);self.assertIsNone(menu.end((30,80)))
        menu.begin((30,80));self.assertIsNone(menu.end((60,80)))
        for point in ((20,20),(20,290)):
            menu.begin(point);menu.move((20,100));self.assertIsNone(menu.end((20,100)))
        menu.begin((30,80));menu.cancel();self.assertIsNone(menu.end((30,80)))
    def test_overview_monitor_marks_only_fresh_preference_from_ui_owner(self):
        v=network_fixture();v['monitoring_preferred']='deskb3x'
        for n in v['nodes']:n['monitoring_ready']=True;n['vip']={'xiaozhi':False,'dns':False,'vpn':False}
        r=panel.Renderer.__new__(panel.Renderer)
        self.assertNotIn('Monitor',r.overview_roles(v['nodes'][1],v,True,0))
        self.assertIn('Monitor',r.overview_roles(v['nodes'][2],v,True,0))
        v['nodes'][0]['age_ms']=21000
        self.assertNotIn('Monitor',r.overview_roles(v['nodes'][2],v,True,0))
        v['nodes'][0]['age_ms']=1000;v['nodes'][2]['monitoring_ready']=False
        self.assertNotIn('Monitor',r.overview_roles(v['nodes'][2],v,True,0))
    def test_monitor_preference_and_readiness_are_validated(self):
        v=fixture();v['monitoring_preferred']='unknown'
        with self.assertRaises(ValueError):panel.validate(v)
        v=fixture();v['nodes'][0]['monitoring_ready']=1
        with self.assertRaises(ValueError):panel.validate(v)
    def test_overview_keeps_all_roles_and_expires_network_independently(self):
        v=network_fixture();n=v['nodes'][0]
        n['vip']={'xiaozhi':True,'dns':True,'vpn':True}
        r=panel.Renderer.__new__(panel.Renderer)
        self.assertEqual(r.overview_roles(n,v,True,0),['Xiaozhi','DNS','VPN','Network'])
        self.assertEqual(r.overview_roles(n,v,True,14000),['Xiaozhi','DNS','VPN'])
        self.assertEqual(r.overview_roles(n,v,True,20000),['Unconfirmed'])
        n=v['nodes'][1];n['vip']={'xiaozhi':False,'dns':False,'vpn':False}
        self.assertEqual(r.overview_roles(n,v,True,0),['Standby'])
        self.assertEqual(r.overview_roles(n,v,True,14000),['Unconfirmed'])
    def test_overview_marks_conflicting_roles_on_each_claiming_node(self):
        v=network_fixture();v['owners']['vpn']={'state':'conflict','node':None}
        v['owners']['network']={'state':'conflict','node':None}
        v['nodes'][1]['network']['state']='active'
        for n in v['nodes'][:2]:n['vip']['vpn']=True
        r=panel.Renderer.__new__(panel.Renderer)
        for n in v['nodes'][:2]:self.assertEqual(r.overview_roles(n,v,True,0),['VPN !','Network !'])
    def test_vpn_owner_and_role_claims_expire_with_the_node(self):
        v=fixture();v['owners']['vpn']={'state':'known','node':'deskb1x'}
        for i,n in enumerate(v['nodes']):n['vip']['vpn']=i==0;n['vpn_ready']=True
        panel.validate(v);renderer=panel.Renderer.__new__(panel.Renderer)
        self.assertEqual(renderer.owner(v,'vpn',True,0),'deskb1x')
        self.assertEqual(panel.vip_role(v['nodes'][0],'vpn'),'Owner')
        self.assertEqual(panel.vip_role(v['nodes'][1],'vpn'),'Standby')
        self.assertEqual(panel.vip_role(v['nodes'][1],'dns'),'Unconfirmed')
        self.assertEqual(renderer.owner(v,'vpn',True,20000),'Unconfirmed')
        v['nodes'][1]['vip']['vpn']=True;v['owners']['vpn']={'state':'conflict','node':None}
        self.assertEqual(renderer.owner(v,'vpn',True,0),'Conflict')
        self.assertEqual(renderer.owner(v,'vpn',True,20000),'Unconfirmed')
    def test_vpn_metadata_rejects_non_boolean_live_claims(self):
        v=fixture();v['nodes'][0]['vpn_ready']=1
        with self.assertRaises(ValueError):panel.validate(v)
        v=fixture();v['nodes'][0]['vip']['vpn']='false'
        with self.assertRaises(ValueError):panel.validate(v)
    def test_network_is_optional_and_rejects_malformed_data(self):
        self.assertEqual(panel.validate(fixture())['state'],'ready')
        self.assertEqual(panel.validate(network_fixture())['state'],'ready')
        for change in ({'age_ms':float('nan')},{'scan_minutes':True},{'scan_schedule':'bad\ncron'},
                       {'state':'invented'},{'voter_ready':1},{'revision':{'term':1,'node':'unknown','sequence':3}}):
            v=network_fixture();v['nodes'][0]['network'].update(change)
            with self.assertRaises(ValueError):panel.validate(v)
    def test_network_expires_before_node_without_another_api_reply(self):
        v=network_fixture();n=v['nodes'][0]
        self.assertTrue(panel.network_live(n,True,13000))
        self.assertTrue(panel.node_live(n,True,14000))
        self.assertFalse(panel.network_live(n,True,14000))
        renderer=panel.Renderer.__new__(panel.Renderer)
        self.assertEqual(renderer.owner(v,'network',True,14000),'Unconfirmed')
        self.assertEqual(panel.network_detail(n,v,True,14000)['label'],'Unknown')
        v['owners']['network']={'state':'conflict','node':None};v['nodes'][1]['network']['state']='active'
        self.assertEqual(renderer.owner(v,'network',True,0),'Conflict')
        self.assertEqual(renderer.owner(v,'network',True,14000),'Unconfirmed')
    def test_network_replica_behind_checkpoint_or_warming_cannot_claim_synced(self):
        v=network_fixture();n=v['nodes'][1]
        self.assertEqual(panel.network_detail(n,v,True,0)['label'],'Synced')
        n['network']['revision']['sequence']=39
        self.assertEqual(panel.network_detail(n,v,True,0)['label'],'Pending')
        n['network']['voter_ready']=False
        self.assertEqual(panel.network_detail(n,v,True,0)['label'],'Warming up')
        n['network']['revision']=None
        self.assertFalse(panel.network_detail(n,v,True,0)['ok'])
    def test_all_five_navigation_buttons_match_the_rendered_width(self):
        for i,(_,page) in enumerate(panel.NAVIGATION):
            for x in (i*96,i*96+48,i*96+95):self.assertEqual(panel.navigation_page(x),page)
        self.assertEqual(panel.navigation_page(-1),'overview')
        self.assertEqual(panel.navigation_page(480),'settings')
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
