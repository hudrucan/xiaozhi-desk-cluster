"""Export only native camera entries from the clean isolated validation runtime."""
import json,pathlib,urllib.parse
root=pathlib.Path('/var/lib/desk-camera');seed=root/'seed-two/config';report=json.loads((seed/'report.json').read_bytes())
if not report.get('stopped_cleanly') or len(report.get('cameras',[]))!=2 or any(c.get('state')!='loaded' for c in report['cameras']):raise SystemExit('Clean validated camera entries required')
c=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes());auth=urllib.parse.quote(c['relay']['user'],safe='')+':'+urllib.parse.quote(c['relay']['password'],safe='')
entries=json.loads((seed/'.storage/core.config_entries').read_bytes())['data']['entries'];entries=[e for e in entries if e['domain']=='tapo_control']
if {e['data']['ip_address'] for e in entries}!={'192.168.1.161','192.168.1.163'}:raise SystemExit('Unexpected camera entry scope')
new_ids={e['entry_id'] for e in entries}
entities=[e for e in json.loads((seed/'.storage/core.entity_registry').read_bytes())['data']['entities'] if e.get('config_entry_id') in new_ids]
devices=[e for e in json.loads((seed/'.storage/core.device_registry').read_bytes())['data']['devices'] if e.get('config_entry_id') in new_ids or new_ids.intersection(e.get('config_entries',[]))]
cards=[];safe=[]
for e in entries:
 suffix=int(e['data']['ip_address'].split('.')[-1]);name='cam'+str(suffix);port={161:18557,163:18558}[suffix]
 device=next((d for d in devices if d.get('config_entry_id')==e['entry_id'] or e['entry_id'] in d.get('config_entries',[])),{})
 title=device.get('name_by_user') or device.get('name') or 'Camera .'+str(suffix)
 e['title']=title
 e['data'].update({'custom_stream_hd':'rtsp://'+auth+'@192.168.1.186:'+str(port)+'/'+name+'_hd','custom_stream_sd':'rtsp://'+auth+'@192.168.1.186:'+str(port)+'/'+name+'_sd','media_sync_cold_storage_path':'/media/tapo_control/'+name})
 cards.append({'type':'custom:webrtc-camera','title':title,'ui':True,'mode':'webrtc,mse','streams':[{'url':name+'_hd','name':'HD'},{'url':name+'_sd','name':'SD'}]})
 safe.append({'host':e['data']['ip_address'],'title':title,'camera_entities':[x['entity_id'] for x in entities if x.get('config_entry_id')==e['entry_id'] and x['entity_id'].startswith('camera.')]})
out={'version':2,'entries':entries,'entities':entities,'devices':devices,'cards':cards};p=root/'core-additions.json';p.write_text(json.dumps(out));p.chmod(0o600)
print(json.dumps({'entries':len(entries),'devices':len(devices),'cameras':safe}))
