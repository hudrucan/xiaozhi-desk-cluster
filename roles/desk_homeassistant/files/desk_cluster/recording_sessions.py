"""Ephemeral browser viewers; last close drains only its managed worker attempt."""
import asyncio,datetime,re,time,types
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from . import bridge

class Sessions:
 def __init__(self,source):
  self.source=source;self.hass=source.hass;self.clients={};self.records={};self.jobs={};self.drains={}
  self.task=self.hass.async_create_background_task(self.run(),'desk_recording_viewers')
  self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP,self.stop)
 def live(self,scope,key=None):
  r=self.records.get(scope)
  return bool(r and time.monotonic()-r['at']<20 and (key is None or r['key']==key))
 def check(self,scope,key):
  if not self.live(scope,key):raise ValueError('Recording viewer closed')
 def bind(self,scope,key,job):
  # An attempt is an ephemeral worker identity, distinct from the cached clip ID.
  if re.fullmatch('[a-f0-9]{32}',job.get('attempt','')):
   current={k:job[k] for k in ('workload','node','id','attempt')}
   old=self.jobs.get(key,{})
   if old.get('external') and all(old.get(k)==v for k,v in current.items()):current['external']=True
   self.jobs[key]=current
  if not self.live(scope,key):self.orphan(key)
 def orphan(self,key):
  if any(self.live(scope,key) for scope in self.records):return
  job=self.jobs.pop(key,None)
  if not job:return
  if job.get('external'):return  # The controller owns each detached viewer ref.
  task=asyncio.create_task(self.cancel(key,job));self.drains[key]=task
  task.add_done_callback(lambda t:self.drains.pop(key,None) if self.drains.get(key) is t else None)
 async def cancel(self,key,job):
  # Keep camera busy until the worker acknowledges process/session cleanup.
  try:
   async with asyncio.timeout(30):
    while True:
     result=await bridge('camera_cancel',value=job)
     if result.get('state')=='cancelled':break
     await asyncio.sleep(.5)
  except Exception:pass  # Independent 30s worker viewer deadline remains.
  finally:await self.cleanup(key,job)
 async def cleanup(self,key,job):
  # This waiter only polls Core-side progress. Once the controller owns viewer
  # refs, stop that polling/notice without sending any worker cancellation.
  task=self.source.jobs.get(key,{}).get('task')
  current=self.source.jobs.get(key,{})
  if current.get('attempt')==job.get('attempt') and task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
  self.source.inventories.pop(key[0],None)
  if hasattr(self.source,'_thumbnails') and not any(self.live(s) and r['key'][0]==key[0] for s,r in self.records.items()):
   for entry in list(self.source._thumbnails.suspended):
    try:
     if self.source.camera_entry({'entry':entry})[1]==key[0]:self.source._thumbnails.resume(entry)
    except Exception:pass
  from homeassistant.components import persistent_notification
  persistent_notification.async_dismiss(self.hass,'desk_camera_playback_'+job['id'])
 async def drain(self,workload):
  tasks=[t for key,t in list(self.drains.items()) if key[0]==workload]
  if tasks:await asyncio.gather(*(asyncio.shield(t) for t in tasks))
 def parse(self,user,value):
  if set(value) not in ({'action','client','sequence','scope'},{'action','client','sequence','scope','clip'}):raise ValueError('Invalid viewer')
  client,seq,scope=value['client'],value['sequence'],value['scope']
  if not isinstance(user,str) or type(seq) is not int or not 0<seq<10**9 or not all(isinstance(v,str) and re.fullmatch('[a-f0-9]{32}',v) for v in (client,scope)):raise ValueError('Invalid viewer')
  previous=self.clients.get((user,client))
  if previous and (seq<previous['sequence'] or (seq==previous['sequence'] and scope!=previous['scope'])):raise ValueError('Stale viewer')
  return client,seq,scope,previous
 async def action(self,user,value):
  client,seq,scope,previous=self.parse(user,value);action=value['action']
  if action not in ('open','touch','close'):raise ValueError('Unknown viewer action')
  if action=='open':
   clip=value.get('clip')
   if not isinstance(clip,dict) or set(clip)!={'entry','date','start','end'}:raise ValueError('Invalid clip')
   if not isinstance(clip['entry'],str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',clip['entry']) or not isinstance(clip['date'],str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',clip['date']):raise ValueError('Invalid clip')
   datetime.datetime.strptime(clip['date'],'%Y-%m-%d')
   start,end=clip['start'],clip['end']
   if type(start) is not int or type(end) is not int or not 1500000000<=start<end<=2200000000 or end-start>1800:raise ValueError('Invalid clip')
   _,workload,_=self.source.camera_entry({'entry':clip['entry']})
   key=(workload,clip['date'].replace('-',''),start,end)
   if previous and seq==previous['sequence']:raise ValueError('Viewer already used')
   if previous:
    old=self.records.pop(previous['scope'],None)
    if old:self.orphan(old['key'])
   if scope in self.records:raise ValueError('Viewer already used')
   if len(self.records)>=64 or (len(self.clients)>=128 and (user,client) not in self.clients):raise ValueError('Too many viewers')
   self.clients[user,client]={'sequence':seq,'scope':scope,'at':time.monotonic()}
   self.records[scope]={'user':user,'client':client,'key':key,'entry':clip['entry'],'at':time.monotonic()}
   try:
    await self.drain(workload);self.check(scope,key)
    from urllib.parse import urlencode
    query={'entry':clip['entry'],'date':clip['date'],'startDate':start,'endDate':end,'desk_viewer':scope}
    result=await self.source.async_resolve_media(types.SimpleNamespace(identifier='tapo_control/?'+urlencode(query)))
    self.check(scope,key)
    owner=getattr(self.source,'owner',None)
    if owner and owner.enabled(workload):
     from .recording_model import clip_id
     from urllib.parse import urlsplit
     serving=urlsplit(result.url).path.split('/')
     if len(serving)<7 or serving[1:4]!=['api','desk_cluster','recording'] or serving[4]!=workload:raise ValueError('Recording route')
     node,ident=serving[5:7]
     if ident!=clip_id(workload,clip['date'],start,end):raise ValueError('Recording clip')
     job=self.jobs.get(key,{})
     grant=await bridge('camera_delivery',value={'workload':workload,'node':node,'id':ident,'attempt':job.get('attempt'),'viewer':scope,'deadline':time.time()+25})
     self.check(scope,key)
     self.jobs.setdefault(key,dict(workload=workload,node=node,id=ident))['external']=True
     await self.cleanup(key,dict(job,workload=workload,node=node,id=ident))
     self.check(scope,key)
     return {'url':grant['url'],'mime_type':grant['mime_type'],'delivery':{k:grant[k] for k in ('touch_url','close_url','viewer','heartbeat_seconds','expires_in_seconds')}}
    return {'url':result.url,'mime_type':result.mime_type}
   except BaseException:
    self.records.pop(scope,None);self.orphan(key);raise
  # Closing before a delayed open arrives is a tombstone, never a new lease.
  if not previous or seq>previous['sequence']:
   if action=='touch':raise ValueError('Unknown viewer')
   if len(self.clients)>=128 and (user,client) not in self.clients:raise ValueError('Too many viewers')
   self.clients[user,client]={'sequence':seq,'scope':scope,'at':time.monotonic()}
  r=self.records.get(scope)
  if r and (r['user'],r['client'])!=(user,client):raise ValueError('Viewer belongs to another client')
  if action=='touch':
   if not r or not self.live(scope):raise ValueError('Viewer expired')
   r['at']=time.monotonic();return {'active':True}
  if r:
   self.records.pop(scope,None);self.orphan(r['key']);await self.drain(r['key'][0])
   if hasattr(self.source,'_thumbnails') and not any(self.live(s) and item['key'][0]==r['key'][0] for s,item in self.records.items()):self.source._thumbnails.resume(r.get('entry'))
  return {'closed':True}
 async def sweep(self):
  for scope,r in list(self.records.items()):
   if not self.live(scope):self.records.pop(scope,None);self.orphan(r['key'])
  self.clients={k:v for k,v in self.clients.items() if time.monotonic()-v['at']<3600 or v['scope'] in self.records}
  for key,job in list(self.jobs.items()):
   if job.get('external'):continue
   if any(self.live(scope,key) for scope in self.records):
    try:
     status=await bridge('camera_touch',value=job)
     if status.get('state')=='missing':self.jobs.pop(key,None)
    except Exception:pass
   else:self.orphan(key)
 async def run(self):
  while True:await asyncio.sleep(5);await self.sweep()
 async def stop(self,_):
  self.task.cancel();self.records.clear()
  for key in list(self.jobs):self.orphan(key)
  await asyncio.gather(self.task,*list(self.drains.values()),return_exceptions=True)

class RecordingSessionView(HomeAssistantView):
 url='/api/desk_cluster/recording_session';name='api:desk_cluster:recording_session'
 def __init__(self,hass):self.hass=hass
 async def post(self,request):
  try:
   if request.content_length is None or request.content_length>2048:raise ValueError('Viewer size')
   value=await request.json();source=self.hass.data['desk_cluster']['recording_source']
   return web.json_response(await source.viewer_manager().action(request['hass_user'].id,value),headers={'Cache-Control':'no-store'})
  except asyncio.CancelledError:raise
  except Exception:raise web.HTTPConflict(text='Recording viewer closed or worker unavailable; close and retry.') from None
