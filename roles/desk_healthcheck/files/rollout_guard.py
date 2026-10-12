#!/usr/bin/env python3
"""One-shot metadata-only validation for native readiness probe rollout."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

UNITS=('keepalived','desk-dns-vip','haproxy','nats-server','xiaozhi-control-plane',
       'xiaozhi-core','xiaozhi-worker','xiaozhi-mqtt-gateway','xiaozhi-runtime-apply',
       'deskbox-front-panel','desk-dnsdist','desk-adguard','desk-dns-backend',
       'desk-dns-sync-agent','beszel-hub','beszel-agent','beszel-config-sync')
PATHS=('/etc/keepalived/keepalived.conf','/etc/desk-dns/keepalived.conf')

def normalize(text):
    # Only these three recurring commands may differ from the deployment snapshot.
    text=re.sub(r'^    script "/usr/local/libexec/(?:xiaozhi-ingress-probe stats|desk-healthcheck ingress) --socket /run/xiaozhi-haproxy.sock --nodes (?:deskb1x deskb2x deskb3x|deskb1x,deskb2x,deskb3x)"$', '    script INGRESS_READINESS',text,flags=re.M)
    text=re.sub(r'^    script "(?:/usr/bin/python3 /usr/local/libexec/desk-dns/dns-probe.py ready --config /etc/desk-dns/public.json|/usr/local/libexec/desk-healthcheck dns) --host ([0-9.]+) --port 5353"$',r'    script DNS_READINESS \1',text,flags=re.M)
    text=re.sub(r'^    script "(?:/usr/bin/python3 /usr/local/libexec/desk-dns/dns-probe.py prefer --config /etc/desk-dns/public.json|/usr/local/libexec/desk-healthcheck prefer --state /etc/xiaozhi-ingress.json)"$', '    script DNS_PREFERENCE',text,flags=re.M)
    return text

def snapshot():
    units={}
    for unit in UNITS:
        raw=subprocess.check_output(['systemctl','show',unit,'--property=MainPID,ActiveState'],text=True,timeout=4)
        units[unit]=dict(line.split('=',1) for line in raw.splitlines())
        if unit not in ('keepalived','desk-dns-vip') and units[unit]['ActiveState']!='active':
            raise ValueError('Protected service unavailable')
    addresses=json.loads(subprocess.check_output(['ip','-j','addr','show'],text=True,timeout=3))
    # Read deployed IPv4 state and IPv6 addresses from the installed DNS VRRP config.
    wanted={json.loads(Path(p).read_text())['vip'] for p in ('/etc/xiaozhi-ingress.json','/etc/desk-dns/vip.json')}
    dns=Path(PATHS[1]).read_text()
    wanted.update(re.findall(r'^\s+([0-9a-f:]+)/128 dev ',dns,re.M))
    owned=sorted([link['ifname'],item['local']] for link in addresses for item in link.get('addr_info',[]) if item.get('local') in wanted)
    protected={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path('/etc/haproxy/haproxy.cfg'),Path('/etc/desk-dns/dnsdist.conf'),*Path('/etc').glob('xiaozhi*.env')]}
    return {'units':units,'owned':owned,'protected':protected,'configs':{p:Path(p).read_text() for p in PATHS}}

def candidate(before,after):
    for path in PATHS:
        if normalize(before['configs'][path])!=normalize(after['configs'][path]):raise ValueError('Unexpected VRRP config change')
    if '/usr/local/libexec/desk-healthcheck ingress ' not in after['configs'][PATHS[0]]:raise ValueError('Missing ingress replacement')
    for action in ('dns','prefer'):
        if '/usr/local/libexec/desk-healthcheck '+action+' ' not in after['configs'][PATHS[1]]:raise ValueError('Missing DNS replacement')

def main():
    mode=sys.argv[1]
    if mode=='compare':
        native=subprocess.run(['/usr/local/libexec/desk-healthcheck','prefer','--state','/etc/xiaozhi-ingress.json'],capture_output=True,timeout=3)
        previous=subprocess.run(['/usr/bin/python3','/usr/local/libexec/desk-dns/dns-probe.py','prefer','--config','/etc/desk-dns/public.json'],capture_output=True,timeout=4)
        if native.returncode!=previous.returncode:raise ValueError('DNS VIP preference differs')
        print('Native preference matches existing deployed policy')
        return
    after=snapshot()
    if mode=='freeze':print(json.dumps(after));return
    before=json.load(sys.stdin);candidate(before,after)
    if mode=='guard':
        if before['units']!=after['units'] or before['owned']!=after['owned'] or before['protected']!=after['protected']:
            raise ValueError('Protected processes, VIP ownership or config changed')
    print('Native readiness commands valid; existing cluster metadata preserved')

if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Native healthcheck guard failed; no secrets printed')
