import copy
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

auth=load('ui_auth',ROOT/'files/ui_auth.py')
guard=load('ui_guard',ROOT/'files/ui_guard.py')
proxy=load('ui_proxy',REPO/'roles/desk_monitoring/files/install_proxy.py')

class UITests(unittest.TestCase):
    def test_auth_candidate_preserves_policy_and_peer_credentials(self):
        document={'schema_version':34,'users':[{'name':'admin','password':'fixture'}], 'dns':{'port':5354,'upstream_dns':['1.1.1.1']},'user_rules':['custom']}
        config={'username':'desk-sync','password':'fixture-peer-secret','certificate':'/retained','local_auth':True}
        before=copy.deepcopy(document)
        wanted,agent=auth.candidate(document,config)
        self.assertEqual(document,before)
        self.assertEqual(wanted,{**before,'users':[]})
        self.assertEqual(agent,{**config,'local_auth':False})
        self.assertEqual(auth.candidate(wanted,agent),(wanted,agent))

    def test_unified_routes_are_idempotent_and_preserve_settings_internal_and_mqtt(self):
        current='frontend xiaozhi_settings\n    http-request deny deny_status 404 if { path_beg /internal/ }\n    default_backend xiaozhi_control_planes\nbackend xiaozhi_control_planes\n    server deskb1x 10.10.10.11:8004 check\n'
        monitoring='backend desk_monitoring_hubs\n    server deskb1x 10.10.10.11:8090 check\n'
        dns='backend desk_adguard_ui\n    server dns_vip 192.168.1.250:3001 check\n'
        wanted=proxy.candidate(current,monitoring,dns)
        self.assertEqual(proxy.candidate(wanted,monitoring,dns),wanted)
        self.assertEqual(guard.base(wanted),current)
        self.assertLess(wanted.index('path_beg /internal/'),wanted.index('use_backend desk_adguard_ui'))
        # A later monitoring-only run must retain the DNS route and backend.
        self.assertEqual(proxy.candidate(wanted,monitoring),wanted)
        self.assertIn('src 192.168.1.0/24 127.0.0.1',wanted)
        frontend=wanted.split('\nbackend xiaozhi_control_planes\n')[0]
        self.assertLess(frontend.rfind('    http-request '),frontend.find('    use_backend '))

    def test_dns_ui_guard_allows_only_the_requested_peer_acl_change(self):
        peers=['192.168.1.101','192.168.1.102','192.168.1.103']
        old='frontend dns_admin\n    http-request deny unless { src 192.168.1.0/24 127.0.0.1 }\n    default_backend local_adguard\n'
        new=old.replace('192.168.1.0/24',' '.join(peers))
        self.assertEqual(guard.ui_base(old,peers),guard.ui_base(new,peers))
        with self.assertRaises(ValueError):guard.ui_base(old.replace('192.168.1.0/24','0.0.0.0/0'),peers)
        self.assertNotEqual(guard.ui_base(new.replace('local_adguard','different_backend'),peers),guard.ui_base(old,peers))

    def test_guard_rejects_runtime_and_policy_drift(self):
        before={'units':{'xiaozhi-core':{'MainPID':'123'}},'dns_policy':'same'}
        guard.guard(before,copy.deepcopy(before))
        for current in ({**before,'dns_policy':'changed'}, {**before,'units':{}}):
            with self.assertRaises(ValueError):guard.guard(before,current)

if __name__=='__main__':unittest.main()
