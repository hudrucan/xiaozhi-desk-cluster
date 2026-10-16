"""Export only the successfully validated camera entry, not canary auth or config."""
import datetime,json,pathlib,secrets,urllib.parse
root=pathlib.Path('/var/lib/desk-camera');seed=root/'seed/config/.storage'
report=json.loads((root/'seed/config/report.json').read_bytes())
if not report.get('stopped_cleanly') or report.get('entry_state')!='loaded':raise SystemExit('Clean validated seed required')
c=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes())
store=json.loads((seed/'core.config_entries').read_bytes());entries=[e for e in store['data']['entries'] if e['domain']=='tapo_control']
if len(entries)!=1:raise SystemExit('Exactly one camera required')
entry=entries[0];relay=c['relay'];auth=urllib.parse.quote(relay['user'],safe='')+':'+urllib.parse.quote(relay['password'],safe='')
entry['data'].update({'custom_stream_hd':'rtsp://'+auth+'@192.168.1.186:18556/bed_hd','custom_stream_sd':'rtsp://'+auth+'@192.168.1.186:18556/bed_sd','media_sync_cold_storage_path':'/media/tapo_control/bed'})
stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
webrtc=dict(entry,entry_id=secrets.token_hex(13).upper(),domain='webrtc',title='WebRTC Camera',unique_id=None,version=1,minor_version=1,data={'url':'http://'+auth+'@192.168.1.186:1986/'},options={},created_at=stamp,modified_at=stamp)
entities=json.loads((seed/'core.entity_registry').read_bytes())['data']['entities'];entities=[e for e in entities if e.get('config_entry_id')==entry['entry_id']]
devices=json.loads((seed/'core.device_registry').read_bytes())['data']['devices'];devices=[e for e in devices if e.get('config_entry_id')==entry['entry_id'] or entry['entry_id'] in e.get('config_entries',[])]
out={'version':1,'entries':[entry,webrtc],'entities':entities,'devices':devices}
p=root/'core-migration.json';p.write_text(json.dumps(out));p.chmod(0o600)
print(json.dumps({'entries':2,'camera_entities':sum(e['entity_id'].startswith('camera.') for e in entities),'proxy_entities':0}))
