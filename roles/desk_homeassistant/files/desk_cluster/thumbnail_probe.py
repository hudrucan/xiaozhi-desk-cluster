"""One explicit native-session JPEG capability check; no preload or image export."""
import asyncio,io,time
from contextlib import aclosing

async def check(hass,device,date):
 from pytapo.media_stream.snapshot import getRecordingSnapshots
 from PIL import Image
 started=time.monotonic();controller=device['controller'];stage='index'
 try:
  async with asyncio.timeout(45):
   records=await hass.async_add_executor_job(controller.getRecordings,date)
   ranges=[(int(v['startTime']),int(v['endTime'])) for row in records for v in row.values() if isinstance(v,dict) and 'startTime' in v and 'endTime' in v]
   ranges=[(a,b) for a,b in ranges if a<b]
   if not ranges:return {'status':'no_recordings'}
   stage='events'
   response=await hass.async_add_executor_job(controller.executeFunction,'searchDetectionList',{'playback':{'search_detection_list':{'start_index':0,'end_index':127,'channel':0,'start_time':min(a for a,b in ranges),'end_time':max(b for a,b in ranges)}}})
   events=response['playback']['search_detection_list']
   starts=sorted({int(e['start_time']) for e in events if isinstance(e,dict) and 'start_time' in e and any(a<=int(e['start_time'])<b for a,b in ranges)})
   if not starts:return {'status':'no_events','event_page_count':len(events)}
   stage='snapshot';at=time.monotonic()
   async with aclosing(getRecordingSnapshots(controller,[starts[0]],timeout=8)) as snapshots:
    image=await anext(snapshots)
   elapsed=round(time.monotonic()-at,3)
   if not image:return {'status':'no_jpeg','event_page_count':len(events),'snapshot_seconds':elapsed}
   if not isinstance(image,bytes) or not 4<len(image)<=2*1024*1024:raise ValueError('Invalid snapshot size')
   stage='validate'
   with Image.open(io.BytesIO(image)) as picture:
    if picture.format!='JPEG' or not all(0<n<=4096 for n in picture.size):raise ValueError('Invalid snapshot format')
    width,height=picture.size;picture.verify()
   return {'status':'ready','width':width,'height':height,'bytes':len(image),'event_page_count':len(events),'snapshot_seconds':elapsed,'total_seconds':round(time.monotonic()-started,3)}
 except asyncio.CancelledError:raise
 except Exception as error:return {'status':'error','stage':stage,'error_type':type(error).__name__}
