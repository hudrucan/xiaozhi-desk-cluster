"""Folder-scoped thumbnail orchestration. Camera fetch/decode runs on its owner."""
import asyncio,datetime,hashlib,io,json,re,secrets,time
import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from .thumbnail_cache import Cache
from .recording_model import MediaSessionExpired
PLACEHOLDER=b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180"><rect width="320" height="180" fill="#333"/><path d="M120 68h60v44h-60zM180 80l20-10v40l-20-10z" fill="#888"/></svg>'
# Intentional media arbitration is transient, never a failed/cached thumbnail.
PAUSED=PLACEHOLDER.replace(b'<svg ',b'<svg data-desk-thumbnail-paused="true" ')

def validate(entry,date,start,end):
 if not re.fullmatch('[A-Za-z0-9_-]{1,64}',entry):raise ValueError('Invalid camera entry')
 datetime.datetime.strptime(date,'%Y%m%d');start,end=int(start),int(end)
 if not 1500000000<=start<end<=2200000000 or end-start>86400:raise ValueError('Invalid thumbnail range')
 return start,end

def jpeg(data):
 from PIL import Image
 if not isinstance(data,bytes) or not 4<len(data)<=2*1024*1024:raise ValueError('Invalid JPEG size')
 with Image.open(io.BytesIO(data)) as p:
  if p.format!='JPEG' or not all(0<n<=4096 for n in p.size):raise ValueError('Invalid JPEG')
  p.verify()
 return data

def token(user,client,sequence,scope):
 if not isinstance(user,str) or not re.fullmatch('[a-f0-9]{32}',client) or not re.fullmatch('[a-f0-9]{32}',scope) or type(sequence) is not int or not 0<sequence<10**9:raise ValueError('Invalid folder scope')
 return user,client,sequence,scope

