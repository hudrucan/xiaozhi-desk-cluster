"""Replace only the retired remote-pilot ACL block with scoped media identities."""
import json,os,pathlib,re,subprocess,tempfile
BEGIN='    # BEGIN DESK CAMERA MEDIA USERS\n';END='    # END DESK CAMERA MEDIA USERS\n'
def candidate(old,users):
 if set(users)!={'deskb1x','deskb2x','deskb3x'}:raise ValueError('Full cluster required')
 text=old
 for start,end in [(BEGIN,END),('    # BEGIN DESK REMOTE INTEGRATION USERS\n','    # END DESK REMOTE INTEGRATION USERS\n')]:
  if text.count(start)!=text.count(end) or text.count(start)>1:raise ValueError('Invalid owned block')
  text=re.sub(re.escape(start)+'.*?'+re.escape(end),'',text,flags=re.S)
 marker='  users: [\n'
 if text.count(marker)!=1:raise ValueError('Unexpected NATS layout')
 records=[]
 for node,user in sorted(users.items()):
  records.append(dict(user,permissions={'publish':{'allow':['desk.ha.media.control.*','desk.ha.media.*.control.*','desk.ha.media.reply.'+node+'.>','desk.ha.media.*.reply.'+node+'.>']},'subscribe':{'allow':['desk.ha.media.control.'+node,'desk.ha.media.*.control.'+node,'desk.ha.media.reply.'+node+'.>','desk.ha.media.*.reply.'+node+'.>']},'allow_responses':{'max':1,'expires':'3s'}}))
 return text.replace(marker,marker+BEGIN+''.join('    '+json.dumps(v)+'\n' for v in records)+END,1)
if __name__=='__main__':
 try:
  path=pathlib.Path('/etc/nats-server.conf');old=path.read_text();new=candidate(old,json.loads(pathlib.Path('/etc/desk-camera/nats-users.json').read_bytes()))
  if old==new:print('unchanged');raise SystemExit(0)
  with tempfile.NamedTemporaryFile('w',dir='/etc',prefix='nats-camera-',delete=False) as f:temp=pathlib.Path(f.name);f.write(new)
  try:
   subprocess.run(['/usr/sbin/nats-server','-t','-c',str(temp)],check=True,capture_output=True,timeout=5)
   stat=path.stat();os.chown(temp,stat.st_uid,stat.st_gid);os.chmod(temp,stat.st_mode&0o777);os.replace(temp,path)
   subprocess.run(['systemctl','kill','--kill-who=main','--signal=HUP','nats-server'],check=True,capture_output=True,timeout=5);print('changed')
  finally:temp.unlink(missing_ok=True)
 except Exception:raise SystemExit('Camera ACL update failed; credentials withheld')
