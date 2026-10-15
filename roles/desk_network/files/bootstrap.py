"""Brief CPU budget on an idle node; pinned cgroup FD always returns to 0.5 core."""
import errno,math,os,pathlib,re,threading,time
def quiet(local):
 activity=local.get('activity') if isinstance(local,dict) else None
 return isinstance(activity,dict) and all(type(activity.get(k)) is int and activity[k]==0 for k in ('asr','llm','tts'))
def idle(local):
 cpu=local.get('cpu') if isinstance(local,dict) else None
 return quiet(local) and type(cpu) in (int,float) and math.isfinite(cpu) and 0<=cpu<50
class Budget:
 def __init__(self,clock=time.monotonic,root='/sys/fs/cgroup',proc='/proc'):
  self.clock=clock;self.root=pathlib.Path(root);self.proc=pathlib.Path(proc);self.lock=threading.Lock();self.fd=None;self.until=0
 def start(self,record,local,fresh):
  if not fresh or not idle(local):return False
  pid,start=record;process=self.proc/str(pid);fd=None
  try:
   if process.joinpath('stat').read_text().split(') ',1)[1].split()[19]!=start:return False
   group=process.joinpath('cgroup').read_text().strip()
   match=re.fullmatch(r'0::(/(?:machine.slice/)?libpod-[a-f0-9]{64}\.scope(?:/container)?)',group)
   if not match:return False
   group_path=match[1].removesuffix('/container')
   for path in dict.fromkeys((group_path,match[1])):
    candidate=os.open(self.root/path.lstrip('/')/'cpu.max',os.O_RDWR|os.O_CLOEXEC)
    if os.read(candidate,128).split()==[b'50000',b'100000']:fd=candidate;break
    os.close(candidate)
   if fd is None:return False
   if process.joinpath('stat').read_text().split(') ',1)[1].split()[19]!=start:os.close(fd);return False
   with self.lock:
    if self.fd is not None:os.close(fd);return False
    os.lseek(fd,0,os.SEEK_SET);os.write(fd,b'100000 100000');self.fd=fd;self.until=self.clock()+60
   return True
  except (OSError,ValueError,IndexError):
   if fd is not None:os.close(fd)
   return False
 def settle(self,force=False):
  with self.lock:
   if self.fd is None or not(force or self.clock()>=self.until):return
   try:os.lseek(self.fd,0,os.SEEK_SET);os.write(self.fd,b'50000 100000')
   except OSError as error:
    if error.errno not in (errno.ENOENT,errno.ENODEV,errno.EBADF):return # Retry next watchdog tick.
   try:os.close(self.fd)
   except OSError:pass
   self.fd=None
 def close(self):
  with self.lock:
   if self.fd is not None:os.close(self.fd);self.fd=None
