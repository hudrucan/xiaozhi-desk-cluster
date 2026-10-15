#!/usr/bin/env python3
"""Create once; never print secrets or rotate existing material."""
import base64,json,os,pathlib,secrets,subprocess
root=pathlib.Path(__file__).resolve().parents[1]/'secret/network';root.mkdir(parents=True,exist_ok=True);os.chmod(root,0o700)
config=root/'credentials.json'
if not config.exists():
 p=root/'credentials.tmp';p.write_text(json.dumps({'key':base64.b64encode(secrets.token_bytes(32)).decode(),'api_token':secrets.token_hex(32)}));os.chmod(p,0o600);os.replace(p,config)
key=root/'peer.key';cert=root/'peer.crt'
if not key.exists() and not cert.exists():
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','3650','-keyout',str(key),'-out',str(cert),'-subj','/CN=Desk Network cluster','-addext','subjectAltName=IP:10.10.10.11,IP:10.10.10.12,IP:10.10.10.13'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
if not key.exists() or not cert.exists():raise SystemExit('Incomplete TLS material; restore original files')
for p in (key,cert,config):os.chmod(p,0o600)
print('Network credentials retained in ignored secret/network (0600)')
