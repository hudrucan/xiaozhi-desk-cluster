"""Core readiness and an authenticated, admin-only manual config sync panel."""
import asyncio,json,logging,pathlib
import probatio
from aiohttp import web
from homeassistant.components import backup,panel_custom,websocket_api,persistent_notification,frontend
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from .reminder import PendingReminder
from homeassistant.components.http import HomeAssistantView,StaticPathConfig

_LOGGER=logging.getLogger(__name__)
DOMAIN='desk_cluster'
SOCKET='/run/desk-cluster/bridge.sock'

async def bridge(action,**data):
 async with asyncio.timeout(10):
  reader,writer=await asyncio.open_unix_connection(SOCKET,limit=65536)
  try:
   writer.write(json.dumps(dict(action=action,**data)).encode()+b'\n');await writer.drain()
   raw=await reader.readline()
   if len(raw)>65536:raise ValueError('Cluster response too large')
   result=json.loads(raw)
   if result.get('ok') is not True:raise ValueError('Cluster operation unavailable')
   return result['value']
  finally:writer.close();await writer.wait_closed()

class Readiness(HomeAssistantView):
 url='/api/desk_cluster/ready'
 name='api:desk_cluster:ready'
 requires_auth=False
 def __init__(self,hass):self._desk_hass=hass
 async def get(self,request):
  ready=self._desk_hass.is_running
  return web.json_response({'ready':ready},status=200 if ready else 503)

async def sync_config(hass,state,ticket):
 manager=backup.async_get_manager(hass);created=None;imported=False;terminal=False
 try:
  # Native registry saves are delayed. Do not alter Store internals or fire a
  # shutdown event to flush them. A later disk write remains visibly pending.
  state.update(stage='preparing',error=None)
  await asyncio.sleep(15)
  state['stage']='creating_snapshot'
  created=await manager.async_create_backup(agent_ids=['backup.local'],include_addons=None,
   include_all_addons=False,include_database=True,include_folders=None,
   include_homeassistant=True,name='Desk Cluster sync',password=None,
   extra_metadata={'desk_cluster_sync':ticket})
  local=manager.local_backup_agents['backup.local']
  await local.async_get_backup(created.backup_job_id)
  path=local.get_backup_path(created.backup_job_id)
  state['stage']='replicating'
  await bridge('import',ticket=ticket,filename=path.name,backup_id=created.backup_job_id)
  imported=True
  for _ in range(180):
   status=await bridge('status');operation=status.get('operation',{})
   if operation.get('state') in ('complete','failed'):
    terminal=True
    if operation['state']=='failed':raise ValueError('Replication failed')
    state.update(stage='complete',error=None);return
   await asyncio.sleep(2)
  raise TimeoutError('Replication still pending')
 except Exception:
  state.update(stage='failed',error='Sync did not complete. Local changes remain; check cluster status before retrying.')
  _LOGGER.warning('Cluster config sync did not complete; private details withheld')
  if not imported:
   try:await bridge('abort',ticket=ticket)
   except Exception:pass
 finally:
  # Only remove the exact backup this operation created, after the importer is
  # finished. Never delete a user backup or a possibly still-open input archive.
  if created and (terminal or not imported):
   try:await manager.async_delete_backup(created.backup_job_id,agent_ids=['backup.local'])
   except Exception:_LOGGER.warning('Cluster temporary backup cleanup deferred')
  state['task']=None

@websocket_api.websocket_command({probatio.Required('type'):'desk_cluster/recording_session',probatio.Required('value'):dict})
@websocket_api.async_response
async def recording_session_command(hass,connection,msg):
 try:
  source=hass.data[DOMAIN]['recording_source']
  value=await source.viewer_manager().action(connection.user.id,msg['value'])
  connection.send_result(msg['id'],value)
 except asyncio.CancelledError:raise
 except Exception:connection.send_error(msg['id'],'recording_unavailable','Recording closed or worker unavailable; close and retry.')

@websocket_api.websocket_command({probatio.Required('type'):'desk_cluster/thumbnail_scope',probatio.Required('value'):dict})
@websocket_api.async_response
async def thumbnail_scope_command(hass,connection,msg):
 try:
  source=hass.data[DOMAIN]['recording_source']
  result=await source.thumbnail_manager().focus(connection.user.id,msg['value'])
  connection.send_result(msg['id'],result)
 except Exception:connection.send_error(msg['id'],'thumbnail_scope_unavailable','Folder preview unavailable; reopen the folder.')

@websocket_api.require_admin
@websocket_api.websocket_command({probatio.Required('type'):'desk_cluster/status'})
@websocket_api.async_response
async def status_command(hass,connection,msg):
 try:
  status=await bridge('status');state=hass.data[DOMAIN]
  status['preparation']={k:state[k] for k in ('stage','error')}
  connection.send_result(msg['id'],status)
 except Exception:connection.send_error(msg['id'],'cluster_unavailable','Cluster status unavailable; retry shortly.')

