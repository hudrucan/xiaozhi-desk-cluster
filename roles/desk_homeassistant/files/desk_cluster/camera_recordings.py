"""Native Tapo browsing with owned, lease-routed recording preparation."""
import asyncio,datetime
from urllib.parse import urlparse,parse_qsl
from homeassistant.components import persistent_notification
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.models import PlayMedia,BrowseMediaSource
from homeassistant.components.media_player import MediaClass,MediaType
from custom_components.tapo_control.media_source import TapoMediaSource,parse_identifier
from . import bridge

DOMAIN='tapo_control'
from .camera_storage import HOSTS,storage_state
async def async_get_media_source(hass):return WorkerRecordings(hass)
class WorkerRecordings(TapoMediaSource):
 name='Tapo: Recordings'
 def __init__(self,hass):
  super().__init__(hass,None);self.domain=DOMAIN;self.jobs={}
 def generateView(self,*args,**kwargs):return super().generateView(*args,**kwargs)
 async def async_browse_media(self,item):
  value=await super().async_browse_media(item)
  if item.identifier is None and value.children:
   def enabled(child):
    query=dict(parse_qsl(urlparse(child.identifier).query));entry=self.hass.config_entries.async_get_entry(query.get('entry',''))
    return entry and entry.data.get('ip_address') in HOSTS and storage_state(self.hass.data.get(DOMAIN,{}).get(entry.entry_id))=='ready'
   value.children=[child for child in value.children if enabled(child)]
  return value
 async def async_resolve_media(self,item):
  try:
   parts=item.identifier.split('/')
   if len(parts)!=2:raise ValueError()
   query=parse_identifier(parts[1]);entry=self.hass.config_entries.async_get_entry(query['entry'])
   workload=HOSTS.get(entry.data.get('ip_address')) if entry and entry.domain=='tapo_control' else None
   if not workload or query.get('childID'):raise ValueError()
   state=storage_state(self.hass.data.get(DOMAIN,{}).get(entry.entry_id))
   if state=='absent':raise Unresolvable('No microSD card is detected in this camera.')
   if state=='needs_attention':raise Unresolvable('The microSD card needs attention in the Tapo app before playback.')
   clip={'date':query['date'].replace('-',''),'start':int(query['startDate']),'end':int(query['endDate'])};key=(workload,*clip.values())
   existing=self.jobs.get(key)
   if existing and existing.get('task') and not existing['task'].done():
    return self.play(workload,existing)
   result=await bridge('camera_prepare',value={'workload':workload,'clip':clip})
   if result['state']=='ready':return self.play(workload,result)
   if sum(bool(j.get('task') and not j['task'].done()) for j in self.jobs.values())>=2:raise Unresolvable('Two recording jobs are already running. Wait for one to finish.')
   # Keep only recent terminal jobs in RAM; cached files live on workers, not Core.
   self.jobs={k:j for k,j in self.jobs.items() if j.get('task') and not j['task'].done()}
   job=dict(result,workload=workload,duration=clip['end']-clip['start']);self.jobs[key]=job
   notice='desk_camera_playback_'+result['id']
   persistent_notification.async_create(self.hass,'Preparing the clip on '+result['node']+'. The player will wait and start when it is ready. Preparation can take close to the clip duration on this camera.',title='Preparing camera recording',notification_id=notice)
   job['task']=self._create_background_task(self.wait(job,notice),entry)
   return self.play(workload,result)
  except Unresolvable:raise
  except Exception as err:raise Unresolvable('Camera worker unavailable or busy. Select a completed clip of at most thirty minutes and retry when ready.') from err
 def play(self,workload,result):
  path='/api/desk_cluster/recording/'+workload+'/'+result['node']+'/'+result['id']
  return PlayMedia(async_sign_path(self.hass,path,datetime.timedelta(hours=2)),'video/mp4')
 async def wait(self,job,notice):
  try:
   for _ in range(min(384,max(42,(job['duration']+120)//5))):
    await asyncio.sleep(5)
    status=await bridge('camera_status',value={k:job[k] for k in ('workload','node','id')})
    if status['state']=='ready':
     persistent_notification.async_create(self.hass,'The recording is ready on '+job['node']+'. It is available in Media → Tapo: Recordings.',title='Camera recording ready',notification_id=notice);return
    if status['state'] in ('failed','missing'):raise ValueError()
   raise TimeoutError()
  except asyncio.CancelledError:raise
  except Exception:persistent_notification.async_create(self.hass,'The worker changed or could not finish. Select the clip again to retry; no automatic retry was started.',title='Camera recording interrupted',notification_id=notice)


def install(hass):
 """Adapt the pinned upstream platform factory, retaining its single native URI.

 No vendor file or entity implementation is changed. HA's lazy platform loader
 calls this factory after setup; both old bookmarks and new cards use Tapo URIs.
 """
 from custom_components.tapo_control import media_source as native
 original=native.async_get_media_source
 native.async_get_media_source=async_get_media_source
 from homeassistant.const import EVENT_HOMEASSISTANT_STOP
 hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP,lambda _:setattr(native,'async_get_media_source',original))
