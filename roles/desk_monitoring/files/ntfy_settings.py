"""One-shot native Shoutrrr settings update through the existing quorum API."""
import json
import sys
from urllib.parse import urlsplit


def configure(hub, webhook):
    destination = urlsplit(webhook)
    users = [u for u in hub.records('users') if u['email'] == 'admin@desk.cluster' and u['role'] == 'admin']
    if len(users) != 1:
        raise ValueError('Existing Beszel administrator required for private ntfy destination')
    settings = [s for s in hub.records('user_settings') if s['user'] == users[0]['id']]
    if len(settings) != 1:
        raise ValueError('Existing native notification settings required')
    record = settings[0]
    value = dict(record['settings'])
    # Rotate this destination's token without accumulating duplicate webhooks.
    def managed(url):
        parsed = urlsplit(url)
        return (parsed.scheme, parsed.hostname, parsed.port, parsed.path) == (destination.scheme, destination.hostname, destination.port, destination.path)
    value['webhooks'] = [w for w in value.get('webhooks', []) if not managed(w)] + [webhook]
    if value == record['settings']:
        return False
    hub.request('/api/collections/user_settings/records/' + record['id'], 'PATCH', {'settings': value})
    return True


if __name__ == '__main__':
    try:
        # Import the installed client; all mutations still go through its quorum boundary.
        import importlib.util
        spec = importlib.util.spec_from_file_location('beszel_sync', '/usr/local/libexec/desk-monitoring/config-sync.py')
        sync = importlib.util.module_from_spec(spec);spec.loader.exec_module(sync)
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:raise ValueError('Oversize publisher configuration')
        value = json.loads(raw)
        changed = configure(sync.Hub('http://' + value['host'] + ':8091', '/var/lib/beszel-hub/data.db'), value['webhook'])
        print('changed' if changed else 'retained')
    except Exception:
        raise SystemExit('Beszel ntfy integration failed; private values withheld')
