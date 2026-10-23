#!/usr/bin/env python3
"""Configure the pinned native publisher through the existing checkpointed UI Save."""
import ast
import json
import re
import sys
import urllib.parse
import urllib.request


def value(row):
    kind = ast.literal_eval(row['setType'])['dataType']
    raw = row['setValue']
    if kind == 'array':
        return ast.literal_eval(raw) if isinstance(raw, str) else raw
    if kind == 'integer':
        return int(raw)
    if kind == 'boolean':
        if raw not in (True, False, 0, 1, 'True', 'False', 'true', 'false', '0', '1'):
            raise ValueError('Invalid native boolean')
        return str(raw).lower() in ('true', '1')
    return raw


def settings(rows, token, base_url):
    if not re.fullmatch(r'[A-Za-z0-9]{32}', token):
        raise ValueError('Invalid publisher token')
    desired = {
        'NTFY_RUN': 'on_notification', 'NTFY_HOST': base_url,
        'NTFY_TOPIC': 'desk-events', 'NTFY_TOKEN': token,
        'NTFY_USER': '', 'NTFY_PASSWORD': '', 'NTFY_PRIORITY': 'default',
        'NTFY_URL_QUERY_STRING': '', 'NTFY_CUSTOM_HEADERS': [],
        'NTFPRCS_INCLUDED_SECTIONS': ['new_devices', 'down_devices'],
        'NTFPRCS_alert_down_time': 5,
    }
    current = {r['setKey']: value(r) for r in rows if not r['setKey'].endswith('__metadata')}
    if not desired.keys() <= current.keys() or 'NTFY' not in current['LOADED_PLUGINS']:
        raise ValueError('Native NTFY plugin is not loaded')
    if all(current[k] == v for k, v in desired.items()):
        return None
    # Native Save replaces app.conf: retain every unrelated setting and its type.
    return [[r['setGroup'], r['setKey'], ast.literal_eval(r['setType'])['dataType'],
             desired.get(r['setKey'], current[r['setKey']])]
            for r in rows if not r['setKey'].endswith('__metadata')]


def configure(open_url, token, base_url, host):
    def read(path):
        with open_url(urllib.request.Request('http://'+host+':20213'+path), timeout=20) as response:
            return json.load(response)
    state = read('/network/desk/status')
    if state['state'] != 'active' or state['sync'] != 'synced':
        raise ValueError('Native owner is not ready and synced')
    rows = read('/network/php/server/query_json.php?file=table_settings.json')['data']
    payload = settings(rows, token, base_url)
    if payload is None:
        return False
    body = urllib.parse.urlencode({'function': 'savesettings', 'settings': json.dumps(payload)}).encode()
    request = urllib.request.Request('http://'+host+':20213/network/php/server/util.php', data=body,
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with open_url(request, timeout=90) as response:
        result = json.load(response)
        if not result.get('success') or response.headers.get('X-Desk-Network-Sync') != 'synced':
            raise ValueError('Native settings Save was not checkpointed; inspect before retry')
    return True


if __name__ == '__main__':
    try:
        args = json.load(sys.stdin)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        print('changed' if configure(opener.open, args['token'], args['base_url'], args['host']) else 'unchanged')
    except Exception:
        # HTTP/config errors can contain credential-bearing payloads.
        sys.exit('Native NTFY configuration failed; inspect owner/checkpoint state')
