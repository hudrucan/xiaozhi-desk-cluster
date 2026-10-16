"""Core readiness and an authenticated, admin-only manual config sync panel."""
import asyncio,json,logging,pathlib
import probatio
from aiohttp import web
from homeassistant.components import backup,panel_custom,websocket_api
from homeassistant.components.http import HomeAssistantView,StaticPathConfig

_LOGGER=logging.getLogger(__name__)
DOMAIN='desk_cluster'
SOCKET='/run/desk-cluster/bridge.sock'

async def bridge(action,**data):
 async with asyncio.timeout(5):
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

async def async_setup(hass,config):
 # Pinned native backup has a module-level exclusion list. Add only this
 # deployment's disposable playback cache, keeping upstream files unchanged.
 from homeassistant.components.backup.const import EXCLUDE_FROM_BACKUP
 for pattern in ('www/tapo_control','www/tapo_control/**'):
  if pattern not in EXCLUDE_FROM_BACKUP:EXCLUDE_FROM_BACKUP.append(pattern)
 hass.data[DOMAIN]={'task':None,'stage':'idle','error':None}
 hass.http.register_view(Readiness(hass))
 await hass.http.async_register_static_paths([StaticPathConfig('/desk-cluster-assets/panel.js',str(pathlib.Path(__file__).parent/'panel.js'),False)])
 websocket_api.async_register_command(hass,status_command)
 websocket_api.async_register_command(hass,sync_command)
 await panel_custom.async_register_panel(hass,frontend_url_path='desk-cluster',
  webcomponent_name='desk-cluster-panel',sidebar_title='Cluster',sidebar_icon='mdi:server-network',
  module_url='/desk-cluster-assets/panel.js?v=1.1.0',require_admin=True)
 return True
