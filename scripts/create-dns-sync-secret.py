#!/usr/bin/env python3
"""Create ignored DNS automation credentials; never read or change admin passwords."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vip', required=True)
    parser.add_argument('--directory', default='secret')
    args = parser.parse_args()
    vip = str(ipaddress.IPv4Address(args.vip))
    directory = Path(args.directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = [directory / ('desk-dns-sync.' + suffix) for suffix in ('json', 'crt', 'key')]
    if all(path.exists() for path in paths):
        print('Existing DNS automation credentials retained')
        return
    if any(path.exists() for path in paths):
        raise SystemExit('Incomplete credential set; preserve and resolve it before retrying')
    password = secrets.token_urlsafe(36)
    result = subprocess.run(['htpasswd', '-niB', '-C', '12', 'desk-sync'],
                            input=password + '\n', text=True, capture_output=True, check=True)
    hashed = result.stdout.strip().split(':', 1)[1]
    with tempfile.TemporaryDirectory() as temporary:
        key = Path(temporary) / 'key'
        cert = Path(temporary) / 'cert'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-days', '825', '-subj', '/CN=Desk DNS policy sync',
                        '-addext', 'subjectAltName=IP:' + vip,
                        '-addext', 'basicConstraints=critical,CA:TRUE',
                        '-addext', 'extendedKeyUsage=serverAuth',
                        '-keyout', str(key), '-out', str(cert)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        values = [json.dumps({'desk_dns_sync_password': password,
                              'desk_dns_sync_password_hash': hashed}).encode(),
                  cert.read_bytes(), key.read_bytes()]
        for path, value in zip(paths, values):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(value)
    print('Created private DNS automation credentials and pinned VIP certificate; admin unchanged')


if __name__ == '__main__':
    main()
