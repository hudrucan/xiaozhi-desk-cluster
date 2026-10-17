"""Bounded recording parts and the shared worker cache identity contract."""
import hashlib,json
PART_SECONDS=600
MESSAGES={
 'camera_busy':'This camera is preparing another clip. Wait for it to finish.',
 'node_busy':'This worker is preparing a clip for another camera. Wait for it to finish.',
 'worker_not_ready':'The camera worker is changing or not ready. Retry when Cluster shows Ready.',
 'memory_low':'The worker does not have enough free memory to prepare this clip. Retry later.',
 'disk_low':'The worker does not have enough free cache space to prepare this clip.',
 'session_missing':'The cached clip expired or its recording session is unavailable. Reopen the date folder and retry.'}
def clip_id(workload,date,start,end):
 value={'workload':workload,'date':date.replace('-',''),'start':int(start),'end':int(end)}
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:32]
def parts(start,end):
 start,end=int(start),int(end)
 if start>=end or end-start>86400:raise ValueError('Invalid recording range')
 return [(at,min(at+PART_SECONDS,end)) for at in range(start,end,PART_SECONDS)]
def cached_state(items,workload,date,start,end):
 ident=clip_id(workload,date,start,end)
 states={v.get('state') for v in items if v.get('workload')==workload and (v.get('id')==ident or (v.get('legacy') is True and v.get('start')==int(start) and v.get('end')==int(end)))}
 return 'ready' if 'ready' in states else 'preparing' if 'preparing' in states else 'missing'
def label(title,state,complete=True):
 if state=='ready':return '✓ '+title.lstrip('✓').strip()+' · Downloaded'
 if state=='preparing':return '⏳ '+title+' · Preparing'
 return title.lstrip('✓').strip()+(' · Not downloaded' if complete else ' · Cache status unavailable')
