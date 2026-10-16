"""Install unmodified camera components and migrate only a stopped HA config."""
import json,pathlib,re,secrets,shutil,sys
from config_journal import atomic,encoded
import yaml
SOURCE=pathlib.Path('/usr/local/libexec/desk-camera/components')
MIGRATION=pathlib.Path('/etc/desk-camera/core-migration.json')
BACKUP=pathlib.Path('/var/lib/desk-camera/core-before-migration')

def configure(root):
 if not MIGRATION.is_file():return
 root=pathlib.Path(root)
 for domain in ('tapo_control','webrtc'):
  source=SOURCE/'custom_components'/domain;target=root/'custom_components'/domain
  if target.is_symlink():raise ValueError('Invalid camera component target')
  target.mkdir(parents=True,exist_ok=True,mode=0o700)
  for p in source.rglob('*'):
   if p.is_symlink():raise ValueError('Invalid vendor source')
   rel=p.relative_to(source);dest=target/rel
   if p.is_dir():dest.mkdir(exist_ok=True,mode=0o700)
   elif p.is_file():atomic(dest,p.read_bytes())
 marker=root/'.storage/desk_camera_migration'
 if marker.exists():
  add_cameras(root);upgrade_cards(root);return
 migration=json.loads(MIGRATION.read_bytes())
 if migration.get('version')!=1:raise ValueError('Unknown camera migration')
 path=root/'.storage/core.config_entries';entries=json.loads(path.read_bytes());existing=entries['data']['entries']
 if any(e['domain'] in ('tapo_control','webrtc') for e in existing):raise ValueError('Existing camera entries require manual review')
 old={e['entry_id'] for e in existing if e['domain']=='desk_remote'}
 selected=migration['entries']
 if sorted(e['domain'] for e in selected)!=['tapo_control','webrtc']:raise ValueError('Invalid migration scope')
 new_ids={e['entry_id'] for e in selected}
 if len(new_ids)!=2 or new_ids & {e['entry_id'] for e in existing}:raise ValueError('Entry identity conflict')
 backup=BACKUP;backup.mkdir(mode=0o700,exist_ok=True)
 for name in ('configuration.yaml','.storage/core.config_entries','.storage/core.entity_registry','.storage/core.device_registry','.storage/lovelace_dashboards'):
  src=root/name;dest=backup/name
  if src.is_file() and not dest.exists():dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700);atomic(dest,src.read_bytes())
 entries['data']['entries']=[e for e in existing if e['entry_id'] not in old]+selected
 # Remove only retired proxy entities/devices; unrelated user state stays intact.
 for name,key in [('core.entity_registry','entities'),('core.device_registry','devices')]:
  regpath=root/'.storage'/name;reg=json.loads(regpath.read_bytes());items=reg['data'][key]
  if key=='entities':
   items=[e for e in items if e.get('config_entry_id') not in old]
   known={e['entity_id'] for e in items}
   if known & {e['entity_id'] for e in migration[key]}:raise ValueError('Camera entity ID conflicts')
  else:
   items=[dict(e,config_entries=[x for x in e.get('config_entries',[]) if x not in old]) if old.intersection(e.get('config_entries',[])) else e for e in items if not (old.intersection(e.get('config_entries',[])) and not set(e.get('config_entries',[]))-old)]
  reg['data'][key]=items+migration[key];atomic(regpath,encoded(reg))
 atomic(path,encoded(entries))
 yamlpath=root/'configuration.yaml';raw=yamlpath.read_text();doc=yaml.compose(raw)
 if not isinstance(doc,yaml.MappingNode):raise ValueError('Expected YAML mapping')
 # Delete just the pilot key, preserving all other YAML bytes and comments.
 for key,value in reversed(doc.value):
  if key.value=='desk_remote':
   start=key.start_mark.index;end=value.end_mark.index
   raw=raw[:start]+raw[end:]
 atomic(yamlpath,raw.encode())
 shutil.rmtree(root/'custom_components/desk_remote',ignore_errors=True)
 # Native storage-mode dashboard: upstream video card, native navigation button.
 dashpath=root/'.storage/lovelace_dashboards'
 dash=json.loads(dashpath.read_bytes()) if dashpath.exists() else {'version':1,'minor_version':1,'key':'lovelace_dashboards','data':{'items':[]}}
 if any(d.get('url_path')=='desk-cameras' for d in dash['data']['items']):raise ValueError('Dashboard URL is already in use')
 dash['data']['items'].append({'id':'desk_cameras','show_in_sidebar':True,'icon':'mdi:cctv','title':'Cameras','require_admin':False,'mode':'storage','url_path':'desk-cameras'})
 dashboard={'version':1,'minor_version':1,'key':'lovelace.desk_cameras','data':{'config':{'title':'Cameras','views':[{'title':'Cameras','path':'cameras','icon':'mdi:cctv','cards':[{'type':'custom:webrtc-camera','title':'Bed Cam','ui':True,'mode':'webrtc,mse','streams':[{'url':'bed_hd','name':'HD'},{'url':'bed_sd','name':'SD'}]},{'type':'button','name':'Recordings','icon':'mdi:play-box-multiple','tap_action':{'action':'navigate','navigation_path':'/media-browser'}}]}]}}}
 atomic(dashpath,encoded(dash));atomic(root/'.storage/lovelace.desk_cameras',encoded(dashboard))
 atomic(marker,encoded({'version':1,'retired_entries':len(old),'installed_entries':sorted(new_ids)}))
 add_cameras(root);upgrade_cards(root)
