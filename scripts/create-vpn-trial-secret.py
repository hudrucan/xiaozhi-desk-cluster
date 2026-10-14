#!/usr/bin/env python3
"""Create local, ignored trial credentials once; never print keys."""
import base64,json,os,sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization
root=Path(__file__).resolve().parents[1]/'secret'/'vpn-trial'
os.umask(0o077);root.mkdir(parents=True,exist_ok=True,mode=0o700)
if sys.argv[1:] == ['--profiles-only']:
    credentials=json.loads((root/'credentials.json').read_text())
    client=(root/'client.key').read_text().strip()
    server_public=credentials['server_public']
else:
    if sys.argv[1:]:raise SystemExit('Use --profiles-only or no arguments')
    if any((root/name).exists() for name in ('credentials.json','server.key','client.key')):
        raise SystemExit('Trial credentials already exist; use --profiles-only to update profiles without replacing keys.')
    def key():
        private=X25519PrivateKey.generate()
        return (base64.b64encode(private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())).decode(),base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode())
    server,server_public=key();client,client_public=key()
    (root/'server.key').write_text(server+'\n');(root/'client.key').write_text(client+'\n')
    (root/'credentials.json').write_text(json.dumps({'server_private':server,'server_public':server_public,'client_public':client_public}))
for name,endpoint in (('iphone-lan.conf','192.168.1.103:51820'),('iphone-wan.conf','171.248.168.14:51820')):
    (root/name).write_text(f'[Interface]\nPrivateKey = {client}\nAddress = 10.77.0.2/32\n\n[Peer]\nPublicKey = {server_public}\nEndpoint = {endpoint}\nAllowedIPs = 10.77.0.1/32, 192.168.1.0/24\nPersistentKeepalive = 25\n')
print('iPhone profiles prepared (existing keys preserved with --profiles-only) in ignored secret/vpn-trial, mode 0600.')