class Thumbnails:
 def __init__(self,source,bridge):
  self.source=source;self.hass=source.hass;self.bridge=bridge;self.cache=Cache();self.tasks={};self.consumers={};self.clients={};self.node_locks={};self.suspended={};self.semaphore=asyncio.Semaphore(2)
  self.counts={'requests':0,'hits':0,'misses':0,'event_jpegs':0,'preview_frames':0,'placeholders':0,'worker_requests':0,'cancelled':0,'scope_rejections':0};self.last=None
  self.cleaner=self.hass.async_create_background_task(self.clean(),'desk_thumbnail_cache_gc');self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP,self.stop)
 async def clean(self):
  while True:
   await asyncio.sleep(5);self.cache.prune()
   expired={key for key,v in self.clients.items() if time.monotonic()-v['at']>90}
   for key in expired:self.clients.pop(key,None)
   await self.orphans()
 async def stop(self,_):
  self.cleaner.cancel()
  for task in list(self.tasks.values()):
   if not task.cancelling():task.cancel()
  await asyncio.gather(self.cleaner,*list(self.tasks.values()),return_exceptions=True);self.cache.data.clear();self.cache.bytes=0
 def live(self,t,key=None):
  v=self.clients.get(t[:2])
  if not v or time.monotonic()-v['at']>90 or (v['sequence'],v['scope'])!=t[2:] or v['folder'] is None:return False
  if key:
   entry,date,start,end=key;f=v['folder']
   return entry==f['entry'] and date==f['date'] and (f['lower'] is None or f['lower']<=start<end<=f['upper'])
  return True
 async def orphans(self):
  cancelled=[]
  for key,task in list(self.tasks.items()):
   users=self.consumers.get(key,set());users.intersection_update(t for t in list(users) if self.live(t,key))
   if not users and not task.done():
    if not task.cancelling():task.cancel();self.counts['cancelled']+=1
    cancelled.append(task)
  if cancelled:await asyncio.gather(*cancelled,return_exceptions=True)
 async def focus(self,user,value):
  if set(value)!={'client','sequence','scope','folder'}:raise ValueError('Invalid focus')
  client=value['client'];sequence=value['sequence'];scope=value['scope'];folder=value['folder']
  token(user,client,sequence,scope or '0'*32)
  if folder is not None:
   if not isinstance(folder,dict) or set(folder)!={'entry','date','lower','upper'} or scope is None:raise ValueError('Invalid folder')
   if folder['lower'] is None:
    if folder['upper'] is not None:raise ValueError('Invalid range')
    validate(folder['entry'],folder['date'],1500000000,1500000001)
   else:
    if type(folder['lower']) is not int or type(folder['upper']) is not int:raise ValueError('Invalid range')
    validate(folder['entry'],folder['date'],folder['lower'],folder['upper'])
  elif scope is not None:raise ValueError('Invalid closed scope')
  previous=self.clients.get((user,client))
  if previous and sequence<previous['sequence']:return {'accepted':False}
  if previous and sequence==previous['sequence'] and (scope!=previous['scope'] or folder!=previous['folder']):raise ValueError('Conflicting focus')
  if not previous and len(self.clients)>=64:raise ValueError('Too many tabs')
  self.clients[user,client]={'sequence':sequence,'scope':scope,'folder':folder,'at':time.monotonic()}
  await self.orphans();return {'accepted':True}
 def resume(self,entry):
  self.suspended.pop(entry,None);self.cache.clear_placeholders(entry)
 async def pause(self,entry):
  self.suspended[entry]=time.monotonic()+15;tasks=[task for key,task in list(self.tasks.items()) if key[0]==entry and not task.done()]
  for task in tasks:
   if not task.cancelling():task.cancel();self.counts['cancelled']+=1
  if tasks:await asyncio.gather(*tasks,return_exceptions=True)
 def stats(self):return dict(self.counts,cache=self.cache.stats(),inflight=len(self.tasks),active_tabs=sum(v['folder'] is not None for v in self.clients.values()),last=self.last,execution='camera owner worker; Core routes and caches; legacy cameras retain Core handles')
 async def get(self,entry,date,start,end,consumer):
  start,end=validate(entry,date,start,end);key=entry,date,start,end;self.counts['requests']+=1
  # The focus POST starts before native image rendering, but network ordering
  # is not guaranteed. Briefly admit that exact registration race only.
  for _ in range(20):
   state=self.clients.get(consumer[:2])
   if state and state['sequence']>=consumer[2]:break
   await asyncio.sleep(.025)
  if not self.live(consumer,key):self.counts['scope_rejections']+=1;return PLACEHOLDER,'placeholder'
  found=self.cache.get(key)
  if found:self.counts['hits']+=1;return found
  task=self.tasks.get(key)
  if task and task.cancelling():
   await asyncio.gather(task,return_exceptions=True);task=None
  if task is None:
   if len(self.tasks)>=64:return PLACEHOLDER,'placeholder'
   self.counts['misses']+=1;task=asyncio.create_task(self.produce(key));self.tasks[key]=task;self.consumers[key]=set()
   def done(t):
    if self.tasks.get(key) is t:self.tasks.pop(key,None);self.consumers.pop(key,None)
   task.add_done_callback(done)
  self.consumers[key].add(consumer)
  try:return await asyncio.shield(task)
  except asyncio.CancelledError:
   if not task.cancelled():raise
   return (PAUSED,'paused') if time.monotonic()<self.suspended.get(entry,0) else (PLACEHOLDER,'placeholder')
 async def produce(self,key):
  entry,date,start,end=key;started=time.monotonic();data=PLACEHOLDER;kind='placeholder';stage='admission';session=None
  try:
   async with asyncio.timeout(40):
    async with self.source.locks.setdefault(entry,asyncio.Lock()):
     if time.monotonic()<self.suspended.get(entry,0):return PAUSED,'paused'
     _,workload,device=self.source.camera_entry({'entry':entry});self.source.check_storage(device)
     inv=await self.source.inventory(workload)
     if not inv.get('complete') or self.source.active(workload) or any(v.get('state') in ('preparing','playable') for v in inv['items']) or self.source._any_download_active(device):return PAUSED,'paused'
     fleet=await self.bridge('cameras');camera=next((v for v in fleet['cameras'] if v['workload']==workload and v['ready'] and v['owner']),None)
     if camera is None:return PAUSED,'paused'
     node=camera['owner']
     async with self.node_locks.setdefault(node,asyncio.Lock()),self.semaphore:
      if not (getattr(self.source,'owner',None) and self.source.owner.enabled(workload)):
       stage='handle';controller=device['controller'];uid=await self.hass.async_add_executor_job(controller.getUserID)
       session={'user_id':uid,'player_id':controller.playerID}
      stage='worker';data,kind=await self.remote(workload,camera,{'date':date,'start':start,'end':end},session)
      self.counts['event_jpegs' if kind=='event' else 'preview_frames']+=1
   self.cache.put(key,data,kind);self.last={'kind':kind,'bytes':len(data),'seconds':round(time.monotonic()-started,3),'node':node}
  except asyncio.CancelledError:raise
  except MediaSessionExpired:
   await self.source.invalidate_media_session(entry,session)
   self.last={'kind':'placeholder','stage':stage,'reason':'session_expired','seconds':round(time.monotonic()-started,3)};self.counts['placeholders']+=1
  except Exception as error:
   self.last={'kind':'placeholder','stage':stage,'error_type':type(error).__name__,'seconds':round(time.monotonic()-started,3)};self.counts['placeholders']+=1;self.cache.put(key,data,kind)
  return data,kind
 async def remote(self,workload,camera,clip,session):
  data=json.dumps({'clip':clip,'session':session},separators=(',',':')).encode();ident=secrets.token_hex(16);base={'workload':workload,'node':camera['owner'],'id':ident,'generation':camera['generation']}
  ticket=await self.bridge('camera_thumbnail',value=dict(base,source='fetch',digest=hashlib.sha256(data).hexdigest()));client=async_get_clientsession(self.hass);complete=False
  try:
   self.counts['worker_requests']+=1
   async with client.post(ticket['url'],data=data,headers={'X-Desk-Artifact':ticket['authorization']},allow_redirects=False,timeout=aiohttp.ClientTimeout(total=36,sock_connect=3,sock_read=35)) as r:
    if r.status==409 and r.headers.get('X-Desk-Media-Error')=='session_expired':raise MediaSessionExpired()
    kind=r.headers.get('X-Thumbnail-Source')
    if r.status!=200 or kind not in ('event','keyframe') or not 4<int(r.headers.get('Content-Length',0))<=2*1024*1024:raise ValueError('Worker image unavailable')
    image=jpeg(await r.content.readexactly(int(r.headers['Content-Length'])));complete=True;return image,kind
  finally:
   if not complete:
    # Explicit cancellation plus worker tombstones cover disconnects and a
    # cancel arriving before its original POST. Await cleanup before playback.
    try:
     cancel=await self.bridge('camera_thumbnail',value=dict(base,source='cancel',digest=hashlib.sha256(b'').hexdigest()))
     async with client.post(cancel['url'],data=b'',headers={'X-Desk-Artifact':cancel['authorization']},allow_redirects=False,timeout=aiohttp.ClientTimeout(total=18,sock_connect=3)) as r:
      await r.read()
    except Exception:pass  # Independent owner lease and 32-second job fence remain.

