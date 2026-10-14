#!/usr/bin/python3
"""Owner-only bounded Cloudflare A update. Independent of VPN election health."""
import ipaddress,json,os,re,subprocess,time,urllib.error,urllib.request
from pathlib import Path
HOST='vpn.huynm.dev';VIP='192.168.1.254';BASE='https://api.cloudflare.com/client/v4'
MAX_BYTES=32768
class Failure(Exception):pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise Failure('redirect_rejected')
def load(path,limit=MAX_BYTES):
    with open(path,'rb') as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise Failure('metadata_oversize')
    value=json.loads(raw)
    if not isinstance(value,dict):raise Failure('invalid_metadata')
    return value

def eligible(config,addresses,status,boot,now):
    return (config.get('protocol')=='desk-vpn-ha-v1' and config.get('vip')==VIP
            and config.get('node') in ('deskb1x','deskb2x','deskb3x')
            and any(d.get('ifname')=='wlan0' and any(a.get('family')=='inet' and a.get('local')==VIP for a in d.get('addr_info',[])) for d in addresses)
            and status.get('protocol')=='desk-vpn-status-v1' and status.get('node')==config['node']
            and status.get('boot_id')==boot and status.get('ready') is True
            and 0<=now-status.get('_timestamp',0)<=20)

def is_owner():
    try:
        directory=Path(os.environ['CREDENTIALS_DIRECTORY'])
        config=load(directory/'vpn-public.json')
        status=load(directory/'vpn-status.json',4096)
        from datetime import datetime
        status['_timestamp']=datetime.fromisoformat(status['observed_at'].replace('Z','+00:00')).timestamp()
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        p=subprocess.run(['/usr/sbin/ip','-j','-4','addr','show','dev','wlan0'],capture_output=True,text=True,timeout=2,check=True)
        if len(p.stdout)>MAX_BYTES:return False
        return eligible(config,json.loads(p.stdout),status,boot,time.time())
    except (OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError,Failure):return False

def credentials(value):
    if (not all(isinstance(value.get(k),str) and re.fullmatch(r'[a-f0-9]{32}',value[k]) for k in ('zone_id','record_id'))
            or not isinstance(value.get('api_token'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,512}',value['api_token'])):
        raise Failure('invalid_credentials')
    return value

def public_ipv4(text):
    try:ip=ipaddress.IPv4Address(text.strip())
    except (ValueError,AttributeError):raise Failure('invalid_public_ipv4') from None
    if not ip.is_global:raise Failure('non_public_ipv4')
    return str(ip)

def record(value,record_id):
    if (not isinstance(value,dict) or value.get('id')!=record_id or value.get('name')!=HOST
            or value.get('type')!='A' or value.get('proxied') is not False):
        raise Failure('record_mismatch')
    public_ipv4(value.get('content'))
    return value

class Client:
    def __init__(self,token):
        self.token=token
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def request(self,url,body=None,cloud=False):
        headers={'User-Agent':'Desk-Cluster-DDNS/1'}
        if cloud:headers.update({'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        req=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,headers=headers,method='PATCH' if body is not None else 'GET')
        try:
            with self.opener.open(req,timeout=5) as response:raw=response.read(MAX_BYTES+1)
        except urllib.error.HTTPError as e:raise Failure('cloud_http_'+str(e.code) if cloud else 'public_ip_http') from None
        except (OSError,urllib.error.URLError):raise Failure('network_unavailable') from None
        if len(raw)>MAX_BYTES:raise Failure('response_oversize')
        if not cloud:return raw.decode('ascii').strip()
        value=json.loads(raw)
        if not isinstance(value,dict) or value.get('success') is not True:raise Failure('cloud_rejected')
        return value.get('result')
    def public_ip(self):
        a=public_ipv4(self.request('https://api.ipify.org/'))
        b=public_ipv4(self.request('https://checkip.amazonaws.com/'))
        if a!=b:raise Failure('public_ip_disagreement')
        return a

def reconcile(client,secret,owner=is_owner):
    if not owner():return 'standby'
    ip=client.public_ip()
    url=BASE+'/zones/'+secret['zone_id']+'/dns_records/'+secret['record_id']
    current=record(client.request(url,cloud=True),secret['record_id'])
    if not owner():return 'ownership_changed'
    if current['content']==ip and current.get('ttl')==120:return 'unchanged'
    # Never create/delete a record or modify any hostname besides the pinned A.
    changed=record(client.request(url,body={'content':ip,'ttl':120,'proxied':False},cloud=True),secret['record_id'])
    if changed['content']!=ip or changed.get('ttl')!=120:raise Failure('update_not_confirmed')
    verified=record(client.request(url,cloud=True),secret['record_id'])
    if verified['content']!=ip or verified.get('ttl')!=120:raise Failure('readback_mismatch')
    return 'updated'

def main():
    try:
        if not is_owner():print('DDNS standby');return 0
        secret=credentials(load(Path(os.environ['CREDENTIALS_DIRECTORY'])/'cloudflare.json',4096))
        print('DDNS '+reconcile(Client(secret['api_token']),secret))
        return 0
    except (Failure,OSError,ValueError,TypeError,KeyError) as e:
        print('DDNS failed: '+(str(e) if isinstance(e,Failure) else 'local_or_response_invalid'))
        return 1
if __name__=='__main__':raise SystemExit(main())
