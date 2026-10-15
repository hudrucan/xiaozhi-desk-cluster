#!/usr/bin/env python3
"""Pinned native import preloader; only leased children may open the live database.

The host writes a root-owned, read-only mounted deadline. The single-threaded
parent never runs main(), serves HTTP or retains a SQLite handle. Each activation
forks pristine imports, starts fresh PHP/cron, and all three process groups are
killed on expiry. No process/database snapshot is restored into a running child.
"""
import ctypes,json,math,os,pathlib,re,signal,subprocess,sys,time,threading
CONTROL=pathlib.Path('/run/desk-network-warm/control.json')
STATUS=pathlib.Path('/tmp/desk-warm-status.json')
def clock():return time.clock_gettime(time.CLOCK_BOOTTIME)
def request(path=CONTROL,now=None):
 try:
  if path.stat().st_size>1024:return None
  v=json.loads(path.read_text());now=clock() if now is None else now
  if not isinstance(v,dict) or not re.fullmatch('[a-f0-9]{32}',v.get('generation','')):return None
  until=v.get('until')
  if type(until) not in (int,float) or not math.isfinite(until) or not now<until<=now+11:return None
  return v['generation']
 except (OSError,ValueError,TypeError):return None

def publish(value,path=STATUS):
 tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value));os.chmod(tmp,0o600);os.replace(tmp,path)

def assert_pristine(proc=pathlib.Path('/proc')):
 if threading.active_count()!=1 or len(list((proc/'self/task').iterdir()))!=1:raise RuntimeError('Preload must be single threaded')
 for fd in (proc/'self/fd').iterdir():
  try:target=os.readlink(fd)
  except FileNotFoundError:continue
  if re.search(r'/app\.db(?:-wal|-shm)?(?: \(deleted\))?$',target):raise RuntimeError('Preload retained a live SQLite handle')

def preload():
 sys.path[:0]=['/app','/app/server','/app/server/plugins']
 # The upstream import logs eagerly start an asynchronous writer. Silence only
 # the preload phase; children inherit the original logger with an empty queue.
 import logger
 original=logger.file_print;logger.file_print=lambda *args:None
 try:import server.__main__ as native
 finally:logger.file_print=original
 assert_pristine()
 return native.main

def live_group(group,proc=pathlib.Path('/proc')):
 for entry in proc.iterdir():
  if not entry.name.isdigit():continue
  try:
   fields=(entry/'stat').read_text().split(') ',1)[1].split()
   if int(fields[2])==group and fields[0] not in ('Z','X'):return True
  except (OSError,ValueError,IndexError):pass
 return False

def reap():
 while True:
  try:
   if os.waitpid(-1,os.WNOHANG)[0]==0:return
  except ChildProcessError:return

def stop(groups):
 for group in groups:
  try:os.killpg(group,signal.SIGKILL)
  except ProcessLookupError:pass
 deadline=time.monotonic()+1
 while True:
  reap()
  if not any(live_group(group) for group in groups):return
  if time.monotonic()>deadline:raise RuntimeError('Writer group did not stop')
  time.sleep(.02)

def clean_api(root=pathlib.Path('/tmp/api')):
 # Clear derived memory files only, after all database writers have exited.
 # Fresh main() will regenerate them from the restored certified checkpoint.
 for path in root.iterdir():
  if path.is_file() and path.suffix=='.json':path.unlink()
 restart=pathlib.Path('/tmp/backend_restart_pending');restart.unlink(missing_ok=True)

def launch(native_main):
 assert_pristine()
 pid=os.fork()
 if pid==0:
  try:
   os.setpgid(0,0)
   signal.signal(signal.SIGTERM,signal.SIG_DFL);signal.signal(signal.SIGINT,signal.SIG_DFL)
   native_main()
  finally:os._exit(1)
 try:os.setpgid(pid,pid)
 except (ProcessLookupError,PermissionError):pass
 return pid

def main():
 # Reap orphaned plugin/PHP descendants after stopping their entire groups.
 if ctypes.CDLL(None).prctl(36,1,0,0,0)!=0:raise RuntimeError('Subreaper unavailable')
 stopped=[False]
 for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stopped.__setitem__(0,True))
 started=clock();publish({'state':'preloading','parent':os.getpid()})
 native_main=preload();seconds=round(clock()-started,3)
 groups=[];generation=None;php=cron=None;activations=0;failed=None
 def status(state):
  publish({'state':state,'generation':generation,'parent':os.getpid(),'groups':groups,'preload_seconds':seconds,'activations':activations})
 status('inactive')
 try:
  while not stopped[0]:
   desired=request()
   # A crashing native child must not be restarted repeatedly under the same
   # activation. Withdraw readiness and let the controller replace the canary.
   if groups and (not live_group(groups[0]) or php.poll() is not None or cron.poll() is not None):
    failed=generation;desired=None
   if desired!=generation or (desired is None and groups):
    if groups:stop(groups);groups=[];php=cron=None;clean_api()
    generation=None;status('inactive')
    if desired and desired!=failed and request()==desired:
     clean_api();groups.append(launch(native_main))
     php_args=['/usr/sbin/php-fpm83','-y',os.environ['PHP_FPM_CONFIG_FILE'],'-F']
     if os.getuid()==0:php_args.append('-R')
     php=subprocess.Popen(php_args,start_new_session=True);groups.append(php.pid)
     cron=subprocess.Popen(['supercronic','--quiet',os.environ['SYSTEM_SERVICES_CONFIG_CRON']+'/crontab'],start_new_session=True);groups.append(cron.pid)
     generation=desired;activations+=1;status('active')
   reap();time.sleep(.1)
 finally:
  if groups:stop(groups)
  generation=None;groups=[];status('inactive')
if __name__=='__main__':main()
