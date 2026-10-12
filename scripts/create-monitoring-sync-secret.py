#!/usr/bin/env python3
"""Create an ignored encrypted Beszel config seed from an existing hub; never print secrets."""
import argparse, ipaddress, json, os, subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def write(path,data,mode=0o600):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
 with os.fdopen(fd,'wb') as output:output.write(data);output.flush();os.fsync(output.fileno())

def prepare(root,source,management,admin_email):
 directory=root/'secret';directory.mkdir(exist_ok=True,mode=0o700)
 files={kind:directory/name for kind,name in {'config':'desk-monitoring-sync.json','seed':'desk-monitoring-seed.fernet','crt':'desk-monitoring-sync.crt','tls_key':'desk-monitoring-sync.key'}.items()}
 if files['config'].exists() and files['seed'].exists():
  config=json.loads(files['config'].read_bytes());seed=json.loads(Fernet(config['key'].encode()).decrypt(files['seed'].read_bytes()))
  if config['source']!=source or seed['source']!=source:raise ValueError('Existing shared seed source differs')
  if not files['crt'].exists() or not files['tls_key'].exists():raise ValueError('Existing sync trust incomplete')
  return False
 if any(path.exists() for path in files.values()):raise ValueError('Partial preparation requires recovery; retain existing secrets')
 if source not in ('deskb1x','deskb2x','deskb3x') or ipaddress.IPv4Address(management) not in ipaddress.IPv4Network('192.168.1.0/24'):raise ValueError('Invalid bootstrap source')
 helper=root/'roles/desk_monitoring/files/config_seed.py'
 result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=4','ops@'+management,'sudo -n python3 - snapshot'],input=helper.read_bytes(),capture_output=True,timeout=30)
 if result.returncode:raise ValueError('Initial snapshot unavailable')
 seed=json.loads(result.stdout);seed['source']=source
 if len(seed['value']['collections']['users'])!=1 or len(seed['value']['collections']['_superusers'])!=1:raise ValueError('Initial migration expects the existing single administrator')
 for collection in ('users','_superusers'):
  if next(iter(seed['value']['collections'][collection].values()))['email']!=admin_email:raise ValueError('Managed admin differs')
 key=Fernet.generate_key();private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 now=datetime.now(timezone.utc);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'desk-monitoring-config')])
 certificate=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
  .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1825))
  .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(f'10.10.10.1{i}')) for i in (1,2,3)]),critical=False)
  .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(private,hashes.SHA256()))
 write(files['tls_key'],private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
 write(files['crt'],certificate.public_bytes(serialization.Encoding.PEM),0o644)
 write(files['seed'],Fernet(key).encrypt(json.dumps(seed,sort_keys=True,separators=(',',':')).encode()))
 write(files['config'],json.dumps({'key':key.decode(),'source':source,'admin_email':admin_email}).encode()+b'\n')
 return True

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--source',required=True);parser.add_argument('--management',required=True);parser.add_argument('--admin-email',default='admin@desk.cluster');args=parser.parse_args()
 try:print('created' if prepare(args.root,args.source,args.management,args.admin_email) else 'unchanged')
 except Exception:raise SystemExit('Monitoring sync seed preparation failed; existing accounts/secrets retained; no credentials printed')
