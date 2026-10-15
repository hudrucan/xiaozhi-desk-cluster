"""Bounded read-only scan status: volatile jobs plus native durable import history."""
from contextlib import closing
import datetime,json,math,pathlib,sqlite3,time
NODES=('deskb1x','deskb2x','deskb3x')
REASONS={'dispatcher_busy':'Another scan is running','no_eligible_workers':'No eligible worker: busy, maintenance or unavailable','workers_unavailable':'Workers did not complete the scan','owner_unavailable':'Network owner changed or is recovering','transport_unavailable':'Scan transport unavailable','invalid_request':'Invalid scan request'}

def read_json(path):
 try:
  with pathlib.Path(path).open('rb') as f:raw=f.read(16385)
  if len(raw)>16384:return {}
  v=json.loads(raw)
  return v if isinstance(v,dict) else {}
 except (OSError,ValueError):return {}

def timestamp(value,now):
 return float(value) if type(value) in (int,float) and math.isfinite(value) and 0<value<=now+5 else None

def inventory(database,minutes,now):
 result={'state':'unknown','imported_at':None,'age_seconds':None}
 try:
  with closing(sqlite3.connect(pathlib.Path(database).as_uri()+'?mode=ro',uri=True,timeout=.05)) as db:
   row=db.execute('SELECT scanDate,onlineDevices,allDevices FROM Online_History ORDER BY rowid DESC LIMIT 1').fetchone()
  if not row:return result
  at=datetime.datetime.fromisoformat(row[0].replace('Z','+00:00'))
  if at.tzinfo is None:at=at.replace(tzinfo=datetime.timezone.utc)
  stamp=timestamp(at.timestamp(),now)
  if stamp is None:return result
  age=max(0,int(now-stamp));threshold=max(180,minutes*180) if minutes else 900
  result.update(state='fresh' if age<threshold else 'stale',imported_at=stamp,age_seconds=age)
  for key,value in zip(('online_devices','all_devices'),row[1:]):
   if type(value) is int and 0<=value<=100000:result[key]=value
 except (OSError,ValueError,TypeError,sqlite3.Error):pass
 return result

def observation(value,now):
 result={}
 for key in ('started_at','finished_at','last_success_at','at'):
  at=timestamp(value.get(key),now)
  if at is not None:result[key]=at
 for key in ('worker','owner'):
  if value.get(key) in NODES:result[key]=value[key]
 job=value.get('job_id')
 if isinstance(job,str) and len(job)==32 and all(c in '0123456789abcdef' for c in job):result['job_id']=job
 for key in ('devices','new_devices','ip_changes','resolved','queried'):
  val=value.get(key)
  if type(val) is int and 0<=val<=100000:result[key]=val
 for key in ('wall_seconds','scan_seconds'):
  val=value.get(key)
  if type(val) in (int,float) and math.isfinite(val) and 0<=val<=120:result[key]=val
 reason=value.get('reason')
 if reason in REASONS:result.update(reason=reason,message=REASONS[reason])
 return result

def status(database,native_root,dispatch,owner,term,minutes,queued_at=0,now=None):
 now=time.time() if now is None else now
 data=inventory(database,minutes,now);coordinator=read_json(dispatch)
 valid=coordinator.get('owner')==owner and coordinator.get('term')==term
 scans={}
 for plugin,file in (('arp','desk-arp-status.json'),('mdns','desk-mdns-status.json')):
  job=coordinator.get('scans',{}).get(plugin,{}) if valid and isinstance(coordinator.get('scans'),dict) else {}
  job=job if isinstance(job,dict) else {};native=read_json(pathlib.Path(native_root)/file) if native_root else {}
  result={'phase':'idle'}
  if job.get('phase') in ('selecting','running','retrying','completed','failed'):
   result.update(observation(job,now),phase=job['phase'])
   # An old volatile in-flight observation cannot stay Running indefinitely.
   if result['phase'] in ('selecting','running','retrying') and now-result.get('at',0)>50:result.update(phase='failed',reason='transport_unavailable',message=REASONS['transport_unavailable'])
  finished=timestamp(native.get('finished_at'),now)
  if finished and (not result.get('started_at') or finished>=result['started_at']):
   result.update(observation(native,now))
   if native.get('state')=='ready':
    result['phase']='complete' if plugin=='mdns' or data.get('imported_at',0) is not None and (data.get('imported_at') or 0)>=int(finished) else 'importing'
    if plugin=='mdns' and native.get('queried')==0:result['phase']='cached'
    if result['phase']=='importing' and now-finished>120:result.update(phase='failed',message='Native import is taking longer than expected')
   else:result['phase']='failed'
  if plugin=='arp' and queued_at and result.get('started_at',0)<queued_at-1:
   result={'phase':'queued','started_at':queued_at}
   if now-queued_at>120:result.update(phase='failed',message='Native scan queue did not start in time')
  scans[plugin]=result
 return {'protocol':'desk-network-discovery-v1','owner':owner,'observed_at':now,'inventory':data,'scans':scans}

def changes(database,rows):
 try:
  with closing(sqlite3.connect(pathlib.Path(database).as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
   known=db.execute('SELECT devMac,devLastIP FROM Devices LIMIT 4097').fetchall()
  if len(known)>4096:return {}
  known={mac.lower():ip for mac,ip in known if isinstance(mac,str)}
  return {'new_devices':sum(mac not in known for mac,_ in rows.values()),'ip_changes':sum(mac in known and known[mac]!=ip for ip,(mac,_) in rows.items())}
 except (OSError,ValueError,sqlite3.Error):return {}
