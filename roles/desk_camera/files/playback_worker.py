"""One bounded native pytapo download/remux job; never starts an HA Core."""
import asyncio,json,pathlib,os,time
ROOT=pathlib.Path('/output');STAGE='imports'
def save(value):
 p=ROOT/'progress.tmp';p.write_text(json.dumps(value));os.replace(p,ROOT/'progress.json')
async def run():
 global STAGE
 from pytapo import Tapo
 from pytapo.media_stream.downloader import Downloader
 c=json.loads(pathlib.Path('/input/credentials.json').read_bytes());job=json.loads((ROOT/'request.json').read_bytes())
 secret=c[job['workload']];password=secret['cloud_password']
 STAGE='camera_connection';save({'state':'preparing','stage':STAGE})
 tapo=await asyncio.to_thread(Tapo,secret['ip_address'],'admin',password,password,reuseSession=False,retryStok=False,controlPort=secret.get('control_port',443),isKLAP=False)
 session=json.loads(pathlib.Path('/input/session.json').read_bytes())
 options=json.loads((ROOT/'options.json').read_bytes()) if (ROOT/'options.json').is_file() else {}
 await execute(tapo,job,session,ROOT,options,save,asyncio.to_thread)

async def execute(tapo,job,session,root,options,save,control,permitted=None):
 from pytapo.media_stream.downloader import Downloader
 # Core is the only allocator; do not ask the camera for a competing user ID.
 tapo.userID=session['user_id'];tapo.playerID=session['player_id']
 save({'state':'preparing','stage':'time_correction'})
 if permitted is not None and not permitted():raise ValueError('Owner changed')
 correction=await control(tapo.getTimeCorrection)
 save({'state':'preparing','stage':'recording_index'})
 recordings=await control(tapo.getRecordings,job['date'])
 if not any(int(v.get('startTime',0))<=job['start']<job['end']<=int(v.get('endTime',0)) for record in recordings for v in record.values() if isinstance(v,dict)):raise ValueError('Clip not in camera index')
 if permitted is not None and not permitted():raise ValueError('Owner changed')
 def progress(v):
  if isinstance(v,dict):save({'state':'preparing','progress':v.get('progress',0),'total':v.get('total',0)})
 save({'state':'preparing','stage':'download'})
 if options.get('format')=='hls':
  from hls_worker import package
  save({'state':'preparing','stage':'hls'});await package(tapo,job,correction,root,options['generation'],save);return
 downloader=Downloader(tapo,job['start'],job['end'],correction,str(root)+'/',0,None,None,'clip.mp4',stall_timeout=15)
 result=await downloader.downloadFile(progress)
 p=root/'clip.mp4'
 if result.get('currentAction')=='Recording in progress' or not p.is_file() or not 1024<p.stat().st_size<=256*1024*1024:raise ValueError('Clip unavailable or too large')
 os.replace(p,root/'ready.mp4');save({'state':'ready','bytes':(root/'ready.mp4').stat().st_size,'mode':downloader.method})

if __name__=='__main__':
 try:asyncio.run(asyncio.wait_for(run(),1900))
 except BaseException as err:save({'state':'failed','stage':STAGE,'error_type':type(err).__name__,'error':'Recording preparation failed; retry after checking camera/worker status.'});raise SystemExit(1)
