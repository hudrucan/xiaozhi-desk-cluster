"""Root-only, local Core bridge. No HTTP listener or bearer credential.

The container sees only this socket directory, read-only. It can inspect status,
reserve one online sync, and publish a ticketed native backup; never stop Core.
"""
import asyncio,json,os,pathlib,re,secrets,time

class OnlineSync:
 def ui_init(self):self.ui_job=None
 def ui_busy(self):
  j=getattr(self,'ui_job',None)
  return bool(j and j['state'] in ('preparing','replicating'))
 def expire_ui_job(self):
  if self.ui_busy() and self.ui_job['state']=='preparing' and time.monotonic()>self.ui_job['deadline']:
   self.ui_job['state']='failed';self.ui_job['error']='Sync preparation expired; retry when ready.'
 def ui_status(self):
  self.expire_ui_job();v=self.public();head=self.journal.status(True)
  replicas=[]
  for n in self.members:
   h=self.status() if n==self.node else self.heads.get(n)
   synced=bool(h and h['journal']['revision']==head['revision'] and h['journal'].get('value')==head['value'] and h.get('blob_ready'))
   replicas.append({'node':n,'synced':synced,'revision':h['journal']['revision'] if h else None})
  j=self.ui_job or {}
  return dict(v,replicas=replicas,operation={k:j[k] for k in ('state','error','revision') if k in j})
 def ui_begin(self):
  self.expire_ui_job()
  if not self.owned() or not self.ready or self.phase!='active':raise ValueError('Core owner is not ready')
  if self.ui_busy() or (self.op and not self.op.done()):raise ValueError('Another Core operation is running')
  self.ui_job={'ticket':secrets.token_hex(16),'generation':self.generation,'term':self.term[:],
   'deadline':time.monotonic()+300,'state':'preparing','error':None}
  return {'ticket':self.ui_job['ticket']}
 def ui_check(self,ticket):
  self.expire_ui_job();j=self.ui_job
  if not self.ui_busy() or not secrets.compare_digest(str(ticket),j['ticket']):raise ValueError('Sync reservation expired')
  if not self.owned() or self.generation!=j['generation'] or self.term!=j['term']:raise ValueError('Core ownership changed; retry on the new owner')
  return j
 async def ui_import(self,ticket,filename,backup_id):
  j=self.ui_check(ticket)
  if j['state']!='preparing' or (self.op and not self.op.done()):raise ValueError('Sync is already running')
  if not isinstance(filename,str) or len(filename)>255 or pathlib.Path(filename).name!=filename or not filename.endswith('.tar') or not re.fullmatch('[a-f0-9]{8}',backup_id):raise ValueError('Invalid native backup name')
  self.op=asyncio.create_task(self.online_sync(ticket,filename,backup_id))
  # Start once. The browser polls status; a closed browser never cancels a sync.
  return {'accepted':True}
 async def online_sync(self,ticket,filename,backup_id):
  from agent import ROOT,worker
  j=self.ui_check(ticket);j['state']='replicating';path=ROOT/'blobs'/('online-'+secrets.token_hex(8))
  try:
   directory=(ROOT/'current').resolve()/'backups'
   if directory.is_symlink() or not directory.is_dir():raise ValueError('Invalid backup directory')
   value=json.loads(await asyncio.to_thread(worker,'import-native',source=directory/filename,destination=path,ticket=ticket,backup_id=backup_id))
   self.ui_check(ticket)
   os.replace(path,ROOT/'blobs'/value['digest'])
   revision=await self.propose(value)
   self.applied=revision
   # Compare live config again rather than assuming no edits happened mid-sync.
   from snapshot import config_digest
   self.disk_digest=await asyncio.to_thread(config_digest,(ROOT/'current').resolve());self.last_digest=0
   j.update(state='complete',revision=revision,error=None)
  except Exception:
   j.update(state='failed',error='Sync failed. Local changes remain; the previous certified revision is retained.')
   raise
  finally:path.unlink(missing_ok=True)
 async def ui_cli(self,reader,writer):
  try:
   line=await asyncio.wait_for(reader.readline(),2)
   if len(line)>2048:raise ValueError('Request too large')
   v=json.loads(line)
   if v=={'action':'status'}:result=self.ui_status()
   elif v=={'action':'cameras'}:
    from camera_api import status
    result=await asyncio.to_thread(status)
   elif v.get('action') in ('camera_prepare','camera_status','camera_inventory','camera_cancel','camera_touch') and set(v)=={'action','value'}:
    from camera_api import playback
    result=await playback({'camera_prepare':'prepare','camera_status':'status','camera_inventory':'inventory','camera_cancel':'cancel','camera_touch':'touch'}[v['action']],v['value'])
   elif v.get('action')=='camera_artifact' and set(v)=={'action','value'}:
    from camera_api import artifact
    result=artifact(v['value'])
   elif v.get('action')=='camera_thumbnail' and set(v)=={'action','value'}:
    from camera_api import thumbnail_ticket
    result=thumbnail_ticket(v['value'])
   elif v=={'action':'begin'}:result=self.ui_begin()
   elif set(v)=={'action','ticket','filename','backup_id'} and v['action']=='import':
    result=await self.ui_import(v['ticket'],v['filename'],v['backup_id'])
   elif set(v)=={'action','ticket'} and v['action']=='abort':
    j=self.ui_check(v['ticket'])
    if j['state']!='preparing':raise ValueError('Replication already started')
    j.update(state='failed',error='Native backup failed. Local changes remain; retry sync.');result={'aborted':True}
   else:raise ValueError('Unsupported operation')
   out={'ok':True,'value':result}
  except Exception:out={'ok':False,'error':'Cluster operation unavailable; inspect status and retry.'}
  try:writer.write(json.dumps(out,separators=(',',':')).encode()+b'\n');await writer.drain()
  finally:writer.close();await writer.wait_closed()
 async def ui_server(self,root):
  directory=root/'ui';directory.mkdir(exist_ok=True,mode=0o700);os.chmod(directory,0o700)
  path=directory/'bridge.sock';path.unlink(missing_ok=True)
  server=await asyncio.start_unix_server(self.ui_cli,path=str(path));os.chmod(path,0o600)
  return server
