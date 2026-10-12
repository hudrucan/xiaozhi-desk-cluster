import copy, importlib.util, json, tempfile, threading, unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch,MagicMock
from cryptography.fernet import Fernet
spec=importlib.util.spec_from_file_location('config_sync',Path(__file__).resolve().parents[1]/'files/config_sync.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def seed():
 value={'collections':{name:{} for name in m.COLLECTIONS},'settings':{'batch':{'enabled':True,'timeout':30},'meta':{'appName':'Beszel'},'smtp':{'enabled':False}}}
 value['collections']['users']['aaaaaaaaaaaaaaa']={'email':'admin@desk.cluster','role':'admin','name':'Admin'}
 value['collections']['_superusers']['bbbbbbbbbbbbbbb']={'email':'admin@desk.cluster'}
 value['collections']['systems']['ccccccccccccccc']={'name':'deskb1x','host':'10.10.10.11','port':'45876','users':['aaaaaaaaaaaaaaa'],'paused':False}
 value['collections']['user_settings']['ddddddddddddddd']={'user':'aaaaaaaaaaaaaaa','settings':{'chartTime':'1h','theme':'dark'}}
 return value

class Hub:
 def __init__(self,value,suffix):
  self.settings=copy.deepcopy(value['settings']);self.passwords={};self.calls=[];self.fail=None
  self.rows={name:{} for name in m.COLLECTIONS}
  mapping={name:{key:key[:-1]+suffix for key in rows} for name,rows in value['collections'].items()}
  for name,rows in value['collections'].items():
   for key,row in rows.items():
    item=m.translate_refs(name,row,mapping)
    if name=='systems':item['status']='paused' if item.pop('paused') else 'up'
    self.rows[name][mapping[name][key]]={'id':mapping[name][key],**item}
 def health(self):return True
 def records(self,name):return copy.deepcopy(list(self.rows[name].values()))
 def full_settings(self):return copy.deepcopy(self.settings)
 def request(self,path,method='GET',body=None,token=None):
  self.calls.append((path,method))
  if method!='GET' and self.fail:raise m.Failure(self.fail,'native test failure')
  if path=='/api/settings':
   if method=='PATCH':self.settings=copy.deepcopy(body)
   return copy.deepcopy(self.settings)
  if path=='/api/batch':
   for request in body['requests']:
    pieces=request['url'].split('/');name=pieces[3];identity=pieces[5] if len(pieces)>5 else request['body']['id']
    if request['method']=='DELETE':self.rows[name].pop(identity,None);continue
    item=copy.deepcopy(request['body']);password=item.pop('password',None);item.pop('passwordConfirm',None)
    if password:self.passwords[(name,identity)]=password
    if name=='network_monitors':
     # Reproduce Beszel's native create/update hook changing the supplied ID.
     complete={**self.rows[name].get(identity,{}),**item}
     native=__import__('hashlib').sha256((complete['system']+complete['target']+complete['protocol']+str(complete.get('port',0))+complete.get('server','')).encode()).hexdigest()[:8]
     self.rows[name].pop(identity,None);identity=native
    self.rows[name][identity]={**self.rows[name].get(identity,{}),**item,'id':identity}
   return {'success':True}
  raise AssertionError(path)

class ConfigTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.key=Fernet.generate_key();self.members={f'node{i}':f'10.10.10.{i}' for i in (1,2,3)};self.nodes={};self.offline=set()
  for index,node in enumerate(self.members,1):
   journal=m.Journal(Path(self.tmp.name)/node,Fernet(self.key),node,self.members,m.base64.urlsafe_b64decode(self.key));journal.initialize(seed())
   with patch.object(m.ssl,'create_default_context',return_value=None):
    cluster=m.Cluster({'node':node,'members':self.members,'key':self.key.decode(),'certificate':'none','peer_port':8094},journal,Hub(seed(),str(index)))
   self.nodes[node]=cluster;cluster.apply();self.addCleanup(cluster.pool.shutdown)
  def call(sender,node,action,payload=None):
   if node in self.offline:raise OSError('offline')
   return self.nodes[node].peer(action,payload or {})
  for cluster in self.nodes.values():cluster.call=lambda node,action,payload=None,cluster=cluster:call(cluster,node,action,payload)
  for cluster in self.nodes.values():cluster.refresh()
 def edit(self,node='node1',body=None):
  return self.nodes[node].submit({'collection':'settings','method':'PATCH','body':body or {'meta':{'appName':'Shared'}}})
 def test_cpu_capped_account_batch_has_a_bounded_hashing_deadline(self):
  cluster=self.nodes['node1']
  old=copy.deepcopy(cluster.journal.state['value']);old['settings']['batch']['timeout']=3
  updated=cluster.desired(old,{'collection':'users','method':'PATCH','identity':'aaaaaaaaaaaaaaa','body':{'name':'Changed'}})
  self.assertEqual(updated['settings']['batch']['timeout'],30)
  self.assertEqual(old['settings']['batch']['timeout'],3)
  updated=cluster.desired(updated,{'collection':'settings','method':'PATCH','body':{'batch':{'enabled':False,'timeout':1}}})
  self.assertTrue(updated['settings']['batch']['enabled']);self.assertEqual(updated['settings']['batch']['timeout'],30)
 def test_shared_update_preserves_ids_and_metrics_boundary(self):
  before={name:copy.deepcopy(cluster.journal.state['mapping']) for name,cluster in self.nodes.items()}
  revision,ready=self.edit();self.assertEqual(revision,1);self.assertEqual(len(ready),3)
  for name,cluster in self.nodes.items():
   self.assertEqual(cluster.hub.settings['meta']['appName'],'Shared');self.assertEqual(cluster.journal.state['mapping'],before[name])
   self.assertTrue(all('system_stats' not in path for path,_ in cluster.hub.calls))
 def test_two_nodes_acknowledge_and_recovered_peer_catches_up(self):
  self.offline.add('node3');revision,ready=self.edit();self.assertEqual(revision,1);self.assertEqual(len(ready),2)
  self.offline.clear();self.nodes['node3'].refresh();self.assertEqual(self.nodes['node3'].hub.settings['meta']['appName'],'Shared')
 def test_single_node_never_acknowledges_shared_save(self):
  self.offline.update(('node2','node3'))
  with self.assertRaises(m.Failure) as failure:self.edit()
  self.assertEqual(failure.exception.code,503);self.assertEqual(self.nodes['node1'].journal.state['revision'],0)
 def test_unchanged_polling_does_not_write_the_journal(self):
  paths=[c.journal.path for c in self.nodes.values()];before=[p.stat().st_mtime_ns for p in paths]
  for _ in range(4):
   for cluster in self.nodes.values():cluster.refresh()
  self.assertEqual(before,[p.stat().st_mtime_ns for p in paths])
 def test_concurrent_independent_settings_are_not_lost(self):
  gate=threading.Barrier(2)
  def edit(index):
   gate.wait();return self.edit(f'node{index}',{'meta':{f'field{index}':str(index)}})
  with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(edit,(1,2)))
  for cluster in self.nodes.values():
   cluster.refresh();self.assertEqual(cluster.hub.settings['meta']['field1'],'1');self.assertEqual(cluster.hub.settings['meta']['field2'],'2')
 def test_stale_proposer_rebases_instead_of_replacing_a_new_revision(self):
  old=copy.deepcopy(self.nodes['node1'].journal.state['value']);old['settings']['meta']['appName']='Stale'
  self.edit('node2',{'meta':{'kept':'newer'}})
  complete,revision,_=self.nodes['node1'].choose(old,0)
  self.assertFalse(complete);self.assertEqual(revision,1)
  self.assertEqual(self.nodes['node1'].journal.state['value']['settings']['meta']['kept'],'newer')
 def test_native_validation_rejection_does_not_poison_configuration(self):
  for cluster in self.nodes.values():cluster.hub.fail=400
  with self.assertRaises(m.Failure) as failure:self.edit()
  self.assertEqual(failure.exception.code,400)
  for cluster in self.nodes.values():
   self.assertEqual(cluster.journal.state['value']['settings']['meta']['appName'],'Beszel')
 def test_partial_apply_is_retained_and_retried_not_rolled_back(self):
  self.nodes['node2'].hub.fail=self.nodes['node3'].hub.fail=503
  with self.assertRaises(m.Failure):self.edit()
  self.assertEqual(self.nodes['node1'].journal.state['value']['settings']['meta']['appName'],'Shared')
  for cluster in self.nodes.values():cluster.hub.fail=None;cluster.refresh()
  self.assertTrue(all(c.hub.settings['meta']['appName']=='Shared' for c in self.nodes.values()))
 def test_retry_cannot_acknowledge_a_partial_apply_as_an_unchanged_success(self):
  self.nodes['node2'].hub.fail=self.nodes['node3'].hub.fail=503
  for _ in range(2):
   with self.assertRaises(m.Failure) as failure:self.edit()
   self.assertEqual(failure.exception.code,503)
  self.assertEqual(self.nodes['node1'].journal.state['revision'],1)
 def test_password_update_is_replicated_and_encrypted_at_rest(self):
  password='unique shared test password'
  revision,ready=self.nodes['node1'].submit({'collection':'users','method':'PATCH','identity':'aaaaaaaaaaaaaaa','body':{'password':password,'passwordConfirm':password}})
  self.assertEqual(len(ready),3)
  for cluster in self.nodes.values():
   self.assertIn(password,cluster.hub.passwords.values());self.assertNotIn(password.encode(),cluster.journal.path.read_bytes())
 def test_duplicate_email_is_rejected_before_a_native_account_can_be_overwritten(self):
  before=self.nodes['node1'].journal.state['revision']
  with self.assertRaises(m.Failure) as error:
   self.nodes['node1'].submit({'collection':'users','method':'POST','identity':'eeeeeeeeeeeeeee','body':{'email':'admin@desk.cluster','role':'user','password':'validpassword','passwordConfirm':'validpassword'}})
  self.assertEqual(error.exception.code,409);self.assertEqual(self.nodes['node1'].journal.state['revision'],before)
 def test_additional_superuser_does_not_reuse_the_existing_administrator_id(self):
  revision,ready=self.nodes['node1'].submit({'collection':'_superusers','method':'POST','identity':'eeeeeeeeeeeeeee','body':{'email':'other@desk.cluster','password':'validpassword','passwordConfirm':'validpassword'}})
  self.assertEqual(len(ready),3)
  for cluster in self.nodes.values():
   self.assertEqual(len(cluster.hub.rows['_superusers']),2)
   self.assertEqual(cluster.hub.rows['_superusers'][cluster.journal.state['mapping']['_superusers']['bbbbbbbbbbbbbbb']]['email'],'admin@desk.cluster')
 def test_pocketbase_admin_collection_ids_use_the_same_shared_account_boundary(self):
  cluster=self.nodes['node1'];cluster.hub.collection_name=lambda identity:'_superusers' if identity=='pbc_admin123' else None
  local_id=cluster.journal.state['mapping']['_superusers']['bbbbbbbbbbbbbbb']
  intent=cluster.intent('/api/collections/pbc_admin123/records/'+local_id,'PATCH',{'emailVisibility':True},'token',actor={'collectionName':'_superusers','id':local_id})
  self.assertEqual(intent['collection'],'_superusers');self.assertEqual(intent['identity'],'bbbbbbbbbbbbbbb')
 def test_peer_tls_handshake_is_bounded_before_a_worker_is_created(self):
  server=object.__new__(m.Server);server.peer=True;server.cluster=self.nodes['node1'];server.tls=MagicMock()
  connection=MagicMock();server.tls.wrap_socket.return_value=connection
  with patch.object(m.http.server.HTTPServer,'get_request',return_value=(connection,('10.10.10.2',12345))):
   server.get_request()
  connection.settimeout.assert_called_once_with(3)
  server.tls.wrap_socket.assert_called_once_with(connection,server_side=True)
 def test_wrong_peer_is_closed_before_a_tls_handshake(self):
  server=object.__new__(m.Server);server.peer=True;server.cluster=self.nodes['node1'];server.tls=MagicMock();connection=MagicMock()
  with patch.object(m.http.server.HTTPServer,'get_request',return_value=(connection,('192.168.1.20',12345))):
   with self.assertRaises(OSError):server.get_request()
  connection.close.assert_called_once();server.tls.wrap_socket.assert_not_called()
 def test_new_ids_follow_pinned_native_collection_lengths(self):
  for collection,size in (('users',15),('quiet_hours',10),('network_monitors',10),('universal_tokens',10),('fingerprints',9)):
   self.assertEqual(len(m.record_identity(collection)),size)
  self.assertIsNotNone(m.RECORD_ROUTE.fullmatch('/api/collections/quiet_hours/records/abcdefghij'))
 def test_deleted_record_mappings_do_not_accumulate_in_checkpoints(self):
  cluster=self.nodes['node1']
  cluster.submit({'collection':'users','method':'POST','identity':'eeeeeeeeeeeeeee','body':{'email':'temp@desk.cluster','role':'user','password':'validpassword','passwordConfirm':'validpassword'}})
  cluster.submit({'collection':'users','method':'DELETE','identity':'eeeeeeeeeeeeeee','body':{}})
  for node in self.nodes.values():self.assertNotIn('eeeeeeeeeeeeeee',node.journal.state['mapping']['users'])
 def test_system_delete_removes_dependent_configuration_before_native_cascade(self):
  value=seed();value['collections']['alerts']['eeeeeeeeeeeeeee']={'user':'aaaaaaaaaaaaaaa','system':'ccccccccccccccc','name':'CPU','min':5,'value':80}
  updated=m.mutation(value,'systems','ccccccccccccccc','DELETE',{})
  self.assertEqual(updated['collections']['alerts'],{});self.assertEqual(updated['collections']['systems'],{})
 def test_native_monitor_hook_ids_are_mapped_after_create_update_and_delete(self):
  cluster=self.nodes['node1'];key='eeeeeeeeee'
  intent={'collection':'network_monitors','method':'POST','identity':key,'body':{'system':'ccccccccccccccc','target':'example.invalid','protocol':'icmp','port':443,'server':'unused','interval':60,'enabled':False}}
  _,ready=cluster.submit(intent);self.assertEqual(len(ready),3)
  original={name:node.journal.state['mapping']['network_monitors'][key] for name,node in self.nodes.items()}
  for name,node in self.nodes.items():
   local_id=original[name];self.assertEqual(len(local_id),8);self.assertIn(local_id,node.hub.rows['network_monitors'])
   row=node.journal.state['value']['collections']['network_monitors'][key];self.assertEqual(row['port'],0);self.assertEqual(row['server'],'')
  _,ready=self.nodes['node2'].submit({'collection':'network_monitors','method':'PATCH','identity':key,'body':{'target':'changed.invalid'}})
  self.assertEqual(len(ready),3)
  for name,node in self.nodes.items():
   local_id=node.journal.state['mapping']['network_monitors'][key];self.assertNotEqual(local_id,original[name]);self.assertNotIn(original[name],node.hub.rows['network_monitors']);self.assertIn(local_id,node.hub.rows['network_monitors'])
  self.nodes['node3'].submit({'collection':'network_monitors','method':'DELETE','identity':key,'body':{}})
  for node in self.nodes.values():self.assertEqual(node.hub.rows['network_monitors'],{});self.assertEqual(node.journal.state['mapping']['network_monitors'],{})
 def test_missing_monitor_mapping_recovers_existing_native_record_without_recreate(self):
  cluster=self.nodes['node1'];value=copy.deepcopy(cluster.journal.state['value']);key='eeeeeeeeee'
  row={'system':'ccccccccccccccc','target':'example.invalid','protocol':'dns','port':0,'server':'1.1.1.1','interval':60,'enabled':False}
  value['collections']['network_monitors'][key]=row
  local={name:cluster.hub.records(name) for name in m.COLLECTIONS};mapping=copy.deepcopy(cluster.journal.state['mapping']);mapping['network_monitors'][key]=key
  local['network_monitors']=[{'id':'12345678',**m.translate_refs('network_monitors',row,mapping)}]
  rebound=m.match_mapping(value,local,mapping);self.assertEqual(rebound['network_monitors'][key],'12345678')
  self.assertEqual(m.plan_apply(value,value,rebound,local),[])
 def test_duplicate_monitor_configuration_is_rejected_before_commit(self):
  cluster=self.nodes['node1'];body={'system':'ccccccccccccccc','target':'example.invalid','protocol':'icmp','interval':60,'enabled':False}
  cluster.submit({'collection':'network_monitors','method':'POST','identity':'eeeeeeeeee','body':body})
  with self.assertRaises(m.Failure) as error:cluster.submit({'collection':'network_monitors','method':'POST','identity':'ffffffffff','body':body})
  self.assertEqual(error.exception.code,409)
 def test_network_monitor_server_is_plain_text_not_a_system_relation(self):
  value=m.translate_refs('network_monitors',{'system':'ccccccccccccccc','server':'dns.example.net'},self.nodes['node1'].journal.state['mapping'])
  self.assertEqual(value['server'],'dns.example.net')
 def test_unconfirmed_certificate_cannot_commit(self):
  cluster=self.nodes['node1'];wanted=seed();wanted['settings']['meta']['appName']='bad'
  receipt=cluster.journal.accept(1,[1,'node1'],wanted)
  with self.assertRaises(m.Failure):cluster.journal.commit(1,wanted,[receipt])
  self.assertEqual(cluster.journal.state['revision'],0)
 def test_accepted_proposal_survives_restart_and_is_recovered(self):
  wanted=seed();wanted['settings']['meta']['pending']='retained'
  for node in ('node1','node2'):
   journal=self.nodes[node].journal;journal.promise(1,[1,'node1']);journal.accept(1,[1,'node1'],wanted)
  node=self.nodes['node1'];node.journal=m.Journal(node.journal.path,Fernet(self.key),'node1',self.members,m.base64.urlsafe_b64decode(self.key))
  self.edit('node3',{'meta':{'subsequent':'next'}})
  for cluster in self.nodes.values():
   cluster.refresh();self.assertEqual(cluster.hub.settings['meta']['pending'],'retained');self.assertEqual(cluster.hub.settings['meta']['subsequent'],'next')
if __name__=='__main__':unittest.main()
