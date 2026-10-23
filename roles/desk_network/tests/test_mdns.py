"""Protocol fixtures and inventory/cache isolation; no network or live services."""
import importlib.util,json,pathlib,sqlite3,struct,sys,tempfile,time,types,unittest
from datetime import datetime, timezone
from contextlib import closing
from unittest.mock import patch
import dns.message,dns.name,dns.rrset,dns.reversename,dns.wire
spec=importlib.util.spec_from_file_location('mdns',pathlib.Path(__file__).resolve().parents[1]/'files/mdns.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
MAC='02:11:22:33:44:55';IP='192.168.1.115'
class MDNS(unittest.TestCase):
 def packet(self,ip=IP,name='zerow.local',ttl=120,forward=True):
  q=dns.message.make_query(dns.reversename.from_address(ip),'PTR');q.id=42
  r=dns.message.make_response(q)
  r.answer.append(dns.rrset.from_text(str(q.question[0].name),ttl,'IN','PTR',name+'.'))
  if forward:r.additional.append(dns.rrset.from_text(name+'.',ttl,'IN','A',ip))
  return r.to_wire()
 def fixture(self,root):
  p=root/'app.db'
  with closing(sqlite3.connect(p)) as c:
   c.execute('CREATE TABLE Devices(devMac TEXT,devLastIP TEXT,devName TEXT,devNameSource TEXT,devPresentLastScan INTEGER,devIsArchived INTEGER)')
   c.execute('INSERT INTO Devices VALUES(?,?,?,?,?,?)',(MAC,IP,'(name not found)','NEWDEV',1,0));c.commit()
  return p
 def parse(self,raw,source=(IP,5353),requests=None):
  ptr,forward={},{}
  m.answers(raw,source,{IP:MAC},requests or {42:(IP,None)},ptr,forward)
  return ptr,forward
 def test_native_policy_uses_mac_identity_and_tracks_name_changes(self):
  root=pathlib.Path(__file__).resolve().parents[1]
  policy=json.loads((root/'files/policy.json').read_text())
  spec=importlib.util.spec_from_file_location('mdns_ha',root/'files/ha.py');ha=importlib.util.module_from_spec(spec);spec.loader.exec_module(ha)
  self.assertEqual(ha.POLICY,policy)
  self.assertFalse(policy['NEWDEV_IP_MATCH_NAME'])
  self.assertEqual(policy['AVAHISCAN_SET_ALWAYS'],[])
  self.assertEqual(policy['AVAHISCAN_SET_EMPTY'],['devName','devFQDN'])
  self.assertIn('watchedValue2',policy['AVAHISCAN_WATCH'])
 def test_ptr_and_forward_pair_and_cache_flush(self):
  raw=bytearray(self.packet());p=dns.wire.Parser(bytes(raw),12);p.get_name();p.get_bytes(4)
  for _ in range(2):
   p.get_name();off=p.current;_,cls,_,size=p.get_struct('!HHIH');struct.pack_into('!H',raw,off+2,cls|0x8000);p.get_bytes(size)
  ptr,a=self.parse(bytes(raw));self.assertEqual(ptr[IP]['zerow.local'],120);self.assertEqual(a[(IP,'zerow.local')],120)
 def test_spoofed_wrong_network_port_id_question_ttl_and_malformed(self):
  for raw,source,ids in [(self.packet(),('10.10.10.11',5353),None),(self.packet(),(IP,53),None),(self.packet(),(IP,5353),{41:(IP,None)}),(self.packet(ip='192.168.1.28'),(IP,5353),None),(self.packet(ttl=0),(IP,5353),None),(b'\x00'*12,(IP,5353),None),(b'\0'*12+b'\xc0\x0c',(IP,5353),None)]:
   self.assertEqual(self.parse(raw,source,ids)[0],{})
  r=dns.message.from_wire(self.packet());r.flags|=dns.flags.TC;self.assertEqual(self.parse(r.to_wire()),({},{}))
 def test_resolve_requires_matching_forward_and_rejects_ambiguity(self):
  for mode,expected in [('valid',True),('missing-forward',False),('ambiguous',False)]:
   queue=[]
   class WireSocket:
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def bind(self,*args):pass
    def setsockopt(self,*args):pass
    def settimeout(self,*args):pass
    def sendto(self,raw,destination):
     q=dns.message.from_wire(raw);r=dns.message.make_response(q)
     if q.question[0].rdtype==12:
      r.answer.append(dns.rrset.from_text(str(q.question[0].name),120,'IN','PTR','zerow.local.'))
      if mode=='ambiguous':r.answer.append(dns.rrset.from_text(str(q.question[0].name),120,'IN','PTR','other.local.'))
     elif mode!='missing-forward':r.answer.append(dns.rrset.from_text(str(q.question[0].name),120,'IN','A',IP))
     queue.append(r.to_wire())
    def recvfrom(self,*args):
     if not queue:raise m.socket.timeout()
     return queue.pop(0),(IP,5353)
   with patch.object(m.socket,'socket',return_value=WireSocket()):result,stats=m.resolve({IP:MAC},'192.168.1.101')
   self.assertEqual(bool(result),expected);self.assertLessEqual(stats['sent'],6)
 def test_names_are_host_labels_not_instance_or_result_injection(self):
  for value in ('zerow.local','esp_32.local','A-b.local'):self.assertEqual(m.hostname(dns.name.from_text(value)),value)
  for value in ('host.example.com','bad|host.local',r'bad\010name.local','service name.local','-bad.local','local'):
   self.assertIsNone(m.hostname(dns.name.from_text(value)))
 def test_inventory_excludes_protected_offline_archived_nonlan_and_conflict(self):
  with tempfile.TemporaryDirectory() as td:
   p=self.fixture(pathlib.Path(td))
   with closing(sqlite3.connect(p)) as c:
    for i,values in enumerate([('192.168.1.1','','USER',1,0),('192.168.1.2','','LOCKED',1,0),('192.168.1.3','named','',1,0),('192.168.1.4','','',0,0),('192.168.1.5','','',1,1),('10.77.0.2','','',1,0),('192.168.1.255','','',1,0),('192.168.1.6','','',1,0)]):
     c.execute('INSERT INTO Devices VALUES(?,?,?,?,?,?)',('02:11:22:33:44:%02x'%(i+60),*values))
    c.execute('INSERT INTO Devices VALUES(?,?,?,?,?,?)',('02:11:22:33:44:99','192.168.1.6','','',1,0));c.commit()
   self.assertEqual(m.candidates(p),{IP:MAC})
 def test_cache_negative_positive_expiry_and_mac_ip_change(self):
  with tempfile.TemporaryDirectory() as td,patch.object(m,'interface_address',return_value='192.168.1.101'):
   root=pathlib.Path(td);db=self.fixture(root);cache=root/'cache.json';now=time.time()
   lookup=lambda targets,address:({IP:('zerow.local',120)},{'sent':4,'received':2})
   names,stats=m.run(db,cache,lookup);self.assertEqual(names,[(MAC,IP,'zerow.local')])
   names,stats=m.run(db,cache,lambda *a:self.fail('Positive cache re-queried'));self.assertEqual(stats['queried'],0)
   cache.write_text(json.dumps({MAC+'/'+IP:{'name':'','until':now+600}}))
   self.assertEqual(m.run(db,cache,lambda *a:self.fail('Negative cache re-queried'))[0],[])
   cache.write_text(json.dumps({MAC+'/'+IP:{'name':'','until':now-1}}))
   self.assertEqual(m.run(db,cache,lookup)[0][0][2],'zerow.local')
   with closing(sqlite3.connect(db)) as c:c.execute('UPDATE Devices SET devLastIP=?',('192.168.1.28',));c.commit()
   seen=[]
   def empty(targets,address):seen.extend(targets);return {},{'sent':2,'received':0}
   self.assertEqual(m.run(db,cache,empty)[0],[]);self.assertEqual(seen,['192.168.1.28'])
 def test_manual_name_or_identity_change_during_lookup_discards_result(self):
  with tempfile.TemporaryDirectory() as td,patch.object(m,'interface_address',return_value='192.168.1.101'):
   root=pathlib.Path(td);db=self.fixture(root)
   def changed(targets,address):
    with closing(sqlite3.connect(db)) as c:c.execute("UPDATE Devices SET devName='my-pi',devNameSource='USER'");c.commit()
    return {IP:('zerow.local',120)},{'sent':4,'received':2}
   self.assertEqual(m.run(db,root/'cache',changed)[0],[])
 def test_cache_invalid_and_future_data_ignored(self):
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td)/'cache';p.write_text('not-json');self.assertEqual(m.read_cache(p,100),{})
   p.write_text(json.dumps({'key':{'name':'bad|name.local','until':200},'future':{'name':'host.local','until':701}}));self.assertEqual(m.read_cache(p,100),{})
 def test_batch_bound_and_empty_batch_never_opens_socket(self):
  with tempfile.TemporaryDirectory() as td,patch.object(m,'interface_address',return_value='192.168.1.101'):
   root=pathlib.Path(td);db=self.fixture(root)
   with closing(sqlite3.connect(db)) as c:
    for i in range(1,64):c.execute('INSERT INTO Devices VALUES(?,?,?,?,?,?)',('02:11:22:33:45:%02x'%i,'192.168.1.%s'%i,'','',1,0))
    c.commit()
   sizes=[]
   def empty(targets,address):sizes.append(len(targets));return {},{'sent':0,'received':0}
   m.run(db,root/'cache',empty);m.run(db,root/'cache',empty)
   self.assertEqual(sizes,[32,32])
   with patch.object(m.socket,'socket',side_effect=AssertionError('socket opened')):self.assertEqual(m.resolve({},IP)[0],{})
