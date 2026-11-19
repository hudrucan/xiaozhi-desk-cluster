"""Composed/fallback health checks retain their boundaries while polling less."""
import base64,json,re,unittest
import jinja2,yaml
from test_https import ROOT,render,proxy

class Cadence(unittest.TestCase):
 def servers(self,text,backend):
  block=re.search(r'^backend '+backend+r'\n(.*?)(?=^(?:backend|frontend) |\Z)',text,re.M|re.S)[1]
  return [line.strip() for line in block.splitlines() if line.strip().startswith('server ')]
 def check(self,lines,count,policy):
  self.assertEqual(len(lines),count)
  for line in lines:self.assertIn(' check '+policy,line)
 def test_all_three_composed_nodes_keep_critical_checks_and_verified_tls(self):
  for node in ['deskb1x','deskb2x','deskb3x']:
   current,fragment,_=render(node);text=proxy.https_candidate(current,fragment)
   for suffix in ['cluster','ntfy','ha','herdr']:
    lines=self.servers(text,'desk_caddy_'+suffix);self.check(lines,3,'inter 5s fastinter 2s downinter 5s fall 2 rise 2')
    for line in lines:
     self.assertIn('send-proxy-v2 check-send-proxy check-ssl check-sni ',line);self.assertIn('verify required verifyhost ',line)
   for backend in ['desk_monitoring_hubs','desk_dashboard','desk_node_control']:
    self.check(self.servers(text,backend),3,'inter 10s fastinter 5s downinter 10s fall 2 rise 2')
   self.check(self.servers(text,'desk_vpn_ui'),3,'inter 10s fastinter 3s downinter 10s fall 3 rise 2')
   self.check(self.servers(text,'xiaozhi_control_planes'),3,'inter 2s fall 2 rise 2')
   self.check(self.servers(text,'desk_adguard_ui'),1,'inter 5s fall 2 rise 2')
   self.check(self.servers(text,'desk_network'),3,'inter 3s fall 2 rise 1')
 def test_canonical_fragments_and_fallback_share_cadence_in_both_monitoring_modes(self):
  nodes=['deskb1x','deskb2x','deskb3x'];env=jinja2.Environment(undefined=jinja2.StrictUndefined,trim_blocks=True,lstrip_blocks=True)
  values=dict(groups={'desk_monitoring':nodes,'deskboxes':nodes},hostvars={n:{'cluster_ip':f'10.10.10.{i+11}'} for i,n in enumerate(nodes)},cluster_ip='10.10.10.11',desk_monitoring_hub_port=8090,desk_monitoring_proxy_port=8091,desk_monitoring_sync_enabled=True,desk_node_control_enabled=True)
  for enabled in [False,True]:
   values['desk_monitoring_load_aware_enabled']=enabled
   for role,backend,count,policy in [('desk_monitoring','desk_monitoring_hubs',3,'inter 10s fastinter 5s downinter 10s fall 2 rise 2'),('desk_dashboard','desk_dashboard',3,'inter 10s fastinter 5s downinter 10s fall 2 rise 2'),('desk_dashboard','desk_node_control',3,'inter 10s fastinter 5s downinter 10s fall 2 rise 2'),('desk_vpn_ui','desk_vpn_ui',3,'inter 10s fastinter 3s downinter 10s fall 3 rise 2')]:
    text=env.from_string((ROOT/f'roles/{role}/templates/backend.cfg.j2').read_text()).render(values)
    self.check(self.servers(text,backend),count,policy)
    if role=='desk_monitoring':self.assertEqual(text.count('agent-inter 30s'),3 if enabled else 0)

if __name__=='__main__':unittest.main()
