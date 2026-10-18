"""Bounded root-local IPC; no SDK imports or media bytes in the controller."""
import asyncio,json
SOCKET='/run/desk-camera-thumbnails/worker.sock'
async def request(value,timeout=5):
 async with asyncio.timeout(timeout):
  reader,writer=await asyncio.open_unix_connection(SOCKET,limit=65536)
  try:
   writer.write(json.dumps(value,separators=(',',':')).encode()+b'\n');await writer.drain()
   raw=await reader.readline()
   if len(raw)>(33000 if value.get('action')=='recording_v1' else 4096):raise ValueError('Media worker response size')
   response=json.loads(raw)
   if response.get('ok') is not True:raise ValueError('Warm media worker unavailable')
   return response.get('value',{})
  finally:writer.close();await writer.wait_closed()
