#!/usr/bin/env python3
"""Freeze runtime isolation and AdGuard policy without exposing data or secrets."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request
import yaml

UNITS = ('nats-server', 'haproxy', 'keepalived', 'xiaozhi-control-plane',
         'xiaozhi-worker', 'xiaozhi-core', 'xiaozhi-mqtt-gateway',
         'xiaozhi-runtime-apply', 'deskbox-front-panel', 'desk-dnsdist',
         'desk-dns-ui', 'desk-dns-vip', 'desk-dns-activity')

class DriftError(ValueError):
    pass

def digest(data):
    return hashlib.sha256(data).hexdigest()

def base(text):
    for name in ('MONITORING ROUTE','MONITORING BACKEND','DNS UI ROUTE','DNS UI BACKEND','DASHBOARD BACKEND'):
        text=re.sub(r'^# BEGIN DESK '+name+r'\n.*?^# END DESK '+name+r'\n','',text,flags=re.M|re.S)
    return text

def ui_base(text, peers):
    old='    http-request deny unless { src 192.168.1.0/24 127.0.0.1 }'
    new='    http-request deny unless { src '+' '.join(peers)+' 127.0.0.1 }'
    matches=[line for line in text.splitlines() if line in (old,new)]
    if len(matches)!=1:raise ValueError('Unexpected dedicated UI access rule')
    return text.replace(matches[0], '    # requested internal-only UI access rule')

def state():
    result={'units':{},'digests':{}}
    for unit in UNITS:
        raw=subprocess.check_output(['systemctl','show',unit,'--property=MainPID,ActiveState'],text=True,timeout=5)
        value=dict(line.split('=',1) for line in raw.splitlines())
        if unit != 'desk-dns-vip' and (value['ActiveState']!='active' or value['MainPID']=='0'):
            raise ValueError('Protected unit unavailable')
        if unit == 'desk-dns-ui':
            value.pop('MainPID')  # Its requested access restriction needs a dedicated restart.
        result['units'][unit]=value
    paths={Path('/etc/resolv.conf'),Path('/etc/xiaozhi-ingress.json'),Path('/etc/haproxy/xiaozhi-mqtt.cfg')}
    paths.update(Path('/etc').glob('xiaozhi*.env'))
    paths.update(p for p in Path('/etc/keepalived').rglob('*') if p.is_file())
    paths.update(Path('/etc/desk-dns')/name for name in ('public.json','vip.json','election.json','ui-haproxy.cfg','keepalived.conf'))
    for path in Path('/etc/systemd/system').rglob('*'):
        if path.is_file() and any(unit in str(path) for unit in UNITS):paths.add(path)
    result['digests']={str(p):digest(p.read_bytes()) for p in paths}
    public=json.loads(Path('/etc/desk-dns/public.json').read_bytes())
    peers=[v['management'] for _,v in sorted(public['nodes'].items())]
    ui_path=Path('/etc/desk-dns/ui-haproxy.cfg')
    result['digests'][str(ui_path)]=digest(ui_base(ui_path.read_text(),peers).encode())
    result['haproxy_base']=digest(base(Path('/etc/haproxy/haproxy.cfg').read_text()).encode())
    document=yaml.safe_load(Path('/var/lib/desk-adguard/adguard-home.yaml').read_bytes())
    document.pop('users',None)
    result['dns_policy']=digest(json.dumps(document,sort_keys=True,separators=(',',':')).encode())
    result['dns_policy_fields']={k:digest(json.dumps(v,sort_keys=True,separators=(',',':')).encode()) for k,v in document.items()}
    ingress=json.loads(Path('/etc/xiaozhi-ingress.json').read_bytes())
    dns=json.loads(Path('/etc/desk-dns/public.json').read_bytes())
    result['vip']=ingress['vip']
    result['dns_vip']=dns['vip']
    return result

def guard(previous,current):
    if previous!=current:
        changed=[]
        for key in sorted(set(previous)|set(current)):
            if previous.get(key)==current.get(key):continue
            if key in ('units','digests','dns_policy_fields'):
                a=previous.get(key,{}) or {};b=current.get(key,{}) or {}
                changed.extend(key+':'+k for k in sorted(set(a)|set(b)) if a.get(k)!=b.get(k))
            else:changed.append(key)
        raise DriftError('Unified UI protected state changed: '+', '.join(changed))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('freeze','guard'))
    args=parser.parse_args()
    try:
        current=state()
        if args.mode=='guard':
            guard(json.load(sys.stdin),current)
            print('Runtime, DNS policy and VIP configuration unchanged')
        else:print(json.dumps(current))
    except DriftError as error:
        raise SystemExit(str(error))
    except Exception:
        raise SystemExit('Unified UI isolation check failed; no private data printed')
