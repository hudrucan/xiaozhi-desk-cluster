"""Bounded logical snapshots of a pinned wg-easy schema; never copy live SQLite."""
from contextlib import closing
import base64,copy,hashlib,ipaddress,json,sqlite3
from pathlib import Path
TABLES=('interfaces_table','general_table','users_table','hooks_table','user_configs_table','clients_table','one_time_links_table')
LIMIT=2*1024*1024
class Invalid(ValueError):pass

def key(v,empty=False):
 if empty and v=='':return
 try:
  if not isinstance(v,str) or len(base64.b64decode(v,validate=True))!=32:raise Invalid('Invalid key')
 except Exception:raise Invalid('Invalid key') from None

def public(private):
 from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
 from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
 return base64.b64encode(X25519PrivateKey.from_private_bytes(base64.b64decode(private)).public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()

def read(path):
 with closing(sqlite3.connect(path,timeout=5)) as db, db:
  db.row_factory=sqlite3.Row
  db.execute('BEGIN')
  return {'tables':{t:[dict(r) for r in db.execute('SELECT * FROM '+t+' ORDER BY '+('name' if t=='interfaces_table' else 'id'))] for t in TABLES},'sequence':dict(db.execute('SELECT name,seq FROM sqlite_sequence'))}

def validate(v,seed):
 if not isinstance(v,dict) or set(v)!= {'tables','sequence'} or set(v['tables'])!=set(TABLES) or len(json.dumps(v).encode())>LIMIT:raise Invalid('Invalid snapshot')
 tables=v['tables'];original=seed['tables']
 for t in TABLES:
  if not isinstance(tables[t],list) or len(tables[t])>32:raise Invalid('Too many records')
  columns=set(original[t][0]) if original[t] else None
  for r in tables[t]:
   if not isinstance(r,dict) or (columns and set(r)!=columns):raise Invalid('Unexpected columns')
 if len(tables['interfaces_table'])!=1 or len(tables['general_table'])!=1 or len(tables['user_configs_table'])!=1:raise Invalid('Invalid singleton config')
 a,b=tables['interfaces_table'][0],original['interfaces_table'][0]
 # Infrastructure, static identity, firewall, transport and hooks remain cluster-owned.
 for k in a:
  if k not in ('updated_at',) and a[k]!=b[k]:raise Invalid('VPN infrastructure is managed by cluster')
 if tables['hooks_table']!=original['hooks_table']:raise Invalid('VPN hooks are managed by cluster')
 for row in tables['user_configs_table']:
  if row['host']!='vpn.huynm.dev' or row['port']!=51820:raise Invalid('Endpoint is managed by cluster')
  if json.loads(row['default_allowed_ips'])!=['10.77.0.1/32','192.168.1.0/24']:raise Invalid('Keep the LAN split tunnel')
  if row['default_mtu']!=1380 or not 0<=row['default_persistent_keepalive']<=65535:raise Invalid('Invalid client default')
  dns=json.loads(row['default_dns'])
  if not isinstance(dns,list) or len(dns)>4:raise Invalid('Invalid default DNS')
  for addr in dns:ipaddress.ip_address(addr)
  for field in ('default_pre_up','default_post_up'): # reserved future native schema fields
   if row.get(field):raise Invalid('Client hooks disabled')
 # Initial LAN administrator and session identity are pinned; no user/password routes exposed.
 if tables['users_table']!=original['users_table'] or tables['general_table']!=original['general_table']:raise Invalid('Retain cluster access configuration')
 users={r['id'] for r in tables['users_table']};ips=set();ips6=set();keys=set();ids=set()
 for r in tables['clients_table']:
  if r['user_id'] not in users or r['interface_id']!='wg0' or not isinstance(r['id'],int) or r['id']<=0 or r['id'] in ids:raise Invalid('Invalid client identity')
  ids.add(r['id'])
  if not isinstance(r['name'],str) or not 1<=len(r['name'])<=128 or any(ord(c)<32 for c in r['name']):raise Invalid('Invalid client name')
  ip=ipaddress.ip_address(r['ipv4_address'])
  if ip.version!=4 or ip not in ipaddress.ip_network('10.77.0.0/24') or str(ip) in ('10.77.0.0','10.77.0.1','10.77.0.255') or str(ip) in ips:raise Invalid('Invalid tunnel address')
  ip6=ipaddress.ip_address(r['ipv6_address'])
  if ip6.version!=6 or ip6 not in ipaddress.ip_network(original['interfaces_table'][0]['ipv6_cidr']) or str(ip6) in ips6:raise Invalid('Invalid IPv6 metadata')
  ips6.add(str(ip6));ips.add(str(ip));key(r['private_key']);key(r['public_key']);key(r['pre_shared_key'],empty=True)
  if public(r['private_key'])!=r['public_key'] or r['public_key'] in keys:raise Invalid('Invalid peer keys')
  keys.add(r['public_key'])
  if r['server_allowed_ips']!='[]' or r['server_endpoint'] is not None or r['firewall_ips'] is not None:raise Invalid('Server peer routing is managed by cluster')
  if any(r.get(k) for k in ('pre_up','post_up','pre_down','post_down')):raise Invalid('Client hooks disabled')
  if r['mtu']!=1380 or r['enabled'] not in (0,1) or not 0<=r['persistent_keepalive']<=65535:raise Invalid('Invalid peer settings')
  if r['allowed_ips'] and json.loads(r['allowed_ips'])!=['10.77.0.1/32','192.168.1.0/24']:raise Invalid('Keep the LAN split tunnel')
  # Expiration is enforced by the cluster journal, not autonomous per-node native cron.
  if r['dns'] is not None:
   dns=json.loads(r['dns'])
   if not isinstance(dns,list) or len(dns)>4:raise Invalid('Invalid client DNS')
   for addr in dns:ipaddress.ip_address(addr)
  if r['expires_at'] is not None:raise Invalid('Peer expiration not supported by cluster yet')
 if tables['one_time_links_table']:raise Invalid('One-time sharing is disabled for LAN cluster access')
 for table,n in v['sequence'].items():
  if table not in ('users_table','clients_table') or not isinstance(n,int) or n<0:raise Invalid('Invalid identity sequence')
 return v

def apply(path,v):
 # A transaction updates rows in-place so existing native SQLite handles remain valid.
 with closing(sqlite3.connect(path,timeout=5)) as db, db:
  db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
  for t in reversed(TABLES):db.execute('DELETE FROM '+t)
  for t in TABLES:
   for row in v['tables'][t]:
    cols=list(row);db.execute('INSERT INTO '+t+' ('+','.join('"'+c+'"' for c in cols)+') VALUES ('+','.join('?' for _ in cols)+')',[row[c] for c in cols])
  db.execute('DELETE FROM sqlite_sequence')
  for name,seq in v['sequence'].items():db.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)',(name,seq))
  if db.execute('PRAGMA foreign_key_check').fetchall():raise Invalid('Invalid record relationships')

def tunnel(v):
 s=v['tables']['interfaces_table'][0]
 text='[Interface]\nPrivateKey = '+s['private_key']+'\nAddress = 10.77.0.1/24\nListenPort = 51820\nMTU = 1380\nFwMark = 51853\nPostUp = /usr/bin/python3 /usr/local/libexec/desk-vpn/firewall.py up\nPostDown = /usr/bin/python3 /usr/local/libexec/desk-vpn/firewall.py down\n'
 peers={}
 for r in v['tables']['clients_table']:
  if not r['enabled']:continue
  peers[r['public_key']]=r['ipv4_address']+'/32'
  text+='\n[Peer]\nPublicKey = '+r['public_key']+'\nAllowedIPs = '+peers[r['public_key']]+'\n'
  if r['pre_shared_key']:text+='PresharedKey = '+r['pre_shared_key']+'\n'
 return text,peers

if __name__=='__main__':
 import sys
 if len(sys.argv)!=3 or sys.argv[1]!='apply':raise SystemExit(2)
 apply(sys.argv[2],json.load(sys.stdin))
