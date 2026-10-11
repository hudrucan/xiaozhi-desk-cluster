#!/usr/bin/env python3
"""Patch only the existing DNS transport, never replace mutable AdGuard policy."""
import argparse
import copy
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import yaml

PATH = Path('/var/lib/desk-adguard/adguard-home.yaml')
BIN = '/opt/desk-adguard/v0.107.79/adguard-home'

def update(original):
    value = copy.deepcopy(original)
    value.setdefault('http', {}).setdefault('doh', {})['insecure_enabled'] = True
    dns = value['dns']
    # The only HTTP forwarder into AGH is the locally restricted TLS boundary.
    dns['trusted_proxies'] = ['127.0.0.1/32']
    dns['allowed_clients'] = list(dict.fromkeys(dns.get('allowed_clients', []) + ['192.168.1.0/24', 'fe80::/10']))
    for key in ('statistics', 'querylog'):
        config = value.setdefault(key, {})
        config['ignored'] = list(dict.fromkeys(config.get('ignored', []) + ['health.desk-dns.test', 'who.desk-dns.test', 'blocked.desk-dns.test']))
        config['ignored_enabled'] = True
    return value

def atomic(path, raw, mode=0o600):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, mode)
        user = pwd.getpwnam('desk-adguard'); os.fchown(fd, user.pw_uid, user.pw_gid)
        with os.fdopen(fd, 'wb') as target: target.write(raw)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--rollback', action='store_true'); args = parser.parse_args()
    backup = PATH.with_name('pre-client-identity.yaml')
    if args.rollback:
        if not backup.exists(): raise ValueError('No rollback')
        subprocess.run(['systemctl', 'stop', 'desk-adguard'], check=True)
        atomic(PATH, backup.read_bytes())
        subprocess.run(['systemctl', 'start', 'desk-adguard'], check=True)
        return
    original = yaml.safe_load(PATH.read_bytes())
    if update(original) == original: print('unchanged'); return
    subprocess.run(['systemctl', 'stop', 'desk-adguard'], check=True)
    original_raw = PATH.read_bytes(); original = yaml.safe_load(original_raw)
    atomic(backup, original_raw)
    candidate = PATH.with_name('client-identity-candidate.yaml')
    try:
        atomic(candidate, yaml.safe_dump(update(original), sort_keys=False).encode())
        subprocess.run([BIN, '--check-config', '--no-check-update', '-w', str(PATH.parent), '-c', str(candidate)], check=True, capture_output=True)
        atomic(PATH, candidate.read_bytes())
        subprocess.run(['systemctl', 'start', 'desk-adguard'], check=True)
        print('changed')
    except Exception:
        atomic(PATH, original_raw)
        subprocess.run(['systemctl', 'start', 'desk-adguard'], check=True)
        raise
    finally: candidate.unlink(missing_ok=True)

if __name__ == '__main__':
    try: main()
    except Exception: raise SystemExit('Client identity configuration failed; no private data printed')
