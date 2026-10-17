"""Signed HTTP to a root-only warm camera worker. No camera I/O in controller."""
import hashlib,json,pathlib,re,socket,threading,time
from playback import session_handle
SOCKET='/run/desk-camera-thumbnails/worker.sock'
MAX_IMAGE=2*1024*1024

def rpc(value,timeout=45):
 with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
  sock.settimeout(timeout);sock.connect(SOCKET);sock.sendall(json.dumps(value).encode()+b'\n')
  with sock.makefile('rb') as f:
   raw=f.readline(4097)
   if len(raw)>4096:raise ValueError('Worker header')
   header=json.loads(raw)
   if header.get('ok') is not True:raise ValueError('Thumbnail worker unavailable')
   size=header.get('bytes',0)
   if type(size) is not int or not 0<=size<=MAX_IMAGE:raise ValueError('Worker size')
   data=f.read(size)
   if len(data)!=size:raise ValueError('Incomplete image')
   return header,data

class Fetcher:
 def __init__(self):self.lock=threading.Lock();self.completed=0;self.cancelled=0;self.failed=0;self.last=None
 def request(self,agents,scope,data):
  if set(scope)!={'workload','id','source','generation','digest'} or scope['source'] not in ('fetch','cancel'):raise ValueError('Fetch scope')
  if not re.fullmatch('[a-f0-9]{32}',scope['id']) or not re.fullmatch('[a-f0-9]{32}',scope['generation']) or hashlib.sha256(data).hexdigest()!=scope['digest']:raise ValueError('Fetch binding')
  agent=next(a for a in agents if a.c['workload']==scope['workload'])
  if scope['source']=='cancel':
   if data:raise ValueError('Cancel body')
   rpc({'action':'cancel','id':scope['id']},15)
   # Acknowledge after the original HTTP handler has consumed its IPC reply
   # and released admission, not merely after the SDK task has stopped.
   if not self.lock.acquire(timeout=5):raise ValueError('Cancellation still draining')
   self.lock.release();self.cancelled+=1;return b'', 'cancelled'
  if not agent.owned() or not agent.ready() or agent.generation!=scope['generation'] or any(a.playback.active() for a in agents):raise ValueError('Worker busy or owner changed')
  from thumbnail_decode import memory_ok
  if not memory_ok() or not self.lock.acquire(timeout=2):raise ValueError('Worker admission')
  started=time.monotonic()
  try:
   value=json.loads(data)
   if set(value)!={'clip','session'}:raise ValueError('Fetch body')
   clip=value['clip'];session=session_handle(value['session'])
   if set(clip)!={'date','start','end'} or not re.fullmatch('[0-9]{8}',clip['date']) or type(clip['start']) is not int or type(clip['end']) is not int or not 1500000000<=clip['start']<clip['end']<=2200000000 or clip['end']-clip['start']>86400:raise ValueError('Clip range')
   import datetime
   day=datetime.datetime.strptime(clip['date'],'%Y%m%d').replace(tzinfo=datetime.timezone.utc).timestamp()
   if not day-86400<=clip['start']<=day+172800:raise ValueError('Clip date')
   header,image=rpc(dict(value,session=session,action='fetch',id=scope['id'],workload=scope['workload'],generation=scope['generation']))
   if not agent.owned() or agent.generation!=scope['generation']:raise ValueError('Owner changed')
   if not image.startswith(b'\xff\xd8') or header.get('kind') not in ('event','keyframe'):raise ValueError('Worker image')
   self.completed+=1;self.last={'node':agent.node,'workload':scope['workload'],'kind':header['kind'],'bytes':len(image),'seconds':round(time.monotonic()-started,3)}
   return image,header['kind']
  except Exception as e:self.failed+=1;self.last={'workload':scope['workload'],'error_type':type(e).__name__};raise
  finally:self.lock.release()
 def public(self):return {'completed':self.completed,'cancelled':self.cancelled,'failed':self.failed,'last':self.last,'busy':self.lock.locked()}
