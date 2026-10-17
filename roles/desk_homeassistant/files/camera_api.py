"""Fixed, read-only camera status for the authenticated HA bridge."""
import asyncio,base64,secrets,sys,concurrent.futures,json,pathlib,time,urllib.request,threading
_cache=None;_at=0;_lock=threading.Lock()
def status():
 global _cache,_at
 with _lock:
  if _cache is not None and time.monotonic()-_at<2:return _cache
  path=pathlib.Path('/etc/desk-camera/config.json')
  if not path.is_file():return {'enabled':False,'cameras':[]}
  c=json.loads(path.read_bytes());opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
  def get(item):
   node,ip=item
   try:
    with opener.open('http://'+ip+':20226/status',timeout=1) as r:v=json.loads(r.read(32769))
    if v.get('node')!=node or not 0<=time.time()-v['observed_at']<10:raise ValueError()
    return node,v
   except Exception:return node,None
  with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:views=dict(pool.map(get,c['members'].items()))
  cameras=[]
  for camera in c.get('cameras',[]):
   workload=camera['workload'];nodes=[]
   for node,v in views.items():
    selected=next((x for x in v.get('cameras',[]) if x.get('workload')==workload),{}) if v else {}
    nodes.append({'node':node,'online':bool(v),'ready':selected.get('local_ready') is True,'reachable':selected.get('reachable'),'score':selected.get('score'),'draining':selected.get('draining')})
   owners=[x['node'] for x in nodes if x['ready']]
   owner=owners[0] if len(owners)==1 else None
   source=next((x for x in (views.get(owner) or {}).get('cameras',[]) if x.get('workload')==workload),{})
   cameras.append({'workload':workload,'owner':owner,'ready':bool(owner),'nodes':nodes,'generation':source.get('generation'),'owner_changed_at':source.get('owner_changed_at'),'previous_owner':source.get('previous_owner')})
  _cache={'enabled':True,'cameras':cameras,'observed_at':time.time()};_at=time.monotonic();return _cache


async def playback(action,value):
 reader,writer=await asyncio.wait_for(asyncio.open_unix_connection('/run/desk-camera/bridge.sock'),2)
 try:
  writer.write(json.dumps(dict(value,action=action)).encode()+b'\n');await writer.drain()
  raw=await asyncio.wait_for(reader.readline(),7)
  if len(raw)>65536:raise ValueError()
  result=json.loads(raw)
  if result.get('ok') is not True:raise ValueError('Playback worker unavailable')
  return result['value']
 finally:writer.close();await writer.wait_closed()

def artifact(value):
 sys.path.insert(0,'/usr/local/libexec/desk-camera')
 from playback import clip_path
 from protocol import pack
 c=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes())
 path=clip_path(value['workload'],value['id'])
 scope={'workload':value['workload'],'id':value['id']}
 route='/artifact/'+value['workload']+'/'+value['id']
 if set(value)=={'workload','id','node','generation','file'}:
  from hls_files import artifact_path
  artifact_path(path.parent,value['generation'],value['file']);scope.update(generation=value['generation'],file=value['file'])
  route='/hls/'+value['workload']+'/'+value['id']+'/'+value['generation']+'/'+value['file']
 elif set(value)!={'workload','id','node'}:raise ValueError()
 if value['node'] not in c['members']:raise ValueError()
 token=pack({'sender':c['node'],'recipient':value['node'],'id':secrets.token_hex(16),'at':time.time(),'action':'artifact','value':scope},c['key'])
 return {'url':'http://'+c['members'][value['node']]+':20226'+route,'authorization':base64.b64encode(token).decode()}


def thumbnail_ticket(value):
 import hashlib,re
 sys.path.insert(0,'/usr/local/libexec/desk-camera')
 from playback import clip_path
 from protocol import pack
 if set(value)!={'workload','node','id','generation','source','digest'}:raise ValueError()
 clip_path(value['workload'],value['id'])
 if value['source'] not in ('ts','cache','fetch','cancel') or not re.fullmatch('[a-f0-9]{64}',value['digest']):raise ValueError()
 if value['source'] in ('ts','fetch','cancel') and not re.fullmatch('[a-f0-9]{32}',value['generation']):raise ValueError()
 if value['source']=='cancel' and value['digest']!=hashlib.sha256(b'').hexdigest():raise ValueError()
 if value['source']=='cache' and (value['generation']!='' or value['digest']!=hashlib.sha256(b'').hexdigest()):raise ValueError()
 c=json.loads(pathlib.Path('/etc/desk-camera/config.json').read_bytes())
 if value['node'] not in c['members']:raise ValueError()
 scope={k:v for k,v in value.items() if k!='node'}
 token=pack({'sender':c['node'],'recipient':value['node'],'id':secrets.token_hex(16),'at':time.time(),'action':'thumbnail','value':scope},c['key'])
 return {'url':'http://'+c['members'][value['node']]+':20226/thumbnail/'+value['workload']+'/'+value['id'],'authorization':base64.b64encode(token).decode()}
