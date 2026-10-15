"""Bounded signed envelopes for ephemeral Core NATS scan jobs."""
import hashlib,hmac,json,math,re,time
NODES=('deskb1x','deskb2x','deskb3x')
MAX_BYTES=32768
class Unavailable(Exception):pass

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def pack(value,key):
 raw=encoded(value)
 if len(raw)>MAX_BYTES:raise ValueError('Payload too large')
 return encoded({'body':value,'signature':hmac.new(key.encode(),raw,hashlib.sha256).hexdigest()})
def unpack(raw,key):
 if len(raw)>MAX_BYTES+256:raise ValueError('Envelope too large')
 v=json.loads(raw)
 if set(v)!= {'body','signature'} or not isinstance(v['signature'],str) or not hmac.compare_digest(v['signature'],hmac.new(key.encode(),encoded(v['body']),hashlib.sha256).hexdigest()):raise ValueError('Invalid signature')
 return v['body']
def valid_term(term,owner):return isinstance(term,list) and len(term)==2 and type(term[0]) is int and 0<term[0]<=1000000000 and term[1]==owner and owner in NODES

def validate_job(v,node,now=None):
 from mdns import lan_ip
 now=time.time() if now is None else now
 if not isinstance(v,dict) or set(v)!={'id','owner','term','worker','plugin','deadline','targets'}:raise ValueError('Invalid job')
 if not re.fullmatch('[a-f0-9]{32}',v['id']) or v['worker']!=node or node not in NODES or not valid_term(v['term'],v['owner']) or v['plugin'] not in ('mdns','arp'):raise ValueError('Invalid job identity')
 deadline=v['deadline']
 if type(deadline) not in (int,float) or not math.isfinite(deadline) or not now<deadline<=now+(19 if v['plugin']=='arp' else 9):raise ValueError('Expired job')
 targets=v['targets']
 if v['plugin']=='arp':
  if targets!={}:raise ValueError('ARP subnet is fixed, targets forbidden')
  return v
 if not isinstance(targets,dict) or not 1<=len(targets)<=32:raise ValueError('Invalid targets')
 for ip,mac in targets.items():
  if lan_ip(ip)!=ip or not isinstance(mac,str) or not re.fullmatch('(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',mac) or int(mac[:2],16)&1 or mac=='00:00:00:00:00:00':raise ValueError('Invalid LAN identity')
 return v

def validate_result(v,job):
 from mdns import hostname
 import dns.name
 if not isinstance(v,dict) or v.get('id')!=job['id'] or v.get('term')!=job['term'] or v.get('worker')!=job['worker'] or time.time()>=job['deadline']:raise Unavailable('Stale result')
 if v.get('state')!='ready':raise Unavailable('Worker unavailable')
 rows=v.get('found');stats=v.get('stats')
 if job['plugin']=='arp':
  from arp import validate_rows
  validate_rows(rows)
  if not isinstance(stats,dict) or type(stats.get('received')) is not int or stats['received']!=len(rows):raise ValueError('Invalid ARP counters')
  return rows,dict(stats,worker=job['worker'],job_id=job['id'],plugin='arp')
 if not isinstance(rows,dict) or len(rows)>len(job['targets']) or not isinstance(stats,dict):raise ValueError('Invalid result')
 for ip,row in rows.items():
  if ip not in job['targets'] or not isinstance(row,list) or len(row)!=2:raise ValueError('Unexpected result target')
  name,ttl=row
  if not isinstance(name,str) or hostname(dns.name.from_text(name))!=name or type(ttl) is not int or not 0<ttl<=600:raise ValueError('Invalid name result')
 for key in ('sent','received'):
  if type(stats.get(key)) is not int or not 0<=stats[key]<=256:raise ValueError('Invalid counters')
 return {ip:tuple(row) for ip,row in rows.items()},dict(stats,worker=job['worker'],job_id=job['id'])
