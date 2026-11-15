#!/usr/bin/env python3
"""Fixed LAN ARP worker and native ARPSCAN result adapter; no database writes."""
import fcntl,json,math,os,pathlib,re,resource,socket,subprocess,sys,tempfile,time
LAN='192.168.1.0/24'
INTERFACE='wlan0'
GATEWAY='192.168.1.1'
MAX_OUTPUT=65536
GRACE_SECONDS=600
PRESENCE=pathlib.Path('/tmp/desk-arp-presence.json')
MAX_MACS=2048
MAX_STATE_BYTES=262144

def presence(rows,path=None,now=None):
 # Called only for validated successful scans, under the adapter's process lock.
 # /tmp is a private container tmpfs, never the durable checkpoint data tree.
 from mdns import lan_ip
 path=PRESENCE if path is None else path
 now=time.time() if now is None else now
 try:
  grace=float(os.getenv('DESK_NETWORK_ARP_GRACE_SECONDS',str(GRACE_SECONDS)))
  if not math.isfinite(grace) or grace<0:raise ValueError('Invalid grace')
 except ValueError:grace=GRACE_SECONDS
 prior={}
 try:
  with path.open('rb') as source:raw=source.read(MAX_STATE_BYTES+1)
  if len(raw)>MAX_STATE_BYTES:raise ValueError('Presence state too large')
  value=json.loads(raw)
  if value['version']!=1 or not isinstance(value['entries'],dict) or len(value['entries'])>MAX_MACS:raise ValueError('Invalid presence state')
  for mac,entry in value['entries'].items():
   if not re.fullmatch('(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',mac) or int(mac[:2],16)&1 or mac=='00:00:00:00:00:00':continue
   if not isinstance(entry,list) or len(entry)!=2:continue
   ip,seen=entry
   if not isinstance(ip,str) or lan_ip(ip)!=ip or type(seen) not in (float,int):continue
   if not math.isfinite(seen) or not 0<=now-seen<=grace:continue
   prior[mac]=[ip,seen]
 except (OSError,ValueError,KeyError,TypeError,OverflowError,RecursionError):prior={}
 # Current replies always win, including a new MAC taking an old MAC's IP.
 found=dict(rows);entries={mac:[ip,now] for ip,(mac,vendor) in rows.items()}
 held=0
 for mac,(ip,seen) in sorted(prior.items(),key=lambda item:(-item[1][1],item[0])):
  if len(entries)>=MAX_MACS:break
  if mac in entries or ip in found:continue
  found[ip]=[mac,''];entries[mac]=[ip,seen];held+=1
 saved=True;pending=None
 try:
  with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix='.desk-arp-presence-',delete=False) as target:
   pending=pathlib.Path(target.name)
   json.dump({'version':1,'entries':entries},target,separators=(',',':'))
  os.replace(pending,path)
 except OSError:saved=False
 finally:
  if pending is not None:
   try:pending.unlink(missing_ok=True)
   except OSError:saved=False
 return found,{'held':held,'grace_state':'ready' if saved else 'unavailable'}

def validate_rows(rows):
 from mdns import lan_ip
 if not isinstance(rows,dict) or not 1<=len(rows)<=254:raise ValueError('Invalid ARP rows')
 seen=set()
 for ip,row in rows.items():
  if lan_ip(ip)!=ip or not isinstance(row,(list,tuple)) or len(row)!=2:raise ValueError('Invalid ARP identity')
  mac,vendor=row
  if not isinstance(mac,str) or not re.fullmatch('(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',mac) or int(mac[:2],16)&1 or mac=='00:00:00:00:00:00' or mac in seen:raise ValueError('Invalid or conflicting MAC')
  if not isinstance(vendor,str) or len(vendor)>96 or any(ord(c)<32 or ord(c)>126 or c=='|' for c in vendor):raise ValueError('Invalid vendor')
  seen.add(mac)
 if GATEWAY not in rows:raise ValueError('Gateway absent; scan inconclusive')
 return rows

