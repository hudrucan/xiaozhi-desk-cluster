#!/usr/bin/env python3
"""Generate scoped scan credentials once; never print or rotate existing keys."""
import json,os,pathlib,secrets
root=pathlib.Path(__file__).resolve().parents[1]/'secret/network';root.mkdir(parents=True,exist_ok=True)
p=root/'scan-nats.json'
if not p.exists():
 value={'key':secrets.token_hex(32),'coordinators':{},'workers':{}}
 for node in ('deskb1x','deskb2x','deskb3x'):
  for kind,prefix in (('coordinators','network-owner-'),('workers','network-worker-')):
   value[kind][node]={'user':prefix+node,'password':secrets.token_urlsafe(32)}
 fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 with os.fdopen(fd,'w') as f:json.dump(value,f)
os.chmod(p,0o600)
print('Network scan credentials retained in ignored secret/network/scan-nats.json (0600)')
