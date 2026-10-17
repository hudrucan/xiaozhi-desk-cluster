"""Native Tapo browser with one media handle and lease-routed preparation."""
import asyncio,copy,datetime,time
from urllib.parse import urlparse,parse_qsl
from homeassistant.components import persistent_notification
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.models import PlayMedia
from custom_components.tapo_control.media_source import TapoMediaSource,parse_identifier,build_identifier
from . import bridge
from .camera_storage import HOSTS,storage_state
from .recording_model import PART_SECONDS,MESSAGES,cached_state,clip_id,label,parts
DOMAIN='tapo_control'
async def async_get_media_source(hass):return WorkerRecordings(hass)
class WorkerRecordings(TapoMediaSource):
 name='Tapo: Recordings'
 def __init__(self,hass):
  super().__init__(hass,None);self.domain=DOMAIN;self.jobs={};self.index_cache={};self.inventories={};self.locks={}
 def camera_entry(self,query):
  entry=self.hass.config_entries.async_get_entry(query.get('entry',''))
  workload=HOSTS.get(entry.data.get('ip_address')) if entry and entry.domain==DOMAIN else None
  if not workload or query.get('childID'):raise Unresolvable('Camera recording entry is unavailable.')
  return entry,workload,self.hass.data.get(DOMAIN,{}).get(entry.entry_id,{})
 def active(self,workload):return any(j.get('workload')==workload and j.get('task') and not j['task'].done() for j in self.jobs.values())
 async def inventory(self,workload):
  saved=self.inventories.get(workload)
  if saved and time.monotonic()-saved[0]<3:return saved[1]
  try:value=await bridge('camera_inventory',value={'workload':workload})
  except Exception:value={'items':[],'complete':False}
  self.inventories[workload]=(time.monotonic(),value);return value
 def check_storage(self,device):
  state=storage_state(device)
  if state=='absent':raise Unresolvable('No microSD card is detected in this camera.')
  if state=='needs_attention':raise Unresolvable('Check the microSD card in the Tapo app before playback.')
 async def async_browse_media(self,item):
  if item.identifier is None:
   value=await super().async_browse_media(item)
   value.children=[c for c in value.children or [] if self.enabled(c)]
   return value
  query=parse_identifier(item.identifier.split('/')[-1]);entry,workload,device=self.camera_entry(query);self.check_storage(device)
  inv=await self.inventory(workload)
  if 'startDate' in query and 'endDate' in query:
   return self.part_folder(query,device,workload,inv)
  key=item.identifier;cached=self.index_cache.get(key)
  busy=self.active(workload) or any(v.get('state')=='preparing' for v in inv['items'])
  if cached and (busy or time.monotonic()-cached[0]<30):value=copy.deepcopy(cached[1])
  elif busy:
   raise Unresolvable('A recording is being prepared. Reopen a previously viewed date, or wait for preparation to finish.')
  else:
   lock=self.locks.setdefault(entry.entry_id,asyncio.Lock())
   async with lock:
    try:value=await super().async_browse_media(item)
    except Unresolvable as err:
     if not cached:raise Unresolvable('The camera recording session is busy or temporarily unavailable. Retry shortly.') from err
     value=copy.deepcopy(cached[1]);value.title+=' · Last known list'
    else:
     if len(self.index_cache)>=64:self.index_cache.pop(next(iter(self.index_cache)))
     self.index_cache[key]=(time.monotonic(),copy.deepcopy(value))
  return self.decorate(value,workload,inv)
 def enabled(self,child):
  try:
   entry,_,device=self.camera_entry(dict(parse_qsl(urlparse(child.identifier).query)))
   return storage_state(device)=='ready'
  except Unresolvable:return False
 def decorate(self,value,workload,inventory):
  for child in value.children or []:
   q=parse_identifier(child.identifier)
   if not all(k in q for k in ('startDate','endDate','date')):continue
   start,end=int(q['startDate']),int(q['endDate']);state=cached_state(inventory['items'],workload,q['date'],start,end)
   if end-start>PART_SECONDS and state!='ready':
    ranges=parts(start,end);count=sum(cached_state(inventory['items'],workload,q['date'],a,b)=='ready' for a,b in ranges)
    child.can_play=False;child.can_expand=True;child.title+=f' · {len(ranges)} parts'+(f' · {count} downloaded' if count else '')
   else:child.title=label(child.title,state,inventory['complete'])
  return value
 def part_folder(self,query,device,workload,inventory):
  children=[]
  for index,(start,end) in enumerate(parts(query['startDate'],query['endDate']),1):
   title=f"Part {index} · "+self._format_clip_label(start,end,device.get('timezoneOffset',0))
   ident=build_identifier(dict(query,startDate=start,endDate=end,title=title,part='1'))
   state=cached_state(inventory['items'],workload,query['date'],start,end)
   children.append(self.generateView(ident,label(title,state,inventory['complete']),True,False))
  return self.generateView(build_identifier(query),query.get('title','Recording parts'),False,True,children=children)
 async def async_resolve_media(self,item):
  query=parse_identifier(item.identifier.split('/')[-1]);entry,workload,device=self.camera_entry(query);self.check_storage(device)
  try:clip={'date':query['date'].replace('-',''),'start':int(query['startDate']),'end':int(query['endDate'])}
  except (KeyError,ValueError):raise Unresolvable('Invalid recording. Reopen its date folder.') from None
  key=(workload,*clip.values());existing=self.jobs.get(key)
  if existing and existing.get('task') and not existing['task'].done():return await self.prepared(workload,existing)
  inv=await self.inventory(workload);cached=cached_state(inv['items'],workload,clip['date'],clip['start'],clip['end'])=='ready'
  pending=next((v for v in inv['items'] if v.get('state')=='preparing' and v.get('id')==clip_id(workload,clip['date'],clip['start'],clip['end'])),None)
  if pending:
   job=dict(pending,workload=workload,duration=clip['end']-clip['start']);self.jobs[key]=job
   job['task']=self._create_background_task(self.wait(job,'desk_camera_playback_'+job['id']),entry)
   return await self.prepared(workload,job)
  if not cached and any(v.get('state')=='preparing' for v in inv['items']):raise Unresolvable(MESSAGES['camera_busy'])
  if not cached and clip['end']-clip['start']>PART_SECONDS:raise Unresolvable('Open this recording folder and select a part of at most ten minutes.')
  if not cached and self.active(workload):raise Unresolvable(MESSAGES['camera_busy'])
  if not cached and sum(bool(j.get('task') and not j['task'].done()) for j in self.jobs.values())>=2:raise Unresolvable('Two clips are being prepared. Wait for one to finish.')
  lock=self.locks.setdefault(entry.entry_id,asyncio.Lock())
  async with lock:
   session=None
   if not cached:
    try:
     controller=device['controller'];uid=await self.hass.async_add_executor_job(controller.getUserID)
     session={'user_id':uid,'player_id':controller.playerID}
    except Exception:raise Unresolvable('The camera recording session is busy. Retry shortly.') from None
   try:result=await bridge('camera_prepare',value={'workload':workload,'clip':clip,'session':session})
   except Exception:raise Unresolvable('The camera worker did not respond. Check Camera workers in Cluster and retry.') from None
   if result.get('state')=='rejected':raise Unresolvable(MESSAGES.get(result.get('reason'),'The worker cannot prepare this clip now. Retry shortly.'))
   if result['state']=='ready':return self.play(workload,result)
   self.jobs={k:j for k,j in self.jobs.items() if j.get('task') and not j['task'].done()}
   job=dict(result,workload=workload,duration=clip['end']-clip['start']);self.jobs[key]=job
   notice='desk_camera_playback_'+result['id']
   persistent_notification.async_create(self.hass,'Preparing the clip on '+result['node']+'. HA is waiting for preparation before opening the player. This can take close to the part duration.',title='Preparing camera recording',notification_id=notice)
   job['task']=self._create_background_task(self.wait(job,notice),entry)
  return await self.prepared(workload,job)
 async def prepared(self,workload,job):
  # Resolve only a completed artifact; do not leave a native video HTTP load idle
  # for the entire camera download. Closing the browser must not cancel the job.
  await asyncio.shield(job['task'])
  try:status=await bridge('camera_status',value={k:job[k] for k in ('workload','node','id')})
  except Exception:raise Unresolvable('Unable to check the prepared clip. Reopen its folder and retry.') from None
  if status.get('state')!='ready':raise Unresolvable('The worker could not prepare this clip. Reopen its folder and retry.')
  return self.play(workload,job)
 def play(self,workload,result):
  path='/api/desk_cluster/recording/'+workload+'/'+result['node']+'/'+result['id']
  return PlayMedia(async_sign_path(self.hass,path,datetime.timedelta(hours=2)),'video/mp4')
 async def wait(self,job,notice):
  try:
   for _ in range(min(384,max(42,(job['duration']+120)//5))):
    await asyncio.sleep(5)
    status=await bridge('camera_status',value={k:job[k] for k in ('workload','node','id')})
    if status['state']=='ready':
     self.inventories.pop(job['workload'],None)
     persistent_notification.async_create(self.hass,'The clip is ready on '+job['node']+'. Reopen its date or parts folder in Tapo: Recordings to see ✓ Downloaded.',title='Camera recording ready',notification_id=notice);return
    if status['state'] in ('failed','missing'):raise ValueError()
   raise TimeoutError()
  except asyncio.CancelledError:raise
  except Exception:persistent_notification.async_create(self.hass,'The worker could not finish. Reopen the folder and select the clip to retry.',title='Camera recording interrupted',notification_id=notice)

def install(hass):
 """Adapt only the pinned native media factory; vendor files remain unchanged."""
 from custom_components.tapo_control import media_source as native
 original=native.async_get_media_source;native.async_get_media_source=async_get_media_source
 from homeassistant.const import EVENT_HOMEASSISTANT_STOP
 hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP,lambda _:setattr(native,'async_get_media_source',original))
