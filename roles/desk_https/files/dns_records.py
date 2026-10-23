#!/usr/bin/env python3
"""Reconcile only owned HTTPS rewrites through the existing certified policy API."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.request

NAMES = ['cluster.huynm.dev', 'ntfy.cluster.huynm.dev', 'ha.cluster.huynm.dev']
BASE = 'http://192.168.1.186/dns/control/'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args): return None


def request(route, value=None):
    data = None if value is None else json.dumps(value).encode()
    req = urllib.request.Request(BASE + route, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=30) as response:
        if response.status not in (200, 202): raise RuntimeError('Policy write not acknowledged')
        body = response.read(1024*1024)
        return json.loads(body) if body else None


def settled(api, sleep=time.sleep):
    for _ in range(120):
        state = api('desk_policy')
        if state.get('error') or state.get('operation') == 'failed': raise RuntimeError('Certified DNS policy failed')
        if state.get('operation') == 'idle' and set(state.get('ready', [])) == {'deskb1x', 'deskb2x', 'deskb3x'} and state.get('revision', -1) >= 0: return
        sleep(1)
    raise RuntimeError('Certified DNS policy still pending; do not promote')


def save(path, owned):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.dns-owned-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(owned, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def reconcile(path, rollback=False, api=request, sleep=time.sleep):
    path = Path(path)
    owned = json.loads(path.read_text()) if path.exists() else []
    wanted = [{'domain': name, 'answer': '192.168.1.186', 'enabled': True} for name in NAMES]
    if not isinstance(owned, list) or any(row not in wanted for row in owned): raise ValueError('Invalid owned DNS records')
    if rollback and not owned: return False
    settled(api, sleep)
    existing = api('rewrite/list')
    # Check every named collision before writing any record; never replace UI policy.
    for row in wanted:
        if any(x['domain'].rstrip('.').lower() == row['domain'] and x != row for x in existing):
            raise ValueError('HTTPS rewrite conflicts with current UI policy: ' + row['domain'])
    changed = False
    for row in (list(owned) if rollback else wanted):
        existing = api('rewrite/list')
        if rollback:
            if row in existing:
                api('rewrite/delete', row); settled(api, sleep); changed = True
            owned.remove(row); save(path, owned)
        elif row not in existing:
            # Record intent before the write so an interrupted request can be recovered.
            if row not in owned: owned.append(row); save(path, owned)
            api('rewrite/add', row); settled(api, sleep)
            if row not in api('rewrite/list'): raise RuntimeError('DNS readback differs')
            changed = True
    return changed


def rollback(path, api=request, sleep=time.sleep):
    """DNS uncertainty must not block removal of HTTPS on surviving nodes."""
    attempted = False
    def tracked(route, value=None):
        nonlocal attempted
        if route == 'rewrite/delete': attempted = True
        return api(route, value)
    try:
        if not Path(path).is_file(): raise ValueError('Ownership journal unavailable on surviving coordinator')
        changed = reconcile(path, True, tracked, sleep)
        return {'status': 'complete', 'changed': changed}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        return {'status': 'pending', 'changed': attempted, 'reason': str(error)}


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--rollback', action='store_true')
    p.add_argument('--state', default='/etc/desk-https/dns-owned.json')
    args = p.parse_args()
    print(json.dumps(rollback(args.state), sort_keys=True) if args.rollback else 'changed' if reconcile(args.state) else 'unchanged')