def scan():
 from mdns import interface_address
 address=interface_address()
 if address not in ('192.168.1.101','192.168.1.102','192.168.1.103'):raise ValueError('Unexpected LAN interface')
 # No shell, user-supplied interface, flags or subnet. Bound bandwidth/time/output.
 argv=['/usr/sbin/arp-scan','--interface='+INTERFACE,'--ignoredups','--retry=3','--bandwidth=128000','--timeout=500','--plain','--quiet','--ouifile=/dev/null','--macfile=/dev/null',LAN]
 with tempfile.TemporaryFile() as output:
  started=time.monotonic();cpu=resource.getrusage(resource.RUSAGE_CHILDREN)
  subprocess.run(argv,stdout=output,stderr=subprocess.DEVNULL,timeout=12,check=True,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C'})
  if output.tell()>MAX_OUTPUT:raise ValueError('ARP output too large')
  output.seek(0);raw=output.read(MAX_OUTPUT+1).decode('ascii',errors='replace')
 rows={}
 for line in raw.splitlines():
  fields=line.split(None,2)
  if len(fields)<2:continue
  ip,mac=fields[:2];mac=mac.lower()
  if not re.fullmatch('(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',mac):continue
  # NetAlertX enriches vendors centrally using its cached VNDRPDT database.
  vendor=''
  if ip in rows and rows[ip][0]!=mac:raise ValueError('Conflicting ARP replies')
  rows[ip]=[mac,vendor]
 # A scanner cannot ARP itself; retain its directly verified interface identity.
 local_mac=pathlib.Path('/sys/class/net/wlan0/address').read_text().strip().lower()
 rows[address]=[local_mac,'']
 # VIP aliases share a physical MAC. Native ARPSCAN has one object per MAC;
 # prefer physical node addresses rather than whichever VIP replied first.
 by_mac={}
 priority=lambda ip:(ip!=GATEWAY,ip not in ('192.168.1.101','192.168.1.102','192.168.1.103'),int(ip.rsplit('.',1)[1]))
 for ip,row in sorted(rows.items(),key=lambda item:priority(item[0])):
  by_mac.setdefault(row[0],(ip,row))
 rows=dict(by_mac.values())
 validate_rows(rows)
 return rows,{'received':len(rows),'scan_seconds':round(time.monotonic()-started,3),'scan_cpu_seconds':round(sum(resource.getrusage(resource.RUSAGE_CHILDREN)[:2])-cpu.ru_utime-cpu.ru_stime,4)}

def lookup():
 with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
  client.settimeout(45);client.connect('/run/desk-network-scan/dispatch.sock')
  client.sendall(b'{"plugin":"arp","targets":{}}\n');raw=bytearray()
  while not raw.endswith(b'\n'):
   part=client.recv(4096)
   if not part or len(raw)+len(part)>32768:raise OSError('Invalid ARP dispatch response')
   raw.extend(part)
 value=json.loads(raw)
 if value.get('state')!='ready':
  error=OSError('Distributed ARP unavailable');error.reason=value.get('reason','transport_unavailable');raise error
 return validate_rows(value['found']),value['stats']

def main():
 if os.getenv('DESK_NETWORK_DISTRIBUTED')!='1':
  os.execv(sys.executable,[sys.executable,'/app/server/plugins/arp_scan/script.py',*sys.argv[1:]])
 sys.path[:0]=['/app/server','/app/server/plugins']
 from const import logPath
 from mdns import save_json,DATABASE,write_plugin_results
 from discovery import changes
 path=pathlib.Path(logPath)/'plugins/last_result.ARPSCAN.log'
 with open('/tmp/desk-arp.lock','w') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return 1
  write_plugin_results(path,[]) # Never re-import a stale successful scan.
  pending=path.with_name('.desk-arp-result');pending.unlink(missing_ok=True)
  started=time.time()
  try:
   rows,stats=lookup();rows,grace_stats=presence(rows);stats.update(grace_stats);stats.update(changes(DATABASE,rows))
   write_plugin_results(pending,[(mac,ip,ip,vendor,LAN+' --interface='+INTERFACE,'','ARPSCAN','') for ip,(mac,vendor) in rows.items()]);os.replace(pending,path)
   stats=dict(stats,state='ready',devices=len(rows))
  except (OSError,ValueError,KeyError,TypeError) as error:
   # Native execute_plugin skips process_plugin_events when the file is empty.
   # No CurrentScan promotion means no synthetic all-offline scan on failure.
   pending.unlink(missing_ok=True)
   write_plugin_results(path,[]);stats={'state':'unavailable','held':0,'reason':getattr(error,'reason','transport_unavailable')}
  stats.update(started_at=started,finished_at=time.time())
  save_json(pathlib.Path('/tmp/desk-arp-status.json'),stats)
  print('Desk ARP '+json.dumps(stats,sort_keys=True))
  return 0 if stats['state']=='ready' else 1
if __name__=='__main__':raise SystemExit(main())
