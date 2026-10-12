import http.client, importlib.util, io, json, threading, unittest
from pathlib import Path
from unittest.mock import patch, Mock
import urllib.error
spec=importlib.util.spec_from_file_location('ui_boundary',Path(__file__).resolve().parents[1]/'files/cluster_ui.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class BoundaryTests(unittest.TestCase):
 def setUp(self):
  cfg={'node':'node1','nodes':{n:{'private':'10.10.10.'+str(i),'management':'192.168.1.'+str(i)} for i,n in enumerate(('node1','node2','node3'),1)},'authorization':'Basic fixture','ca':'unused'}
  with patch.object(m.ssl,'create_default_context',return_value=None),patch.object(m.http.server.HTTPServer,'server_bind',lambda s:None),patch.object(m.http.server.HTTPServer,'server_activate',lambda s:None):
   self.server=m.Server(cfg)
  self.server.server_address=('127.0.0.1',0);self.server.server_bind();self.server.server_activate()
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  self.addCleanup(self.cleanup)
 def cleanup(self):
  self.server.shutdown();self.server.server_close();self.server.pool.shutdown();self.thread.join()
 def request(self,path,method='GET',body=None,auth=None):
  c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
  c.request(method,path,None if body is None else json.dumps(body),{'Authorization':auth} if auth else {})
  response=c.getresponse();data=response.read();headers=dict(response.getheaders());status=response.status;c.close()
  return status,data,headers
 def test_reserved_rewrite_and_host_local_dns_save_do_not_reach_native_or_policy(self):
  with patch.object(self.server,'policy') as policy,patch.object(self.server,'native') as native:
   for path,method,body in [('/control/rewrite/settings/update','PUT',{'enabled':False}),('/control/rewrite/delete','POST',{'domain':'who.desk-dns.test','answer':'any'}),('/control/dns_config','POST',{'port':53})]:
    self.assertEqual(self.request(path,method,body)[0],400)
   policy.assert_not_called();native.assert_not_called()
 def test_all_managed_save_routes_use_policy_writer_and_truthful_coverage(self):
  for path,method,body in [('rewrite/add','POST',{'domain':'home.test','answer':'192.168.1.8'}),('querylog/config/update','PUT',{'enabled':True}),('clients/add','POST',{'name':'phone','ids':['192.168.1.8']}),('filtering/add_url','POST',{'url':'https://example.invalid/list','name':'Test'})]:
   with patch.object(self.server,'policy',return_value={'result':None,'revision':7,'status':202,'ready':['node1','node2']}) as policy:
    status,data,headers=self.request('/control/'+path,method,body)
    self.assertEqual(status,202);self.assertEqual(headers['X-Desk-DNS-Coverage'],'2/3');self.assertEqual(headers['X-Desk-DNS-Revision'],'7')
    self.assertEqual(headers['X-Desk-DNS-State'],'pending');self.assertEqual(policy.call_args.args[0],'write')
 def test_native_document_loads_only_the_pending_fetch_adapter_without_layout_changes(self):
  with patch.object(self.server,'native',return_value=(b'<html><head></head><body>native</body></html>','text/html','node1')):
   status,data,headers=self.request('/')
  self.assertEqual(status,200);self.assertIn(b'src="control/policy-wait.js"',data)
  self.assertIn(b'<body>native</body>',data);self.assertNotIn(b'Cluster 3/3',data)
  status,data,headers=self.request('/control/policy-wait.js')
  self.assertEqual(status,200);self.assertIn(b'window.fetch',data);self.assertIn('javascript',headers['Content-Type'])
 def test_pending_reply_correlates_the_operation_without_fake_complete_coverage(self):
  with patch.object(self.server,'policy',return_value={'result':{'updated':0},'revision':7,'status':202,'ready':[],'operation_id':'fixture'}):
   status,data,headers=self.request('/control/filtering/refresh','POST',{'whitelist':True})
  self.assertEqual(status,202);self.assertEqual(headers['X-Desk-DNS-Operation'],'fixture')
  self.assertEqual(headers['X-Desk-DNS-Coverage'],'0/3')
 def test_native_root_assets_and_read_health_use_available_source(self):
  with patch.object(self.server,'native',return_value=(b'asset','text/html','node2')) as native:
   self.assertEqual(self.request('/')[1],b'asset')
   self.assertEqual(self.request('/main.js')[1],b'asset')
   self.assertEqual(self.request('/control/desk_health')[0],200)
   self.assertEqual(native.call_args.args[0],'/control/status')
  with patch.object(self.server,'native',side_effect=OSError):
   self.assertEqual(self.request('/control/desk_health')[0],503)
 def test_private_asset_and_policy_rpc_require_shared_auth(self):
  for path,method,body in [('/desk-observe/ui/main.js','GET',None),('/desk-observe/policy/status','POST',{})]:
   self.assertEqual(self.request(path,method,body)[0],401)
  with patch.object(self.server,'native_local',return_value=(b'javascript','application/javascript','node1')):
   self.assertEqual(self.request('/desk-observe/ui/main.js',auth='Basic fixture')[1],b'javascript')
 def test_status_retains_native_bootstrap_shape_not_null_aggregate(self):
  body=b'{"running":true,"protection_enabled":true,"version":"fixture"}'
  with patch.object(self.server,'native',return_value=(body,'application/json','node2')):
   status,data,headers=self.request('/control/status')
  self.assertEqual(status,200);self.assertTrue(json.loads(data)['running']);self.assertEqual(headers['X-Desk-DNS-Coverage'],'1/3')
 def test_partial_clear_is_accepted_and_never_claims_complete_coverage(self):
  with patch.object(self.server,'fanout',return_value=({'node1':None,'node2':None},['node3'])):
   status,data,headers=self.request('/control/querylog_clear','POST')
  self.assertEqual(status,202);self.assertEqual(headers['X-Desk-DNS-Coverage'],'2/3')
 def test_clear_deadline_matches_mutation_not_three_second_read(self):
  server=object.__new__(m.Server);server.config={'node':'node1','nodes':{'node1':{'private':'127.0.0.1'}},'authorization':'Basic fixture'};server.context=None
  opener=Mock();opener.open.return_value=io.BytesIO(b'OK')
  with patch.object(m.urllib.request,'build_opener',return_value=opener):
   self.assertIsNone(server.call('node1','querylog_clear','POST'))
  self.assertEqual(opener.open.call_args.kwargs['timeout'],60)
 def test_owner_native_failure_selects_authenticated_peer_asset(self):
  opener=Mock();data=io.BytesIO(b'peer-asset');data.headers={'Content-Type':'application/javascript'};opener.open.return_value=data
  with patch.object(self.server,'native_local',side_effect=ConnectionRefusedError),patch.object(m.urllib.request,'build_opener',return_value=opener):
   value,mime,node=self.server.native('/main.js')
  self.assertEqual((value,mime,node),(b'peer-asset','application/javascript','node2'))
  request=opener.open.call_args.args[0];self.assertEqual(request.get_header('Authorization'),'Basic fixture')
if __name__=='__main__':unittest.main()
