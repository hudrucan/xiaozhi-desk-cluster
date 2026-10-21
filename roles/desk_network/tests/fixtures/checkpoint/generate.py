"""Regenerate small, checked-in Python golden blobs; large cases are generated in tests."""
import base64
import gzip
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[2] / 'files'))
import ha  # noqa: E402 -- repo helper loaded from its source directory


def generate():
    manifest = {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version, 'cases': {}}
    with tempfile.TemporaryDirectory() as temp:
        db = pathlib.Path(temp) / 'source.db'
        with sqlite3.connect(db) as c:
            c.execute('CREATE TABLE devices(name TEXT)')
            c.execute("INSERT INTO devices VALUES ('fixture-đèn')")
        for name, conf, workflows in (
            ('legacy', b"ARPSCAN_RUN_SCHD='*/5 * * * *'\n", None),
            ('workflows', b"# fixture\nTIMEZONE='UTC'\n", b'[{"name":"fixture","enabled":"No"}]'),
        ):
            blob = ha.checkpoint_blob(db, conf, workflows)
            raw = gzip.decompress(blob)
            assert gzip.compress(ha.encoded(json.loads(raw)), compresslevel=1, mtime=0) == blob
            (ROOT / (name + '.gz')).write_bytes(blob)
            manifest['cases'][name] = {
                'sha256': hashlib.sha256(blob).hexdigest(), 'size': len(blob),
                'database_sha256': hashlib.sha256(db.read_bytes()).hexdigest(),
                'configuration': base64.b64encode(conf).decode(),
                'workflows': None if workflows is None else base64.b64encode(workflows).decode(),
            }
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    generate()