ADDITIONS=pathlib.Path('/etc/desk-camera/core-additions.json')
def add_cameras(root):
 """Apply validated native entries only while the Core runtime is stopped."""
 if not ADDITIONS.is_file():return
 import hashlib
 raw=ADDITIONS.read_bytes();digest=hashlib.sha256(raw).hexdigest();marker=root/'.storage/desk_camera_additions'
 if marker.exists():
  if json.loads(marker.read_bytes()).get('sha256')!=digest:raise ValueError('Camera additions changed after activation')
  return
 migration=json.loads(raw)
 if migration.get('version')!=2:raise ValueError('Unknown camera additions')
 selected=migration['entries'];new_ids={e['entry_id'] for e in selected}
 if len(selected)!=2 or len(new_ids)!=2 or any(e['domain']!='tapo_control' for e in selected):raise ValueError('Invalid camera additions')
 if {e['data']['ip_address'] for e in selected}!={'192.168.1.161','192.168.1.163'}:raise ValueError('Unexpected camera hosts')
 device_ids={e['id'] for e in migration['devices']}
 if any(e.get('device_id') and e['device_id'] not in device_ids for e in migration['entities']):raise ValueError('Camera entity device missing')
 path=root/'.storage/core.config_entries';entries=json.loads(path.read_bytes());existing=entries['data']['entries']
 if new_ids & {e['entry_id'] for e in existing}:raise ValueError('Camera entry identity conflict')
 if any(e['domain']=='tapo_control' and e.get('data',{}).get('ip_address') in {'192.168.1.161','192.168.1.163'} for e in existing):raise ValueError('Camera already exists')
 entries['data']['entries']+=selected;writes={path:encoded(entries)}
 for name,key,identity in [('core.entity_registry','entities','entity_id'),('core.device_registry','devices','id')]:
  regpath=root/'.storage'/name;reg=json.loads(regpath.read_bytes());items=reg['data'][key];new=migration[key]
  if len({e[identity] for e in new})!=len(new) or {e[identity] for e in items}&{e[identity] for e in new}:raise ValueError('Camera registry conflict')
  if key=='entities' and any(e.get('config_entry_id') not in new_ids for e in new):raise ValueError('Unrelated camera entity')
  if key=='devices' and any(not (({e['config_entry_id']} if e.get('config_entry_id') else set(e.get('config_entries',[])))<=new_ids) for e in new):raise ValueError('Unrelated camera device')
  reg['data'][key]+=new;writes[regpath]=encoded(reg)
 dashpath=root/'.storage/lovelace.desk_cameras';dashboard=json.loads(dashpath.read_bytes());views=dashboard['data']['config']['views'];view=next(v for v in views if v.get('path')=='cameras')
 known={stream.get('url') for card in view['cards'] for stream in card.get('streams',[])}
 cards=migration['cards']
 if len(cards)!=2 or any(card.get('type')!='custom:webrtc-camera' or not card.get('streams') for card in cards):raise ValueError('Invalid camera cards')
 if any(stream['url'] in known for card in cards for stream in card['streams']):raise ValueError('Camera card already exists')
 index=next((i for i,c in enumerate(view['cards']) if c.get('name')=='Recordings'),len(view['cards']))
 view['cards'][index:index]=cards;writes[dashpath]=encoded(dashboard)
 # Validate the entire change before touching any file. Never overwrite the recovery copy.
 for p in writes:
  dest=BACKUP/'additions-v2'/p.relative_to(root)
  if not dest.exists():dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700);atomic(dest,p.read_bytes())
 for p,data in writes.items():atomic(p,data)
 atomic(marker,encoded({'version':2,'sha256':digest,'installed_entries':sorted(new_ids)}))


def upgrade_cards(root):
 marker=root/'.storage/desk_camera_ui_v4'
 if marker.exists():return
 path=root/'.storage/lovelace.desk_cameras'
 if not path.is_file():return
 doc=json.loads(path.read_bytes());changed=0
 def visit(value):
  nonlocal changed
  if isinstance(value,dict):
   aliases={'bed_hd','bed_sd','cam161_hd','cam161_sd','cam163_hd','cam163_sd'}
   streams=value.get('streams')
   if value.get('type')=='custom:webrtc-camera' and isinstance(streams,list) and streams and all(s.get('url') in aliases for s in streams):
    value['type']='custom:desk-camera-card';changed+=1
   if value.get('type')=='button' and value.get('name')=='Recordings' and value.get('tap_action',{}).get('navigation_path') in ('/media-browser','/media-browser/browser/app%2Cmedia-source%3A%2F%2Fdesk_cluster'):
    value['tap_action']['navigation_path']='/media-browser/browser/app%2Cmedia-source%3A%2F%2Ftapo_control';changed+=1
   for child in value.values():visit(child)
  elif isinstance(value,list):
   for child in value:visit(child)
 visit(doc)
 if changed:
  backup=BACKUP/'ui-v3';backup.mkdir(parents=True,exist_ok=True,mode=0o700)
  if not (backup/path.name).exists():atomic(backup/path.name,path.read_bytes())
  atomic(path,encoded(doc))
 atomic(marker,encoded({'version':4,'cards':changed}))
