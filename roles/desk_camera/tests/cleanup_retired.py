"""Remove only the superseded remote-integration pilot after certified migration."""
import json,pathlib,shutil,subprocess
root=pathlib.Path('/var/lib/desk-homeassistant');status=json.loads(pathlib.Path('/run/desk-homeassistant/public.json').read_bytes())
if status['checkpoint_revision']<16 or status['sync']!='synced':raise SystemExit('New certified recovery copy required')
if status.get('state')=='active':
 entries=json.loads((root/'current/.storage/core.config_entries').read_bytes())['data']['entries']
 if any(e['domain']=='desk_remote' for e in entries) or not {'tapo_control','webrtc'}<={e['domain'] for e in entries}:raise SystemExit('Active Core migration not complete')
if 'DESK REMOTE INTEGRATION USERS' in pathlib.Path('/etc/nats-server.conf').read_text():raise SystemExit('Retired NATS block still present')
for unit in ('desk-ha-remote','desk-ha-remote-runtime'):
 path=pathlib.Path('/etc/systemd/system',unit+'.service')
 if path.exists():subprocess.run(['systemctl','disable','--now',unit],capture_output=True,check=True)
 path.unlink(missing_ok=True)
subprocess.run(['podman','rm','-f','desk-ha-remote-tplink'],capture_output=True)
for path in ('/etc/desk-ha-remote','/usr/local/libexec/desk-ha-remote','/var/lib/desk-ha-remote','/run/desk-ha-remote'):
 if pathlib.Path(path).exists():shutil.rmtree(path,ignore_errors=False)
(root/'ui/remote.sock').unlink(missing_ok=True)
subprocess.run(['systemctl','daemon-reload'],check=True,capture_output=True)
print('Retired remote pilot units, scoped secrets, runtime state and Core socket removed')
