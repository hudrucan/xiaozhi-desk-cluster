"""Fake HTTPS/API fixtures: no external DNS updates, token reads or hardware."""
import importlib.util,time,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('ddns',Path(__file__).resolve().parents[1]/'files/ddns.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
SECRET={'api_token':'fixture_token_1234567890','zone_id':'a'*32,'record_id':'b'*32}
class Fake:
    def __init__(self,content='1.1.1.1',update=None):
        self.calls=[];self.value={'id':'b'*32,'name':d.HOST,'type':'A','content':content,'proxied':False,'ttl':120};self.update=update
    def public_ip(self):self.calls.append('ip');return '8.8.8.8'
    def request(self,url,body=None,cloud=False):
        self.calls.append((url,body));v=dict(self.value)
        if body:
            self.value.update(body);v=dict(self.value)
            if self.update:v.update(self.update)
        return v
class DDNS(unittest.TestCase):
    def test_standby_has_no_http_calls(self):
        c=Fake();self.assertEqual(d.reconcile(c,SECRET,lambda:False),'standby');self.assertFalse(c.calls)
    def test_owner_loss_during_http_never_updates(self):
        c=Fake();owner=iter((True,False));self.assertEqual(d.reconcile(c,SECRET,lambda:next(owner)),'ownership_changed')
        self.assertEqual(len(c.calls),2);self.assertIsNone(c.calls[1][1])
    def test_unchanged_ip_skips_patch(self):
        c=Fake('8.8.8.8');self.assertEqual(d.reconcile(c,SECRET,lambda:True),'unchanged');self.assertEqual(len(c.calls),2)
    def test_pinned_a_updates_and_reads_back(self):
        c=Fake();self.assertEqual(d.reconcile(c,SECRET,lambda:True),'updated')
        patches=[body for call in c.calls if isinstance(call,tuple) for _,body in [call] if body is not None]
        self.assertEqual(patches,[{'content':'8.8.8.8','ttl':120,'proxied':False}])
        self.assertEqual(c.calls[-1][1],None)
        self.assertTrue(all(call[0].endswith('/'+SECRET['record_id']) for call in c.calls if isinstance(call,tuple)))
    def test_other_hostname_type_or_proxy_never_modified(self):
        for bad in ({'name':'other.huynm.dev'},{'type':'CNAME'},{'id':'c'*32},{'proxied':True}):
            c=Fake();c.value.update(bad)
            with self.assertRaises(d.Failure):d.reconcile(c,SECRET,lambda:True)
            self.assertFalse(any(call[1] for call in c.calls if isinstance(call,tuple)))
    def test_disagreement_and_nonpublic_ip_fail_closed(self):
        for value in ('192.168.1.1','127.0.0.1','100.64.1.1','::1','1.1.1.1\nother','0.0.0.0','999.1.1.1'):
            with self.assertRaises(d.Failure):d.public_ipv4(value)
        c=d.Client(SECRET['api_token'])
        with patch.object(c,'request',side_effect=['1.1.1.1','8.8.8.8']):
            with self.assertRaises(d.Failure):c.public_ip()
    def test_wrong_node_boot_stale_and_missing_health_not_eligible(self):
        config={'protocol':'desk-vpn-ha-v1','vip':d.VIP,'node':'deskb1x'}
        addr=[{'ifname':'wlan0','addr_info':[{'family':'inet','local':d.VIP}]}]
        health={'protocol':'desk-vpn-status-v1','node':'deskb1x','boot_id':'boot','ready':True,'_timestamp':100}
        self.assertTrue(d.eligible(config,addr,health,'boot',100))
        for bad in ({'node':'deskb2x'},{'boot_id':'old'},{'ready':False},{'_timestamp':79},{'_timestamp':101},{'protocol':'other'}):
            self.assertFalse(d.eligible(config,addr,{**health,**bad},'boot',100))
        self.assertFalse(d.eligible(config,[],health,'boot',100))
    def test_start_health_and_live_ownership_are_checked_separately(self):
        c=Fake()
        with patch.object(d,'is_owner') as unused:
            start=lambda:True
            still=lambda:False
            self.assertEqual(d.reconcile(c,SECRET,start,still),'ownership_changed')
            self.assertFalse(any(call[1] for call in c.calls if isinstance(call,tuple)))
        config={'protocol':'desk-vpn-ha-v1','vip':d.VIP,'node':'deskb1x'}
        addr=[{'ifname':'wlan0','addr_info':[{'family':'inet','local':d.VIP}]}]
        status={'protocol':'desk-vpn-status-v1','node':'deskb1x','boot_id':'boot','ready':True,'_timestamp':100}
        self.assertFalse(d.eligible(config,addr,status,'boot',125))
        self.assertTrue(d.eligible(config,addr,status,'boot',125,check_fresh=False))
        self.assertFalse(d.eligible(config,[],status,'boot',125,check_fresh=False))
        self.assertFalse(d.eligible(config,addr,status,'newboot',125,check_fresh=False))

    def test_update_readback_mismatch_is_error(self):
        c=Fake(update={'content':'9.9.9.9'})
        with self.assertRaises(d.Failure):d.reconcile(c,SECRET,lambda:True)
    def test_redirect_is_refused(self):
        with self.assertRaises(d.Failure):d.NoRedirect().redirect_request(None,None,None,None,None,None)
    def test_main_checks_fresh_start_once_and_live_vip_before_patch(self):
        c=Fake()
        with patch.object(d,'is_owner',side_effect=[True,False]) as owned,patch.object(d,'load',return_value=SECRET),patch.object(d,'Client',return_value=c),patch('builtins.print') as out,patch.dict(d.os.environ,{'CREDENTIALS_DIRECTORY':'/fake'}):
            self.assertEqual(d.main(),0)
            self.assertEqual(owned.call_count,2)
            self.assertEqual(owned.call_args.kwargs,{'check_fresh':False})
            self.assertIn('ownership_changed',str(out.call_args))
            self.assertFalse(any(call[1] for call in c.calls if isinstance(call,tuple)))

    def test_failure_never_outputs_token(self):
        with patch.object(d,'is_owner',return_value=True),patch.object(d,'load',side_effect=OSError(SECRET['api_token'])),patch('builtins.print') as out,patch.dict(d.os.environ,{'CREDENTIALS_DIRECTORY':'/fake'}):
            self.assertEqual(d.main(),1)
            self.assertNotIn(SECRET['api_token'],str(out.call_args))
if __name__=='__main__':unittest.main()
