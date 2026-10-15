#!/usr/bin/env python3
"""Add scoped Network users, validate then SIGHUP; preserve existing auth/routes."""
import argparse,json,pathlib,re,subprocess,os,tempfile
BEGIN='    # BEGIN DESK NETWORK SCAN USERS\n';END='    # END DESK NETWORK SCAN USERS\n'
def users(secret):
 result=[]
 for node,credential in secret['coordinators'].items():
  result.append(dict(credential,permissions={'publish':{'allow':['desk.network.scan.*','desk.network.status.*']},'subscribe':{'allow':['desk.network.reply.'+node+'.>']}}))
 for node,credential in secret['workers'].items():
  result.append(dict(credential,permissions={'subscribe':{'allow':['desk.network.scan.'+node,'desk.network.status.'+node]},'allow_responses':{'max':1,'expires':'20s'}}))
 return result
def candidate(text,secret):
 text=re.sub(re.escape(BEGIN)+'.*?'+re.escape(END),'',text,flags=re.S)
 marker='  users: [\n'
 if text.count(marker)!=1:raise ValueError('Unexpected NATS auth structure')
 block=BEGIN+''.join('    '+json.dumps(v,separators=(',',':'))+'\n' for v in users(secret))+END
 return text.replace(marker,marker+block,1)
def main():
 p=argparse.ArgumentParser();p.add_argument('--secret',required=True);args=p.parse_args()
 path=pathlib.Path('/etc/nats-server.conf');old=path.read_text();new=candidate(old,json.loads(pathlib.Path(args.secret).read_text()))
 if old==new:print('unchanged');return
 with tempfile.NamedTemporaryFile(mode='w',prefix='nats-network-',dir='/etc',delete=False) as f:
  temp=pathlib.Path(f.name);f.write(new)
 try:
  os.chmod(temp,0o600)
  r=subprocess.run(['/usr/sbin/nats-server','-t','-c',str(temp)],capture_output=True,timeout=5)
  if r.returncode:raise ValueError('NATS validation failed')
  # Preserve existing file owner/mode, no service restart or credential change.
  stat=path.stat();os.chown(temp,stat.st_uid,stat.st_gid);os.chmod(temp,stat.st_mode&0o777)
  os.replace(temp,path)
  subprocess.run(['systemctl','kill','--kill-who=main','--signal=HUP','nats-server'],check=True,capture_output=True,timeout=5)
  print('changed')
 finally:temp.unlink(missing_ok=True)
if __name__=='__main__':
 try:main()
 except Exception:raise SystemExit('Network NATS credential update failed; no private data printed')
