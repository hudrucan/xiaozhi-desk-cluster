#!/usr/bin/env python3
"""Bootstrap pinned Beszel hubs through their local private API, never reset users."""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import re
import sys
import urllib.request
import urllib.error


def validate(payload, host):
    nodes = payload['nodes']
    if set(nodes) != {'deskb1x', 'deskb2x', 'deskb3x'}:
        raise ValueError('Invalid membership')
    if len(set(nodes.values())) != 3 or host not in nodes.values():
        raise ValueError('Invalid endpoints')
    for address in nodes.values():
        if ipaddress.IPv4Address(address) not in ipaddress.IPv4Network('10.10.10.0/24'):
            raise ValueError('Private endpoints required')
    credentials = payload['credentials']
    if not re.fullmatch(r'[^\s@]+@[^\s@]+', credentials['email']) or len(credentials['password']) < 24:
        raise ValueError('Invalid bootstrap credentials')
    return nodes, credentials


class Hub:
    def __init__(self, host):
        self.endpoint = 'http://' + host + ':8090'
        self.token = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = self.token
        request = urllib.request.Request(self.endpoint + path, data=data, headers=headers)
        with self.opener.open(request, timeout=8) as response:
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError('Oversize response')
            return json.loads(data)


def reconcile(hub, nodes, credentials, verify=False):
    changed = False
    if not verify and hub.call('/api/beszel/first-run')['firstRun']:
        hub.call('/api/beszel/create-user', credentials)
        changed = True
    # A hub using explicitly enabled native AUTO_LOGIN already chooses its own
    # existing account; bootstrap reruns must not reset it to an old email.
    try:
        auth = hub.call('/api/collections/users/auth-refresh', {})
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        error.close()
        auth = hub.call('/api/collections/users/auth-with-password',
                        {'identity': credentials['email'], 'password': credentials['password']})
    hub.token = auth['token']
    user = auth['record']['id']
    info = hub.call('/api/beszel/info')
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?', info['key']):
        raise ValueError('Invalid hub trust key')
    existing = hub.call('/api/collections/systems/records?perPage=100')['items']
    by_name = {record['name']: record for record in existing}
    if len(by_name) != len(existing) or set(by_name) - set(nodes):
        raise ValueError('Unexpected systems; retain user configuration')
    for node, address in sorted(nodes.items()):
        record = by_name.get(node)
        if not record:
            if verify:
                raise ValueError('Missing system')
            record = hub.call('/api/collections/systems/records',
                              {'name': node, 'host': address, 'port': '45876',
                               'users': [user], 'status': 'pending'})
            changed = True
        if record['host'] != address or record.get('port') != '45876' or user not in record['users']:
            raise ValueError('Existing system drift; retain user configuration')
        if verify:
            if record['status'] != 'up':
                raise ValueError('System has no fresh connection')
            records = hub.call('/api/collections/system_stats/records?perPage=1&sort=-created&filter=system%3D%22'
                               + record['id'] + '%22')['items']
            if not records:
                raise ValueError('No sampled metrics')
            stamp = datetime.fromisoformat(records[0]['created'].replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - stamp).total_seconds() > 180:
                raise ValueError('Stale sample')
    return {'changed': changed, 'key': info['key'], 'systems': len(nodes), 'verified': verify}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', required=True)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        raise ValueError('Oversize input')
    nodes, credentials = validate(json.loads(raw), args.host)
    print(json.dumps(reconcile(Hub(args.host), nodes, credentials, args.verify)))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Beszel bootstrap or verification failed; no credentials printed')
