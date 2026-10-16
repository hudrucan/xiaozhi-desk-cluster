"""Allow the local Core bridge to use the existing DNS VIP; preserve policy."""
import pathlib,re,subprocess,tempfile,os,json,urllib.request
ACL='10.88.0.0/16'
def candidate(old):
 matches=list(re.finditer(r'^setACL\(\{([^\n]*)\}\)$',old,re.M))
 if len(matches)!=1:raise ValueError('Unexpected DNS ACL')
 found=matches[0]
 if '"'+ACL+'"' in found[1]:return old
 return old[:found.start(1)]+found[1]+', "'+ACL+'"'+old[found.end(1):]
def backend_access():
 op=urllib.request.build_opener(urllib.request.ProxyHandler({}));base='http://127.0.0.1:3001/control/access/'
 with op.open(base+'list',timeout=3) as response:old=json.load(response)
 if ACL in old['allowed_clients']:return False
 new={**old,'allowed_clients':old['allowed_clients']+[ACL]}
 def write(value):
  request=urllib.request.Request(base+'set',data=json.dumps(value).encode(),headers={'Content-Type':'application/json'},method='POST')
  with op.open(request,timeout=5) as response:response.read()
 try:
  write(new)
  with op.open(base+'list',timeout=3) as response:actual=json.load(response)
  if actual!=new:raise ValueError('Backend ACL verification failed')
 except Exception:write(old);raise
 return True

def main():
 backend_changed=backend_access()
 p=pathlib.Path('/etc/desk-dns/dnsdist.conf');old=p.read_text();new=candidate(old)
 if new==old:print('changed' if backend_changed else 'unchanged');return
 with tempfile.NamedTemporaryFile('w',dir=p.parent,prefix='.ha-acl-',delete=False) as f:tmp=pathlib.Path(f.name);f.write(new)
 try:
  subprocess.run(['dnsdist','--check-config','-C',str(tmp)],capture_output=True,check=True,timeout=10)
  stat=p.stat();os.chmod(tmp,stat.st_mode&0o777);os.chown(tmp,stat.st_uid,stat.st_gid);os.replace(tmp,p)
  try:subprocess.run(['systemctl','restart','desk-dnsdist'],capture_output=True,check=True,timeout=20)
  except Exception:
   p.write_text(old);subprocess.run(['systemctl','restart','desk-dnsdist'],capture_output=True,check=True,timeout=20);raise
  print('changed')
 finally:tmp.unlink(missing_ok=True)
if __name__=='__main__':main()
