"""Reuse the scoped NATS reload pattern. Preserve every existing account/route."""
import json,pathlib,re,subprocess,os,tempfile
BEGIN='    # BEGIN DESK HOME ASSISTANT USERS\n';END='    # END DESK HOME ASSISTANT USERS\n'
def users(secret):
 return [dict(v,permissions={'publish':{'allow':['desk.homeassistant.control.*','desk.homeassistant.reply.'+n+'.>']},'subscribe':{'allow':['desk.homeassistant.control.'+n,'desk.homeassistant.reply.'+n+'.>']},'allow_responses':{'max':1,'expires':'3s'}}) for n,v in secret.items()]
def candidate(old,secret):
 text=re.sub(re.escape(BEGIN)+'.*?'+re.escape(END),'',old,flags=re.S);marker='  users: [\n'
 if text.count(marker)!=1:raise ValueError('Unexpected NATS layout')
 return text.replace(marker,marker+BEGIN+''.join('    '+json.dumps(v)+'\n' for v in users(secret))+END,1)
def main():
 p=pathlib.Path('/etc/nats-server.conf');old=p.read_text();new=candidate(old,json.loads(pathlib.Path('/etc/desk-homeassistant/nats-users.json').read_bytes()))
 if new==old:print('unchanged');return
 with tempfile.NamedTemporaryFile('w',prefix='nats-ha-',dir='/etc',delete=False) as f:temp=pathlib.Path(f.name);f.write(new)
 try:
  subprocess.run(['/usr/sbin/nats-server','-t','-c',str(temp)],check=True,capture_output=True,timeout=5)
  stat=p.stat();os.chown(temp,stat.st_uid,stat.st_gid);os.chmod(temp,stat.st_mode&0o777);os.replace(temp,p)
  subprocess.run(['systemctl','kill','--kill-who=main','--signal=HUP','nats-server'],check=True,capture_output=True,timeout=5)
  print('changed')
 finally:temp.unlink(missing_ok=True)
if __name__=='__main__':
 try:main()
 except Exception:raise SystemExit('Core NATS update failed; credentials withheld')
