import hashlib,json,pathlib,sys,tempfile
from cryptography.fernet import Fernet
sys.path.insert(0,'/usr/local/libexec/desk-homeassistant')
from snapshot import unpack
root=pathlib.Path('/var/lib/desk-homeassistant');c=json.loads(pathlib.Path('/etc/desk-homeassistant/config.json').read_bytes())
state=json.loads(Fernet(c['encryption_key']).decrypt((root/'journal.bin').read_bytes()));value=state['value'];blob=(root/'blobs'/value['digest']).read_bytes()
if hashlib.sha256(blob).hexdigest()!=value['digest']:raise SystemExit('Bundle digest mismatch')
with tempfile.TemporaryDirectory(prefix='camera-restore-',dir='/var/lib/desk-camera') as d:
 p=pathlib.Path(d);manifest=unpack(blob,c['encryption_key'],c['image'],p)
 entries=json.loads((p/'.storage/core.config_entries').read_bytes())['data']['entries'];domains={e['domain'] for e in entries}
 assert {'tapo_control','webrtc'}<=domains and 'desk_remote' not in domains
 assert (p/'.storage/desk_camera_migration').is_file()
 assert (p/'.storage/desk_camera_additions').is_file()
 cameras=[e for e in entries if e['domain']=='tapo_control']
 assert {e['data']['ip_address'] for e in cameras}=={'192.168.1.160','192.168.1.161','192.168.1.163'}
 devices=json.loads((p/'.storage/core.device_registry').read_bytes())['data']['devices'];device_ids={d['id'] for d in devices}
 entities=json.loads((p/'.storage/core.entity_registry').read_bytes())['data']['entities'];camera_ids={e['entry_id'] for e in cameras}
 assert all(not e.get('device_id') or e['device_id'] in device_ids for e in entities if e.get('config_entry_id') in camera_ids)
 assert not any(name.startswith('www/tapo_control/') or name.startswith('media/') for name in manifest['files'])
 assert not (p/'custom_components/desk_remote').exists()
 for domain in ('tapo_control','webrtc'):assert (p/'custom_components'/domain/'manifest.json').is_file()
 report={'node':c['node'],'revision':state['revision'],'digest':value['digest'],'bytes':len(blob),'files':len(manifest['files']),'native_camera_entries':len(cameras),'webrtc_entries':sum(e['domain']=='webrtc' for e in entries),'camera_registry_links':'PASS','retired_proxy_entries':0,'cache_in_bundle':False,'sqlite_and_checksums':'PASS'}
 status=json.loads(pathlib.Path('/run/desk-homeassistant/public.json').read_bytes())
 if status['state']=='active':
  for name in ('.storage/auth','.storage/auth_provider.homeassistant'):
   assert (p/name).read_bytes()==(root/'current'/name).read_bytes()
  report['active_auth_preserved']=True
path=pathlib.Path('/var/lib/desk-camera/bundle-validation.json');path.write_text(json.dumps(report));path.chmod(0o600);print(json.dumps(report))
