"""Authenticated, bounded streaming proxy; SDK/ffmpeg and cache stay on workers."""
import asyncio,aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from . import bridge
class RecordingView(HomeAssistantView):
 url='/api/desk_cluster/recording/{workload}/{node}/{clip}'
 name='api:desk_cluster:recording'
 def __init__(self,hass):self._desk_hass=hass
 async def get(self,request,workload,node,clip):
  try:
   for _ in range(640):
    status=await bridge('camera_status',value={'workload':workload,'node':node,'id':clip})
    if status.get('state')=='ready':break
    if status.get('state') in ('failed','missing'):raise ValueError('Preparation stopped')
    if request.transport is None or request.transport.is_closing():raise asyncio.CancelledError()
    await asyncio.sleep(3)
   else:raise TimeoutError()
   ticket=await bridge('camera_artifact',value={'workload':workload,'node':node,'id':clip})
  except Exception:raise web.HTTPNotFound() from None
  headers={'X-Desk-Artifact':ticket['authorization']}
  if 'Range' in request.headers:headers['Range']=request.headers['Range']
  session=async_get_clientsession(self._desk_hass)
  try:
   async with session.get(ticket['url'],headers=headers,allow_redirects=False,timeout=aiohttp.ClientTimeout(total=None,sock_connect=3,sock_read=15)) as source:
    if source.status not in (200,206):raise web.HTTPNotFound()
    length=int(source.headers.get('Content-Length','0'))
    if not 0<length<=256*1024*1024:raise web.HTTPNotFound()
    result=web.StreamResponse(status=source.status,headers={key:source.headers[key] for key in ('Content-Type','Content-Length','Content-Range','Accept-Ranges') if key in source.headers})
    result.headers['Cache-Control']='private, no-store';result.headers['X-Content-Type-Options']='nosniff'
    await result.prepare(request);sent=0
    async for chunk in source.content.iter_chunked(65536):
     sent+=len(chunk)
     if sent>length:raise ConnectionError('Clip response exceeded declared length')
     await result.write(chunk)
    await result.write_eof();return result
  except (aiohttp.ClientError,TimeoutError):raise web.HTTPServiceUnavailable() from None
