import copy, importlib.util, tempfile, threading, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]/'files'
def load(name):
 spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
 m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
sync=load('desk_dns_sync');m=load('dns_policy')

def initial():
 return {'status':{'protection_enabled':True},'profile':{'language':'en','theme':'dark'},
  'querylog/config':{'enabled':True,'interval':24,'ignored':[]},'stats/config':{'enabled':True,'interval':24,'ignored':[]},
  'dns_info':{'upstream_dns':['1.1.1.1']},'rewrite/settings':{'enabled':True},'rewrite/list':[],
  'clients':{'clients':[]},'blocked_services/get':{'ids':[],'schedule':{}},
  'safesearch/status':{'enabled':False},'safebrowsing/status':{'enabled':False},'parental/status':{'enabled':False},
  'filtering/status':{'enabled':True,'interval':0,'filters':[],'whitelist_filters':[],'user_rules':[sync.RULE]}}
class PolicyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.members=['node1','node2','node3'];self.owner='node1';self.offline=set();self.nodes={};self.data={n:initial() for n in self.members};self.calls=[];self.fail=set()
  for node in self.members:
   config={'node':node,'nodes':self.members,'password':'fixture-private-sync-password','state':self.tmp.name+'/'+node+'/state.json','vip':'vip','adguard':node,'write_wait':2}
   Path(config['state']).parent.mkdir()
   def fetch(url,config,method,value,**kwargs):
    self.mutate(config['node'],url.split('/control/')[1],value)
   api=SimpleNamespace(owns=lambda vip,node=node:node==self.owner,policy_snapshot=lambda base,config:copy.deepcopy(self.data[config['node']]),
    pending_filter_job=lambda:False,encoded=sync.encoded,WRITES=sync.WRITES,validate_write=sync.validate_write,native_ready=lambda node:node not in self.fail,
    fetch=fetch,filter_write=lambda config,route,body:self.mutate(config['node'],route,body))
   c=m.Policy(config,api);self.nodes[node]=c;self.addCleanup(c.pool.shutdown)
  def call(sender,node,action,payload=None):
   if node in self.offline:raise OSError('offline')
   return self.nodes[node].peer(action,payload or {})
  for c in self.nodes.values():c.call=lambda node,action,payload=None,c=c:call(c,node,action,payload)
  self.nodes[self.owner].ensure();self.nodes[self.owner].converge()
 def mutate(self,node,route,value):
  if node in self.fail:raise m.Failure(503,'native temporarily unavailable')
  self.calls.append((node,route));data=self.data[node]
  reads={'querylog/config/update':'querylog/config','stats/config/update':'stats/config','dns_config':'dns_info','profile/update':'profile','rewrite/settings/update':'rewrite/settings','safesearch/settings':'safesearch/status','blocked_services/update':'blocked_services/get'}
  if route in reads:
   if route=='dns_config':data[reads[route]].update(copy.deepcopy(value))
   else:data[reads[route]]=copy.deepcopy(value)
  elif route=='filtering/add_url':
   key='whitelist_filters' if value.get('whitelist') else 'filters'
   data['filtering/status'][key].append({k:value.get(k,True) for k in ('url','name','enabled')})
  elif route=='filtering/remove_url':
   key='whitelist_filters' if value.get('whitelist') else 'filters';data['filtering/status'][key]=[x for x in data['filtering/status'][key] if x['url']!=value['url']]
  elif route=='filtering/set_url':
   key='whitelist_filters' if value.get('whitelist') else 'filters';data['filtering/status'][key]=[copy.deepcopy(value['data']) if x['url']==value['url'] else x for x in data['filtering/status'][key]]
  elif route=='filtering/config':data['filtering/status'].update(value)
  elif route=='filtering/set_rules':data['filtering/status']['user_rules']=value['rules']
  elif route=='filtering/refresh':return {'updated':1}
  elif route=='rewrite/add':data['rewrite/list'].append(copy.deepcopy(value))
  elif route=='rewrite/delete':data['rewrite/list'].remove(value)
  elif route=='clients/add':data['clients']['clients'].append(copy.deepcopy(value))
  elif route=='clients/delete':data['clients']['clients']=[x for x in data['clients']['clients'] if x['name']!=value['name']]
  elif route=='clients/update':data['clients']['clients']=[copy.deepcopy(value['data']) if x['name']==value['name'] else x for x in data['clients']['clients']]
  elif route=='protection':data['status']['protection_enabled']=value['enabled']
  elif route.split('/')[0] in ('parental','safebrowsing'):data[route.split('/')[0]+'/status']={'enabled':route.endswith('/enable')}
  else:raise AssertionError(route)
 def write(self,route='querylog/config/update',body=None):
  return self.nodes[self.owner].write(route,body or {'enabled':True,'interval':24,'ignored':['probe.test']})
 def test_uninitialized_heads_never_claim_applied_coverage(self):
  c=self.nodes['node1']
  with patch.object(c,'refresh',return_value={n:{'revision':-1,'applied_revision':-1} for n in self.members}):
   c.journal.state['revision']=-1
   self.assertEqual(c.public_status()['ready'],[])
 def test_success_verifies_all_native_replicas(self):
  reply=self.write();self.assertEqual(reply['status'],200);self.assertEqual(len(reply['ready']),3)
  self.assertTrue(all(v['querylog/config']['ignored']==['probe.test'] for v in self.data.values()))
 def test_committed_owner_failover_never_publishes_stale_native_policy(self):
  self.offline.add('node3');reply=self.write();self.assertEqual(reply['status'],202);self.assertEqual(len(reply['ready']),2)
  self.assertEqual(self.data['node3']['querylog/config']['ignored'],[])
  self.offline.clear();self.owner='node3';self.nodes['node3'].ensure()
  self.assertEqual(self.data['node3']['querylog/config']['ignored'],['probe.test'])
  self.write('stats/config/update',{'enabled':True,'interval':24,'ignored':['second.test']})
  self.assertTrue(all(v['querylog/config']['ignored']==['probe.test'] for v in self.data.values()))
 def test_native_pending_peer_does_not_claim_three_acknowledgments(self):
  self.fail.add('node3');reply=self.write();self.assertEqual(reply['status'],202);self.assertNotIn('node3',reply['ready'])
  self.fail.clear();self.nodes['node3'].reconcile();self.assertEqual(self.data['node3']['querylog/config']['ignored'],['probe.test'])
 def test_no_quorum_never_acknowledges_a_shared_save(self):
  self.offline.update(('node2','node3'))
  with self.assertRaises(m.Failure):self.write()
  self.assertEqual(self.nodes['node1'].journal.state['revision'],0)
 def test_idle_reconciliation_has_no_disk_writes_or_native_mutations(self):
  before={n:c.journal.path.stat().st_mtime_ns for n,c in self.nodes.items()};self.calls.clear()
  for _ in range(3):
   for c in self.nodes.values():c.reconcile()
  self.assertEqual(before,{n:c.journal.path.stat().st_mtime_ns for n,c in self.nodes.items()});self.assertEqual(self.calls,[])
 def test_reserved_rewrites_rejected_before_any_native_write(self):
  self.calls.clear()
  for route,body in [('rewrite/settings/update',{'enabled':False}),('rewrite/delete',{'domain':'health.desk-dns.test','answer':'127.0.0.1'})]:
   with self.assertRaises(ValueError):self.write(route,body)
  self.assertEqual(self.calls,[])
 def test_filter_crud_and_disabled_flags_are_replicated(self):
  body={'url':'https://example.invalid/list','name':'Test','enabled':False,'whitelist':False}
  reply=self.write('filtering/add_url',body);self.assertEqual(reply['status'],200)
  self.assertTrue(all(v['filtering/status']['filters'][0]['enabled']==False for v in self.data.values()))
  self.write('filtering/remove_url',{'url':body['url'],'whitelist':False})
  self.assertTrue(all(not v['filtering/status']['filters'] for v in self.data.values()))
 def test_refresh_is_serial_once_per_node_and_not_repeated_by_idle_sync(self):
  self.calls.clear();reply=self.write('filtering/refresh',{'force':True,'whitelist':False});self.assertEqual(reply['status'],200)
  self.assertEqual([n for n,r in self.calls if r=='filtering/refresh'],self.members)
  for c in self.nodes.values():c.reconcile()
  self.assertEqual(len([r for _,r in self.calls if r=='filtering/refresh']),3)
 def test_client_rewrite_protection_and_profile_settings_share_the_same_revision_boundary(self):
  for route,body in [('clients/add',{'name':'phone','ids':['192.168.1.8']}),('rewrite/add',{'domain':'printer.home','answer':'192.168.1.20'}),('protection',{'enabled':False,'duration':0}),('profile/update',{'language':'vi','theme':'dark'})]:
   self.assertEqual(self.write(route,body)['status'],200)
  self.assertTrue(all(v==self.data['node1'] for v in self.data.values()))
 def test_restart_loads_encrypted_policy_not_native_stale_snapshot(self):
  self.write();c=self.nodes['node3'];before=c.journal.state['revision'];cfg=c.config
  restored=m.Policy(cfg,c.api);self.addCleanup(restored.pool.shutdown)
  self.assertEqual(restored.journal.state['revision'],before);self.assertNotIn(b'probe.test',restored.journal.path.read_bytes())
 def test_pending_operation_returns_accepted_and_identical_retry_is_not_executed_twice(self):
  c=self.nodes['node1'];c.config['write_wait']=0
  started,release=threading.Event(),threading.Event();old=self.mutate
  def block(node,route,body):
   if node=='node1':started.set();release.wait(3)
   return old(node,route,body)
  with patch.object(self,'mutate',side_effect=block):
   reply=self.write();self.assertEqual(reply['status'],202);self.assertTrue(started.wait(1))
   reply=self.write();self.assertEqual(reply['status'],202)
   with self.assertRaises(m.Failure):self.write('stats/config/update',{'enabled':True})
   release.set();self.assertTrue(c.job['done'].wait(3))
  self.assertEqual(len([x for x in self.calls if x==('node1','querylog/config/update')]),1)
 def test_accepted_proposal_survives_proposer_failure(self):
  wanted={'policy':initial(),'refresh':None};wanted['policy']['stats/config']['ignored']=['retained.test']
  for n in ('node1','node2'):
   j=self.nodes[n].journal;j.promise(1,[1,'node1']);j.accept(1,[1,'node1'],wanted)
  with self.assertRaises(m.Failure):self.nodes['node3'].choose({'policy':initial(),'refresh':None})
  self.nodes['node3'].converge()
  self.assertTrue(all(v['stats/config']['ignored']==['retained.test'] for v in self.data.values()))
if __name__=='__main__':unittest.main()