class Adapter(unittest.TestCase):
 def test_result_format_is_13_columns_with_utc_and_empty_results_clear(self):
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td)/'result';stamp=datetime(2026,10,23,13,0,0,tzinfo=timezone.utc)
   with patch.object(m,'datetime') as clock:
    clock.now.return_value=stamp
    m.write_plugin_results(p,[(MAC,IP,'','zerow.local','','','',MAC)])
    clock.now.assert_called_once_with(timezone.utc)
   self.assertEqual(p.read_text(),f'{MAC}|{IP}|2026-10-23 13:00:00||zerow.local||||{MAC}||||\n')
   self.assertEqual(len(p.read_text().strip().split('|')),13)
   m.write_plugin_results(p,[]);self.assertEqual(p.read_text(),'')
 def test_invalid_row_cannot_publish_partial_results(self):
  row=(MAC,IP,'','zerow.local','','','',MAC)
  with tempfile.TemporaryDirectory() as td:
   p=pathlib.Path(td)/'result';p.write_text('unchanged')
   for bad in [row[:-1],(*row, ''),(*row[:3],'bad|name',*row[4:]),(*row[:3],'bad\nname',*row[4:]),(*row[:3],'bad\rname',*row[4:]),(*row[:3],None,*row[4:])]:
    with self.subTest(row=bad),self.assertRaises(ValueError):m.write_plugin_results(p,[row,bad])
    self.assertEqual(p.read_text(),'unchanged')
 def test_mdns_main_uses_native_columns_without_plugin_helper_and_clears_on_failure(self):
  with tempfile.TemporaryDirectory() as td:
   root=pathlib.Path(td);(root/'plugins').mkdir();p=root/'plugins/last_result.AVAHISCAN.log'
   with patch.dict(sys.modules,{'plugin_helper':None,'const':types.SimpleNamespace(logPath=td)}),patch.object(sys,'path',sys.path.copy()),patch.object(m,'save_json'):
    with patch.object(m,'run',return_value=([(MAC,IP,'zerow.local')],{})):
     self.assertEqual(m.main(),0)
    fields=p.read_text().strip().split('|')
    self.assertEqual(len(fields),13);self.assertEqual(fields[:2],[MAC,IP]);self.assertEqual(fields[4],'zerow.local');self.assertEqual(fields[8],MAC)
    with patch.object(m,'run',side_effect=OSError('worker unavailable')):
     self.assertEqual(m.main(),0)
    self.assertEqual(p.read_text(),'')

if __name__=='__main__':unittest.main()
