import copy
import io
from unittest.mock import Mock, patch
import importlib.util
from pathlib import Path
import unittest
R=Path(__file__).resolve().parents[1]/'files'
def load(name):
 s=importlib.util.spec_from_file_location(name,R/(name+'.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
ui=load('cluster_ui');identity=load('client_identity')
class ClusterTests(unittest.TestCase):
 def stats(self,n,latency):
  return {'time_units':'hours','num_dns_queries':n,'num_blocked_filtering':n//2,'avg_processing_time':latency,'dns_queries':[0,n], 'blocked_filtering':[0,n//2], 'top_clients':[{'192.168.1.145':n}], 'top_upstreams_responses':[{'1.1.1.1':n}], 'top_upstreams_avg_time':[{'1.1.1.1':latency}]}
 def test_plaintext_clear_ack_is_success_without_repeating_mutation(self):
  server=object.__new__(ui.Server);server.config={'nodes':{'deskb1x':{'private':'10.10.10.11'}},'authorization':'Basic fixture'};server.context=None
  opener=Mock();opener.open.return_value=io.BytesIO(b'OK\n')
  with patch.object(ui.urllib.request,'build_opener',return_value=opener):
   self.assertIsNone(server.call('deskb1x','querylog_clear','POST',b''))
  self.assertEqual(opener.open.call_count,1)
  request=opener.open.call_args.args[0]
  self.assertIsNone(request.data)
  self.assertIsNone(request.get_header('Content-type'))
 def test_hostname_keeps_ip_visible_without_changing_stored_client(self):
  source={'192.168.1.145':{'name':'Workstation','ids':['192.168.1.145']}}
  self.assertEqual(ui.merge_lookup([[source]])[0]['192.168.1.145']['name'],'Workstation')
  self.assertEqual(source['192.168.1.145']['name'],'Workstation')
 def test_native_null_client_collections_and_lookup_are_valid(self):
  result=ui.merge_clients([{'clients':None,'auto_clients':None,'supported_tags':None}])
  self.assertEqual(result,{'clients':[],'auto_clients':[],'supported_tags':[]})
  self.assertEqual(ui.merge_lookup([[{'192.168.1.9':None}]]),[{'192.168.1.9':{'name':''}}])
  self.assertEqual(ui.merge_logs({'deskb1x':{'data':None}},100),{'data':[],'oldest':''})
 def test_inventory_names_are_available_without_hiding_ip_or_overwriting_names(self):
  nodes={'deskb1x':{'management':'192.168.1.101','private':'10.10.10.11'}}
  values=[{'192.168.1.101':None},{'192.168.1.23':{'name':'phone'}}]
  result=ui.known_client_names('clients/search',values,nodes)
  self.assertEqual(ui.merge_lookup([result])[0]['192.168.1.101']['name'],'deskb1x')
  self.assertEqual(result[1]['192.168.1.23']['name'],'phone')
 def test_all_counters_and_weighted_latency(self):
  value=ui.merge_stats([self.stats(10,1),self.stats(20,2),self.stats(30,3)])
  self.assertEqual(value['num_dns_queries'],60);self.assertEqual(value['dns_queries'],[0,60]);self.assertEqual(value['top_clients'],[{'192.168.1.145':60}]);self.assertAlmostEqual(value['avg_processing_time'],140/60)
 def test_recorded_node_ips_are_not_hidden_from_chart(self):
  value=self.stats(5,1);value['top_clients'].extend([{'10.10.10.13':1000}])
  result=ui.merge_stats([value]);self.assertEqual(result['top_clients'],[{'10.10.10.13':1000},{'192.168.1.145':5}]);self.assertEqual(result['num_dns_queries'],5)
 def test_incompatible_window_is_not_silently_summed(self):
  a=self.stats(1,0);b=self.stats(1,0);b['dns_queries']=[1]
  with self.assertRaises(ValueError):ui.merge_stats([a,b])
 def test_devices_are_unioned_without_losing_resolved_names(self):
  values=[{'auto_clients':[{'ip':'192.168.1.145','name':''}],'clients':[{'name':'phone','ids':['192.168.1.8']}]},{'auto_clients':[{'ip':'192.168.1.145','name':'mac'},{'ip':'192.168.1.8','name':'phone'}],'clients':[{'name':'phone','ids':['192.168.1.8']}]}]
  merged=ui.merge_clients(values);self.assertEqual(len(merged['clients']),1);self.assertEqual(len(merged['auto_clients']),2);self.assertEqual(merged['auto_clients'][0]['name'],'mac')
 def test_log_merge_orders_globally_keeps_distinct_requests_and_paginates(self):
  rows={'deskb1x':{'data':[{'time':'2026-10-11T12:00:03Z','client':'192.168.1.8'},{'time':'2026-10-11T12:00:01Z','client':'192.168.1.9'}]},'deskb2x':{'data':[{'time':'2026-10-11T12:00:02Z','client':'192.168.1.8'}]}}
  merged=ui.merge_logs(rows,1,1);self.assertEqual(merged['data'][0]['desk_node'],'deskb2x');self.assertEqual(merged['oldest'],'2026-10-11T12:00:02Z')
 def test_transport_patch_preserves_filters_keys_ports_and_is_idempotent(self):
  source={'http':{'address':'127.0.0.1:3001'},'dns':{'port':5354,'bind_hosts':['10.10.10.11'],'allowed_clients':['10.10.10.11']},'users':[],'filters':[{'id':3,'url':'https://example.test/list'}],'user_rules':['my-rule'],'tls':{'enabled':False}}
  before=copy.deepcopy(source);value=identity.update(source)
  self.assertEqual(source,before);self.assertEqual(value['filters'],source['filters']);self.assertEqual(value['user_rules'],source['user_rules']);self.assertEqual(value['dns']['port'],5354);self.assertEqual(value['tls'],source['tls']);self.assertEqual(identity.update(value),value)
if __name__=='__main__':unittest.main()
