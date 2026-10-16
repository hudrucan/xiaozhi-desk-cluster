"""Reuse the scoped signed NATS envelope pattern; metadata only, no archives."""
import hashlib,hmac,json,math,re,time
MAX=65536
NODES=('deskb1x','deskb2x','deskb3x')
def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def pack(v,key):
 b=encoded(v)
 if len(b)>MAX:raise ValueError('Oversized metadata')
 return encoded({'body':v,'signature':hmac.new(key.encode(),b,hashlib.sha256).hexdigest()})
def unpack(raw,key):
 if len(raw)>MAX+256:raise ValueError('Oversized envelope')
 v=json.loads(raw)
 if set(v)!={'body','signature'} or not isinstance(v['signature'],str) or not hmac.compare_digest(v['signature'],hmac.new(key.encode(),encoded(v['body']),hashlib.sha256).hexdigest()):raise ValueError('Invalid signature')
 b=v['body']
 if b.get('sender') not in NODES or b.get('recipient') not in NODES or not re.fullmatch('[a-f0-9]{32}',b.get('id','')):raise ValueError('Invalid RPC identity')
 at=b.get('at')
 if type(at) not in (int,float) or not math.isfinite(at) or abs(time.time()-at)>10:raise ValueError('Expired RPC')
 return b
