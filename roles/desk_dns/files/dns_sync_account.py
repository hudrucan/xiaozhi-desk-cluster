#!/usr/bin/env python3
"""Provision one automation login while preserving all existing AdGuard policy."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import yaml


def account_candidate(document, config):
    if document.get('schema_version') != 34:
        raise ValueError('Unexpected AdGuard schema')
    users = document.get('users', [])
    wanted = {'name': config['username'], 'password': config['password_hash']}
    existing = [x for x in users if x['name'] == config['username']]
    if len(existing) > 1 or (existing and existing[0] != wanted):
        raise ValueError('Existing automation account differs; explicit rotation required')
    if not existing:
        document['users'] = users + [wanted]
    return document, not bool(existing)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inspect', 'inject', 'install'])
    parser.add_argument('--config', default='/etc/desk-dns/sync-agent.json')
    parser.add_argument('--path', default='/var/lib/desk-adguard/adguard-home.yaml')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_bytes())
    path = Path(args.path)
    original = path.read_bytes()
    document, changed = account_candidate(yaml.safe_load(original), config)
    if args.action == 'inspect':
        print('missing' if changed else 'present')
        return
    if not changed:
        return
    if args.action == 'install':
        backup = Path('/var/lib/desk-adguard/pre-sync-account.yaml')
        if not backup.exists():
            with backup.open('xb') as output:
                output.write(original)
            backup.chmod(0o600)
    metadata = path.stat()
    fd, temporary = tempfile.mkstemp(prefix='.sync-account-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, metadata.st_uid, metadata.st_gid)
        with os.fdopen(fd, 'w') as output:
            yaml.safe_dump(document, output, sort_keys=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    main()
