#!/usr/bin/env python3
"""Interactively save a dedicated DNS token in ignored secret/, never print it."""
import argparse,base64,getpass,json,os,re,tempfile,urllib.request,urllib.error
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--zone-id');args=parser.parse_args()
zone=args.zone_id
if not zone:
    try:
        cert=(Path.home()/'.cloudflared/cert.pem').read_text()
        match=re.search(r'-----BEGIN ARGO TUNNEL TOKEN-----\s*(.*?)\s*-----END ARGO TUNNEL TOKEN-----',cert,re.S)
        zone=json.loads(base64.b64decode(match[1]))['zoneID']  # Only public zone identity; never copy its token.
    except (OSError,ValueError,KeyError,TypeError):zone=input('Zone ID for huynm.dev: ').strip()
if not re.fullmatch(r'[a-f0-9]{32}',zone or ''):raise SystemExit('Invalid zone ID')
token=getpass.getpass('Dedicated Cloudflare token (hidden): ').strip()
if not re.fullmatch(r'[A-Za-z0-9_-]{20,512}',token):raise SystemExit('Invalid token format')
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):raise ValueError('Redirect refused')
req=urllib.request.Request(f'https://api.cloudflare.com/client/v4/zones/{zone}/dns_records?name=vpn.huynm.dev',headers={'Authorization':'Bearer '+token})
try:
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(req,timeout=10) as r:
        raw=r.read(32769)
    if len(raw)>32768:raise ValueError()
    value=json.loads(raw);rows=value.get('result',[])
    if value.get('success') is not True or len(rows)!=1:raise ValueError()
    row=rows[0]
    if row.get('name')!='vpn.huynm.dev' or row.get('type')!='A' or row.get('proxied') is not False or not re.fullmatch(r'[a-f0-9]{32}',row.get('id','')):raise ValueError()
except (OSError,ValueError,urllib.error.URLError):raise SystemExit('Token could not read the exact DNS-only A record; no secret saved') from None
root=Path(__file__).resolve().parents[1]/'secret'
os.umask(0o077);root.mkdir(mode=0o700,exist_ok=True)
fd,tmp=tempfile.mkstemp(dir=root,prefix='.vpn-ddns-')
try:
    with os.fdopen(fd,'w') as f:
        json.dump({'zone_id':zone,'record_id':row['id'],'api_token':token},f);f.write('\n')
    os.replace(tmp,root/'vpn-ddns.json')
finally:Path(tmp).unlink(missing_ok=True)
print('Saved ignored secret/vpn-ddns.json (0600); token not printed')
