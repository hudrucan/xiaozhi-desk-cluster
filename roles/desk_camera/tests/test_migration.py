import importlib.util,json,pathlib,sys,tempfile,types,unittest
ROOT=pathlib.Path(__file__).parent
if not (ROOT/'camera_config.py').exists():ROOT=ROOT.parent/'files'
def atomic(p,b):p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
sys.modules['config_journal']=types.SimpleNamespace(atomic=atomic,encoded=lambda v:json.dumps(v).encode())
spec=importlib.util.spec_from_file_location('camera_config',ROOT/'camera_config.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Migration(unittest.TestCase):
 def test_preserves_unrelated_entries_unicode_yaml_and_empty_devices(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d)/'config';storage=root/'.storage';storage.mkdir(parents=True)
   source=pathlib.Path(d)/'vendor';m.SOURCE=source;m.BACKUP=pathlib.Path(d)/'backup'
   for domain in ('tapo_control','webrtc'):
    p=source/'custom_components'/domain;p.mkdir(parents=True);(p/'manifest.json').write_text('{}')
   migration={'version':1,'entries':[{'domain':'tapo_control','entry_id':'newcam'},{'domain':'webrtc','entry_id':'video'}],'entities':[{'entity_id':'camera.bed','config_entry_id':'newcam'}],'devices':[{'id':'cam','config_entries':['newcam']}]}
   m.MIGRATION=pathlib.Path(d)/'migration.json';m.MIGRATION.write_text(json.dumps(migration))
   (root/'configuration.yaml').write_text('# Tiếng Việt\ndefault_config: {}\ndesk_remote: {}\n')
   atomic(storage/'core.config_entries',json.dumps({'data':{'entries':[{'domain':'desk_remote','entry_id':'old'},{'domain':'input_boolean','entry_id':'keep'}]}}).encode())
   atomic(storage/'core.entity_registry',json.dumps({'data':{'entities':[{'entity_id':'switch.old','config_entry_id':'old'},{'entity_id':'input_boolean.keep','config_entry_id':'keep'}]}}).encode())
   atomic(storage/'core.device_registry',json.dumps({'data':{'devices':[{'id':'old','config_entries':['old']},{'id':'mixed','config_entries':['old','keep']},{'id':'empty','config_entries':[]}]}}).encode())
   m.configure(root)
   self.assertEqual((root/'configuration.yaml').read_text(),'# Tiếng Việt\ndefault_config: {}\n\n')
   entries=json.loads((storage/'core.config_entries').read_bytes())['data']['entries'];self.assertEqual({e['entry_id'] for e in entries},{'keep','newcam','video'})
   devices=json.loads((storage/'core.device_registry').read_bytes())['data']['devices'];self.assertEqual({e['id'] for e in devices},{'mixed','empty','cam'})
   self.assertEqual(next(e for e in devices if e['id']=='mixed')['config_entries'],['keep'])
   before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()};m.configure(root)
   self.assertEqual(before,{p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()})
 def test_additions_preflight_atomic_scope_and_idempotence(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d)/'config';storage=root/'.storage';storage.mkdir(parents=True);m.BACKUP=pathlib.Path(d)/'backup'
   m.ADDITIONS=pathlib.Path(d)/'additions.json'
   data={'version':2,'entries':[{'domain':'tapo_control','entry_id':'cam'+str(i),'data':{'ip_address':'192.168.1.'+str(i)}} for i in (161,163)],'entities':[{'entity_id':'camera.cam'+str(i),'config_entry_id':'cam'+str(i)} for i in (161,163)],'devices':[{'id':'native161','config_entry_id':'cam161'}],'cards':[{'type':'custom:webrtc-camera','streams':[{'url':'cam'+str(i)+'_hd'}]} for i in (161,163)]}
   data['entities'][0]['device_id']='native161'
   m.ADDITIONS.write_text(json.dumps(data))
   for name,key,items in [('core.config_entries','entries',[{'domain':'tapo_control','entry_id':'old','data':{'ip_address':'192.168.1.160'}}]),('core.entity_registry','entities',[{'entity_id':'camera.cam161','config_entry_id':'user'}]),('core.device_registry','devices',[])]:
    atomic(storage/name,json.dumps({'data':{key:items}}).encode())
   atomic(storage/'lovelace.desk_cameras',json.dumps({'data':{'config':{'views':[{'path':'cameras','cards':[{'type':'user_custom','x':'preserve'},{'type':'button','name':'Recordings'}]}]}}}).encode())
   before={p.name:p.read_bytes() for p in storage.iterdir()}
   with self.assertRaises(ValueError):m.add_cameras(root)
   self.assertEqual(before,{p.name:p.read_bytes() for p in storage.iterdir()})
   reg=json.loads((storage/'core.entity_registry').read_bytes());reg['data']['entities'][0]['entity_id']='camera.original';atomic(storage/'core.entity_registry',json.dumps(reg).encode())
   m.add_cameras(root)
   entries=json.loads((storage/'core.config_entries').read_bytes())['data']['entries'];self.assertEqual({e['entry_id'] for e in entries},{'old','cam161','cam163'})
   cards=json.loads((storage/'lovelace.desk_cameras').read_bytes())['data']['config']['views'][0]['cards'];self.assertEqual(cards[0],{'type':'user_custom','x':'preserve'});self.assertEqual(cards[-1]['name'],'Recordings');self.assertEqual(len(cards),4)
   after={p.name:p.read_bytes() for p in storage.iterdir()};m.add_cameras(root);self.assertEqual(after,{p.name:p.read_bytes() for p in storage.iterdir()})
if __name__=='__main__':unittest.main()
