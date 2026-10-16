#!/usr/bin/env python3
"""Local-only ignored credentials for Core HA; never prints keys."""
import datetime,ipaddress,json,os,pathlib,secrets
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
root=pathlib.Path(__file__).resolve().parents[1]/'secret/homeassistant'
root.mkdir(parents=True,exist_ok=True,mode=0o700)
if (root/'cluster.json').exists():raise SystemExit('Existing credentials retained')
key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Desk Home Assistant private cluster')]);now=datetime.datetime.now(datetime.timezone.utc)
cert=x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=5)).not_valid_after(now+datetime.timedelta(days=3650)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('10.10.10.'+str(i))) for i in (11,12,13)]),False).add_extension(x509.BasicConstraints(ca=True,path_length=None),True).sign(key,hashes.SHA256())
values={'key':secrets.token_hex(32),'encryption_key':Fernet.generate_key().decode()}
nats={n:{'user':'desk-ha-'+n,'password':secrets.token_urlsafe(32)} for n in ('deskb1x','deskb2x','deskb3x')}
for name,raw in {'cluster.json':json.dumps(values).encode(),'nats.json':json.dumps(nats).encode(),'peer.crt':cert.public_bytes(serialization.Encoding.PEM),'peer.key':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())}.items():
 fd=os.open(root/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'wb') as f:f.write(raw)
print('Created ignored Home Assistant cluster credentials (0600)')
