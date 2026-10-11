#!/usr/bin/env python3
"""Disable local AdGuard UI auth while retaining policy and protected peer sync."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import yaml

CONFIG = Path('/var/lib/desk-adguard/adguard-home.yaml')
AGENT = Path('/etc/desk-dns/sync-agent.json')

def candidate(document, config):
    if document.get('schema_version') != 34:
        raise ValueError('Unsupported schema')
    original = dict(document)
    original['users'] = []
    agent = dict(config, local_auth=False)
    return original, agent

def atomic(path, data):
    metadata = path.stat()
    descriptor, temporary = tempfile.mkstemp(prefix='.ui-auth-', dir=path.parent)
    try:
        os.fchmod(descriptor, metadata.st_mode & 0o777)
        os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def configure(binary):
    document = yaml.safe_load(CONFIG.read_bytes())
    agent = json.loads(AGENT.read_bytes())
    wanted, wanted_agent = candidate(document, agent)
    if wanted == document and wanted_agent == agent:
        return False
    subprocess.run(['systemctl', 'stop', 'desk-adguard'], check=True, capture_output=True, timeout=15)
    original = CONFIG.read_bytes()  # Stop first to retain the final UI writes.
    original_agent = AGENT.read_bytes()
    metadata = CONFIG.stat()
    descriptor, temporary = tempfile.mkstemp(prefix='.ui-candidate-', dir=CONFIG.parent)
    try:
        document, wanted_agent = candidate(yaml.safe_load(original), json.loads(original_agent))
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
        with os.fdopen(descriptor, 'w') as stream:
            yaml.safe_dump(document, stream, sort_keys=False)
        subprocess.run([binary, '--check-config', '--no-check-update', '-w', str(CONFIG.parent), '-c', temporary], check=True, capture_output=True, timeout=10)
        backup = CONFIG.parent/'pre-ui-auth.yaml'
        if not backup.exists():
            with backup.open('xb') as stream:
                stream.write(original)
            backup.chmod(0o600)
        atomic(CONFIG, Path(temporary).read_bytes())
        atomic(AGENT, (json.dumps(wanted_agent, indent=2)+'\n').encode())
        subprocess.run(['systemctl', 'start', 'desk-adguard'], check=True, capture_output=True, timeout=15)
        return True
    except Exception:
        atomic(CONFIG, original)
        atomic(AGENT, original_agent)
        subprocess.run(['systemctl', 'start', 'desk-adguard'], check=True, capture_output=True, timeout=15)
        raise
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--binary', default='/opt/desk-adguard/v0.107.79/adguard-home')
    args=parser.parse_args()
    try:
        print('changed' if configure(args.binary) else 'unchanged')
    except Exception:
        raise SystemExit('AdGuard auth migration failed; retained policy and credentials')
