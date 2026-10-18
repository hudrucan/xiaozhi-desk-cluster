"""One bounded native pytapo download/remux job; never starts an HA Core."""
import asyncio,json,pathlib,os,time
from playback import session_handle
ROOT=pathlib.Path('/output');STAGE='imports'
SESSION_EXPIRED='The camera recording session expired. Reopen its date folder and retry.'
class MediaSessionExpired(ValueError):
 def __init__(self):super().__init__(SESSION_EXPIRED)
class CoreMediaSession:
 """A worker may use a supplied handle, but never allocate or refresh one."""
 def bind_media_session(self,value):
  self._core_media_session=session_handle(value);self._core_media_expired=False
  self.userID=self._core_media_session['user_id'];self.playerID=self._core_media_session['player_id']
 def expire_media_session(self):
  self._core_media_expired=True;raise MediaSessionExpired()
 def check_media_session(self):
  v=getattr(self,'_core_media_session',None)
  if not v or getattr(self,'_core_media_expired',False) or type(self.userID) is not type(v['user_id']) or self.userID!=v['user_id'] or self.playerID!=v['player_id']:self.expire_media_session()
  return v['user_id']
 def getUserID(self,forceReload=False,retry=False):
  if forceReload or retry:self.expire_media_session()
  return self.check_media_session()
 def executeFunction(self,method,*args,**kwargs):
  if method=='getUserID':self.expire_media_session()
  return super().executeFunction(method,*args,**kwargs)
def create_client(*args,**kwargs):
 # Import only in the SDK process. The class guard also covers construction.
 from pytapo import Tapo
 class WorkerTapo(CoreMediaSession,Tapo):pass
 return WorkerTapo(*args,**kwargs)
def failure(error,client=None):
 if isinstance(error,MediaSessionExpired) or getattr(client,'_core_media_expired',False):return {'reason':'session_expired','error':SESSION_EXPIRED}
 return {'error':'Recording preparation failed; retry when the worker is ready.'}
def save(value):
 p=ROOT/'progress.tmp';p.write_text(json.dumps(value));os.replace(p,ROOT/'progress.json')
async def run():
 global STAGE
 from pytapo.media_stream.downloader import Downloader
 c=json.loads(pathlib.Path('/input/credentials.json').read_bytes());job=json.loads((ROOT/'request.json').read_bytes())
 secret=c[job['workload']];password=secret['cloud_password']
 STAGE='camera_connection';save({'state':'preparing','stage':STAGE})
 tapo=await asyncio.to_thread(create_client,secret['ip_address'],'admin',password,password,reuseSession=False,retryStok=False,controlPort=secret.get('control_port',443),isKLAP=False)
 session=json.loads(pathlib.Path('/input/session.json').read_bytes())
 options=json.loads((ROOT/'options.json').read_bytes()) if (ROOT/'options.json').is_file() else {}
 try:await execute(tapo,job,session,ROOT,options,save,asyncio.to_thread)
 except Exception:
  if getattr(tapo,'_core_media_expired',False):raise MediaSessionExpired() from None
  raise

async def execute(tapo,job,session,root,options,save,control,permitted=None):
 from pytapo.media_stream.downloader import Downloader
 # Owner-mode allocation and legacy Core-handle binding share this admission.
 from recording_owner import bind_session
 await bind_session(tapo,session,control)
 save({'state':'preparing','stage':'time_correction'})
 if permitted is not None and not permitted():raise ValueError('Owner changed')
 correction=await control(tapo.getTimeCorrection)
 save({'state':'preparing','stage':'recording_index'})
 recordings=await control(tapo.getRecordings,job['date'])
 tapo.check_media_session()
 if not any(int(v.get('startTime',0))<=job['start']<job['end']<=int(v.get('endTime',0)) for record in recordings for v in record.values() if isinstance(v,dict)):raise ValueError('Clip not in camera index')
 if permitted is not None and not permitted():raise ValueError('Owner changed')
 def progress(v):
  if isinstance(v,dict):save({'state':'preparing','progress':v.get('progress',0),'total':v.get('total',0)})
 save({'state':'preparing','stage':'download'})
 if options.get('format')=='hls':
  from hls_worker import package
  save({'state':'preparing','stage':'hls'});await package(tapo,job,correction,root,options['generation'],save);tapo.check_media_session();return
 downloader=Downloader(tapo,job['start'],job['end'],correction,str(root)+'/',0,None,None,'clip.mp4',stall_timeout=15)
 result=await downloader.downloadFile(progress)
 tapo.check_media_session()
 p=root/'clip.mp4'
 if result.get('currentAction')=='Recording in progress' or not p.is_file() or not 1024<p.stat().st_size<=256*1024*1024:raise ValueError('Clip unavailable or too large')
 os.replace(p,root/'ready.mp4');save({'state':'ready','bytes':(root/'ready.mp4').stat().st_size,'mode':downloader.method})

if __name__=='__main__':
 try:asyncio.run(asyncio.wait_for(run(),1900))
 except BaseException as err:save(dict(state='failed',stage=STAGE,error_type=type(err).__name__,**failure(err)));raise SystemExit(1)