class ThumbnailView(HomeAssistantView):
 url='/api/desk_cluster/thumbnail/{entry}/{date}/{start}/{end}';name='api:desk_cluster:thumbnail'
 def __init__(self,hass):self.hass=hass
 async def get(self,request,entry,date,start,end):
  try:
   q=request.query;consumer=token(request['hass_user'].id,q.get('client',''),int(q.get('sequence','0')),q.get('scope',''))
   source=self.hass.data['desk_cluster']['recording_source'];data,kind=await source.thumbnail_manager().get(entry,date,start,end,consumer)
  except (KeyError,ValueError):raise web.HTTPNotFound() from None
  return web.Response(body=data,content_type='image/svg+xml' if kind in ('placeholder','paused') else 'image/jpeg',headers={'Cache-Control':'private, no-store' if kind in ('placeholder','paused') else 'private, max-age=300','X-Content-Type-Options':'nosniff','X-Thumbnail-Source':kind})
class ThumbnailScopeView(HomeAssistantView):
 url='/api/desk_cluster/thumbnail_scope';name='api:desk_cluster:thumbnail_scope'
 def __init__(self,hass):self.hass=hass
 async def post(self,request):
  try:
   if request.content_length is None or request.content_length>2048:raise ValueError('Focus size')
   value=await request.json();source=self.hass.data['desk_cluster']['recording_source']
   return web.json_response(await source.thumbnail_manager().focus(request['hass_user'].id,value))
  except (KeyError,ValueError,TypeError):raise web.HTTPBadRequest() from None
