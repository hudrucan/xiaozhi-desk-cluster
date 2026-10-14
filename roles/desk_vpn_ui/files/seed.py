#!/usr/bin/env python3
"""Create a one-time ignored seed; retain the deployed server and iPhone keys."""
import argparse,base64,hashlib,json,os,secrets,sqlite3,subprocess,sys
from pathlib import Path
import database
from cryptography.fernet import Fernet
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes,serialization
import datetime,ipaddress

def make(migrations, path, credentials, client_private, password_hash):
 db=sqlite3.connect(path)
 db.execute('CREATE TABLE __drizzle_migrations (id SERIAL PRIMARY KEY, hash text NOT NULL, created_at numeric)')
 for entry in json.loads((migrations/'meta/_journal.json').read_text())['entries']:
  sql=(migrations/(entry['tag']+'.sql')).read_text()
  for statement in sql.split('--> statement-breakpoint'):
   if statement.strip():db.execute(statement)
  db.execute('INSERT INTO __drizzle_migrations(hash,created_at) VALUES (?,?)',(hashlib.sha256(sql.encode()).hexdigest(),entry['when']))
 db.execute("UPDATE general_table SET setup_step=0,session_password=?",(secrets.token_hex(256),))
 db.execute("UPDATE interfaces_table SET device='wlan0',private_key=?,public_key=?,ipv4_cidr='10.77.0.0/24',mtu=1380",(credentials['server_private'],credentials['server_public']))
 db.execute("UPDATE hooks_table SET pre_up='',post_up='',pre_down='',post_down=''")
 db.execute("UPDATE user_configs_table SET default_mtu=1380,default_persistent_keepalive=25,default_dns='[\"192.168.1.250\"]',default_allowed_ips='[\"10.77.0.1/32\",\"192.168.1.0/24\"]',host='vpn.huynm.dev',port=51820")
 db.execute("INSERT INTO users_table(username,password,name,role,totp_verified,enabled) VALUES ('cluster',?,'Desk Cluster',1,0,1)",(password_hash,))
 db.execute("INSERT INTO clients_table(user_id,interface_id,name,ipv4_address,ipv6_address,private_key,public_key,pre_shared_key,server_allowed_ips,persistent_keepalive,mtu,enabled) VALUES (1,'wg0','iPhone','10.77.0.2','fdcc:ad94:bacf:61a4::cafe:2',?,?,'','[]',25,1380,1)",(client_private,credentials['client_public']))
 db.commit();db.close()
 value=database.read(path);database.validate(value,value)
 return value

def main():
 p=argparse.ArgumentParser();p.add_argument('--repo',type=Path,required=True);p.add_argument('--upstream',type=Path,required=True);p.add_argument('--node',required=True);a=p.parse_args()
 folder=a.repo/'secret/vpn-ui';folder.mkdir(mode=0o700,exist_ok=True)
 if (folder/'seed.json').exists():print('Existing ignored VPN UI seed retained');return
 creds=json.loads((a.repo/'secret/vpn-trial/credentials.json').read_text());client=(a.repo/'secret/vpn-trial/client.key').read_text().strip()
 assert database.public(client)==creds['client_public'] and database.public(creds['server_private'])==creds['server_public']
 password=secrets.token_urlsafe(48)
 script="const a=require('./src/.output/server/node_modules/argon2'); let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',async()=>process.stdout.write(await a.hash(s,{type:a.argon2id})));"
 hashed=subprocess.check_output([a.node,'-e',script],input=password.encode(),cwd=a.upstream).decode()
 seedpath=folder/'seed.db';value=make(a.upstream/'src/server/database/migrations',seedpath,creds,client,hashed)
 key=rsa.generate_private_key(public_exponent=65537,key_size=2048);subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Desk VPN configuration')]);now=datetime.datetime.now(datetime.timezone.utc)
 cert=x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=5)).not_valid_after(now+datetime.timedelta(days=3650)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('10.10.10.'+str(i))) for i in (11,12,13)]),False).add_extension(x509.BasicConstraints(ca=True,path_length=None),True).sign(key,hashes.SHA256())
 items={'seed.json':json.dumps(value).encode(),'credentials.json':json.dumps({'username':'cluster','password':password,'backend_token':secrets.token_hex(32),'key':Fernet.generate_key().decode()}).encode(),'tls.crt':cert.public_bytes(serialization.Encoding.PEM),'tls.key':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())}
 for name,data in items.items():
  fd=os.open(folder/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'wb') as f:f.write(data)
 os.chmod(seedpath,0o600);print('Saved ignored VPN UI seed and credentials (0600); existing keys retained')
if __name__=='__main__':main()
