#!/usr/bin/env python3
"""Prepare HA phone profiles using existing ignored keys; never print secrets."""
import base64,json,os,secrets
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization
root=Path(__file__).resolve().parents[1]/'secret'/'vpn-trial'
os.umask(0o077)
c=json.loads((root/'credentials.json').read_text());key=(root/'client.key').read_text().strip()
pub=X25519PrivateKey.from_private_bytes(base64.b64decode(key)).public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
if base64.b64encode(pub).decode()!=c['client_public']:raise SystemExit('Client identity mismatch; profiles not changed')
for name,endpoint in (('iphone-ha','vpn.huynm.dev:51820'),('iphone-ha-lan','192.168.1.254:51820')):
 (root/(name+'.conf')).write_text(f"[Interface]\nPrivateKey = {key}\nAddress = 10.77.0.2/32\n\n[Peer]\nPublicKey = {c['server_public']}\nEndpoint = {endpoint}\nAllowedIPs = 10.77.0.1/32, 192.168.1.0/24\nPersistentKeepalive = 25\n")
print('Prepared HA WAN/LAN phone profiles; existing peer keys preserved')

election=root.parent/'vpn-ha-election.json'
if not election.exists():
 ranks=[100,110,120];secrets.SystemRandom().shuffle(ranks)
 with election.open('x') as stream:json.dump(dict(zip(('deskb1x','deskb2x','deskb3x'),ranks)),stream)
 print('Initialized persisted randomized HA election; existing ranks never replaced')
