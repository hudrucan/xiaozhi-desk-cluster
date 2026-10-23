"""Append only the native HA root-origin listener, validating all HAProxy fragments."""
import pathlib,re,subprocess,tempfile,os,runpy
BEGIN='# BEGIN DESK HOME ASSISTANT\n';END='# END DESK HOME ASSISTANT\n'
def candidate(old,fragment):
 base=re.sub(re.escape(BEGIN)+'.*?'+re.escape(END),'',old,flags=re.S)
 if '# BEGIN DESK HTTPS\n' in old:
  shared=pathlib.Path('/usr/local/libexec/desk-ingress-proxy.py')
  if not shared.is_file():shared=pathlib.Path(__file__).resolve().parents[2]/'desk_monitoring/files/install_proxy.py'
  composer=runpy.run_path(str(shared))
  base=base.replace(composer['https_fragment'](old),'').rstrip()+'\n'
  return composer['full_candidate'](old,base+BEGIN+fragment.rstrip()+'\n'+END)
 return base+('' if base.endswith('\n') else '\n')+BEGIN+fragment.rstrip()+'\n'+END
if __name__=='__main__':
 path=pathlib.Path('/etc/haproxy/haproxy.cfg');old=path.read_text();new=candidate(old,pathlib.Path('/etc/desk-homeassistant/proxy.cfg').read_text())
 if old==new:print('unchanged');raise SystemExit()
 with tempfile.NamedTemporaryFile('w',prefix='.homeassistant-',dir=path.parent,delete=False) as f:temp=pathlib.Path(f.name);f.write(new)
 try:
  subprocess.run(['/usr/sbin/haproxy','-c','-f',str(temp),'-f','/etc/haproxy/xiaozhi-mqtt.cfg'],check=True,capture_output=True,timeout=10)
  os.chmod(temp,0o644);os.replace(temp,path)
  try:subprocess.run(['systemctl','reload','haproxy'],check=True,capture_output=True,timeout=15)
  except Exception:
   path.write_text(old);subprocess.run(['systemctl','reload','haproxy'],check=True,capture_output=True,timeout=15);raise
  print('changed')
 finally:temp.unlink(missing_ok=True)