@websocket_api.require_admin
@websocket_api.websocket_command({probatio.Required('type'):'desk_cluster/sync'})
@websocket_api.async_response
async def sync_command(hass,connection,msg):
 state=hass.data[DOMAIN]
 if state['task'] is not None:
  connection.send_error(msg['id'],'busy','A config sync is already running.');return
 try:
  if not hass.is_running:raise ValueError('Core is not ready')
  reservation=await bridge('begin')
  state.update(stage='preparing',error=None)
  state['task']=hass.async_create_background_task(sync_config(hass,state,reservation['ticket']),'desk_cluster_sync')
  connection.send_result(msg['id'],{'accepted':True})
 except Exception:connection.send_error(msg['id'],'sync_unavailable','Sync unavailable. Check that Core and the cluster are ready.')

@websocket_api.websocket_command({probatio.Required('type'):'desk_cluster/cameras'})
@websocket_api.async_response
async def cameras_command(hass,connection,msg):
 try:connection.send_result(msg['id'],await bridge('cameras'))
 except Exception:connection.send_error(msg['id'],'camera_unavailable','Camera placement unavailable; retry shortly.')

@websocket_api.websocket_command({probatio.Required('type'):'desk_cluster/camera_storage'})
@websocket_api.async_response
async def camera_storage_command(hass,connection,msg):
 from .camera_storage import camera_storage
 connection.send_result(msg['id'],camera_storage(hass))

async def pending_reminders(hass):
 reminder=PendingReminder()
 while True:
  try:
   value=await bridge('status');action=reminder.observe(value)
   if action=='notify':
    revision=value.get('checkpoint_revision')
    persistent_notification.async_create(hass,
     'Changes are saved on the active Core, but have not been synced to the recovery copy. '
     f'If that node fails, recovery uses revision r{revision}. '
     '[Open Cluster to sync config](/desk-cluster).',
     title='Configuration changes not synced',notification_id='desk_cluster_sync_pending')
   elif action=='dismiss':persistent_notification.async_dismiss(hass,'desk_cluster_sync_pending')
  except asyncio.CancelledError:raise
  except Exception:pass  # Keep an existing warning when the bridge is temporarily unavailable.
  await asyncio.sleep(5)

async def async_setup(hass,config):
 # Pinned native backup has a module-level exclusion list. Add only this
 # deployment's disposable playback cache, keeping upstream files unchanged.
 from homeassistant.components.backup.const import EXCLUDE_FROM_BACKUP
 for pattern in ('www/tapo_control','www/tapo_control/**'):
  if pattern not in EXCLUDE_FROM_BACKUP:EXCLUDE_FROM_BACKUP.append(pattern)
 hass.data[DOMAIN]={'task':None,'stage':'idle','error':None}
 task=hass.async_create_background_task(pending_reminders(hass),'desk_cluster_pending_reminder')
 hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP,lambda _:task.cancel())
 from .camera_recordings import install as install_recordings
 install_recordings(hass)
 from .recording_view import RecordingView,HlsRecordingView
 hass.http.register_view(RecordingView(hass))
 hass.http.register_view(HlsRecordingView(hass))
 from .recording_sessions import RecordingSessionView
 hass.http.register_view(RecordingSessionView(hass))
 from .recording_thumbnails import ThumbnailView,ThumbnailScopeView
 hass.http.register_view(ThumbnailView(hass))
 hass.http.register_view(ThumbnailScopeView(hass))
 hass.http.register_view(Readiness(hass))
 await hass.http.async_register_static_paths([StaticPathConfig('/desk-cluster-assets/'+name,str(pathlib.Path(__file__).parent/name),False) for name in ('panel.js','camera-card.js','thumbnail-focus.js','recording-loading.svg')])
 frontend.add_extra_js_url(hass,'/desk-cluster-assets/camera-card.js?v=1.3.0')
 frontend.add_extra_js_url(hass,'/desk-cluster-assets/thumbnail-focus.js?v=2.2.0')
 websocket_api.async_register_command(hass,thumbnail_scope_command)
 websocket_api.async_register_command(hass,recording_session_command)
 websocket_api.async_register_command(hass,cameras_command)
 websocket_api.async_register_command(hass,camera_storage_command)
 websocket_api.async_register_command(hass,status_command)
 websocket_api.async_register_command(hass,sync_command)
 await panel_custom.async_register_panel(hass,frontend_url_path='desk-cluster',
  webcomponent_name='desk-cluster-panel',sidebar_title='Cluster',sidebar_icon='mdi:server-network',
  module_url='/desk-cluster-assets/panel.js?v=1.2.0',require_admin=True)
 return True
