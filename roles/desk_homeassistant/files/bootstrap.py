"""Offline import only: preserve native auth, retire the stopped camera pilot."""
import argparse,json,os,pathlib,shutil,socket,subprocess
import yaml
from config_journal import atomic,encoded
from snapshot import create,sha,unpack
ROOT=pathlib.Path('/var/lib/desk-homeassistant')
class Secret(str):pass
class Loader(yaml.SafeLoader):pass
class Dumper(yaml.SafeDumper):pass
Loader.add_constructor('!secret',lambda loader,node:Secret(loader.construct_scalar(node)))
Dumper.add_representer(Secret,lambda dumper,value:dumper.represent_scalar('!secret',str(value)))

def seed(c,source):
 out=ROOT/'seed.json'
 if out.exists():print('retained');return
 if subprocess.run(['podman','container','exists','desk-ha-poc-core'],capture_output=True).returncode==0:raise ValueError('Retire the old Core container before import')
 source=pathlib.Path(source)
 if not (source/'.storage/auth').is_file():raise ValueError('Completed onboarding required')
 target=ROOT/'import'
 if target.exists():shutil.rmtree(target)
 shutil.copytree(source,target)
 config=yaml.load((target/'configuration.yaml').read_text(),Loader=Loader)
 config['homeassistant']['name']='Desk Home Assistant';config['homeassistant']['internal_url']='http://192.168.1.186:8123'
 config.setdefault('http',{}).update(server_host='0.0.0.0',server_port=8123,use_x_forwarded_for=True,trusted_proxies=['10.88.0.1','10.10.10.11','10.10.10.12','10.10.10.13'])
 relay=config.get('go2rtc',{})
 if isinstance(relay,dict) and str(relay.get('url','')).endswith(':19840'):config.pop('go2rtc')
 config['desk_cluster']={}
 atomic(target/'configuration.yaml',yaml.dump(config,Dumper=Dumper,sort_keys=False).encode())
 entries=target/'.storage/core.config_entries'
 if entries.exists():
  v=json.loads(entries.read_bytes())
  for entry in v['data']['entries']:
   if entry['domain']=='generic' and entry.get('title')=='Tapo C222 PoC':entry['disabled_by']='user'
  atomic(entries,encoded(v))
 component=target/'custom_components/desk_cluster';component.mkdir(parents=True,exist_ok=True)
 for name in ('__init__.py','manifest.json','panel.js'):shutil.copy2(pathlib.Path(__file__).parent/'desk_cluster'/name,component/name)
 from http_config import configure
 configure(target)
 blob,manifest=create(target,c['encryption_key'],c['image']);name=sha(blob)
 atomic(ROOT/'blobs'/name,blob)
 value={'digest':name,'bytes':len(blob),'created_at':manifest['at'],'config_digest':manifest['config_digest'],'image':c['image'],'format':manifest['format']}
 atomic(out,encoded(value));print('created')

def preflight(c):
 if c['node'] not in c['members'] or set(c['members'])!={'deskb1x','deskb2x','deskb3x'}:raise ValueError('Invalid cluster')
 fs=os.statvfs('/var/lib')
 if fs.f_bavail*fs.f_frsize<6*1024**3:raise ValueError('Retain 6 GiB free')
 if subprocess.run(['systemctl','is-active','--quiet','desk-homeassistant'],capture_output=True).returncode!=0:
  mem={k:int(v.split()[0]) for k,v in (line.split(':',1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())}
  if mem['MemAvailable']<1800*1024:raise ValueError('Insufficient Core reserve')
  for port in (18123,20223,20225):
   with socket.socket() as sock:sock.bind((c['members'][c['node']],port))
 print('Core resources and private ports ready')
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('action',choices=['preflight','seed','validate-seed']);p.add_argument('--config',default='/etc/desk-homeassistant/config.json');args=p.parse_args()
 try:
  c=json.loads(pathlib.Path(args.config).read_bytes())
  if args.action=='seed':seed(c,'/var/lib/desk-homeassistant-poc/core')
  elif args.action=='preflight':preflight(c)
  else:
   v=c['seed'];raw=(ROOT/'blobs'/v['digest']).read_bytes()
   if sha(raw)!=v['digest'] or len(raw)!=v['bytes']:raise ValueError('Seed mismatch')
   import tempfile
   with tempfile.TemporaryDirectory() as tmp:unpack(raw,c['encryption_key'],c['image'],tmp)
   print('Encrypted seed verified')
 except Exception:raise SystemExit('Core preparation failed; private data withheld')
