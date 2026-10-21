"""Bounded compatibility operations using the existing Python policy, never DB data."""
import base64
import gzip
import json
import pathlib
import sys


def check_workflows(raw):
    if not isinstance(json.loads(raw), list):
        raise ValueError('workflow list required')


def main():
    action = sys.argv[1]
    if action == 'gzip-header':
        sys.stdout.buffer.write(gzip.compress(b'', compresslevel=1, mtime=0)[:10])
        return
    raw = sys.stdin.buffer.read(3 * 1024 * 1024 + 1)
    if len(raw) > 3 * 1024 * 1024:
        raise ValueError('bridge input limit')
    if action == 'workflows':
        check_workflows(raw)
        return
    if action != 'restore':
        raise ValueError('unknown bridge action')
    payload = json.loads(raw)
    if payload['workflows'] is not None:
        check_workflows(base64.b64decode(payload['workflows'], validate=True))
    conf = base64.b64decode(payload['configuration'], validate=True)
    # Reuse Python repr/regex/unicode and the single authoritative POLICY.
    sys.path.insert(0, str(pathlib.Path(sys.argv[2]).resolve().parent))
    import ha
    sys.stdout.buffer.write(ha.apply_policy(conf, payload['token']))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Input may contain credentials or native settings; never print it.
        sys.stderr.write('checkpoint compatibility helper failed\n')
        sys.exit(1)
