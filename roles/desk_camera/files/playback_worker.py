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
 # Core is the only allocator; do not ask the camera for a competing user ID.
 session=json.loads(pathlib.Path('/input/session.json').read_bytes());tapo.userID=session['user_id'];tapo.playerID=session['player_id']
 correction=await asyncio.to_thread(tapo.getTimeCorrection)
 STAGE='recording_index';save({'state':'preparing','stage':STAGE})
 recordings=await asyncio.to_thread(tapo.getRecordings,job['date'])
 if not any(int(v.get('startTime',0))<=job['start']<job['end']<=int(v.get('endTime',0)) for record in recordings for v in record.values() if isinstance(v,dict)):raise ValueError('Clip not in camera index')
 def progress(v):
  if isinstance(v,dict):save({'state':'preparing','progress':v.get('progress',0),'total':v.get('total',0)})
 STAGE='download';save({'state':'preparing','stage':STAGE})
 downloader=Downloader(tapo,job['start'],job['end'],correction,str(ROOT)+'/',0,None,None,'clip.mp4',stall_timeout=15)
 result=await downloader.downloadFile(progress)
 p=ROOT/'clip.mp4'
 if result.get('currentAction')=='Recording in progress' or not p.is_file() or not 1024<p.stat().st_size<=256*1024*1024:raise ValueError('Clip unavailable or too large')
 os.replace(p,ROOT/'ready.mp4');save({'state':'ready','bytes':(ROOT/'ready.mp4').stat().st_size,'mode':downloader.method})
if __name__=='__main__':
 try:asyncio.run(asyncio.wait_for(run(),1900))
 except BaseException as err:save({'state':'failed','stage':STAGE,'error_type':type(err).__name__,'error':'Recording preparation failed; retry after checking camera/worker status.'});raise SystemExit(1)
