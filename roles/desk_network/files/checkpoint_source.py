"""Read-only change detector for periodic checkpoints; never a snapshot replica."""
import hashlib
import pathlib
import sqlite3
import threading


class CheckpointSource:
    def __init__(self, data):
        self.data = pathlib.Path(data)
        self.connection = None
        self.identity = None
        self.accepted = None
        self.closed = False
        self.lock = threading.RLock()

    def reset(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = self.identity = self.accepted = None

    def close(self):
        with self.lock:
            self.closed = True
            self.reset()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def stamp(self):
        with self.lock:
            if self.closed:
                raise RuntimeError('Checkpoint source retired')
            path = self.data / 'db/app.db'
            st = path.stat()
            identity = (st.st_dev, st.st_ino)
            if self.connection is None or identity != self.identity:
                self.reset()
                self.connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro',
                                                  uri=True, timeout=.1, check_same_thread=False)
                self.identity = identity
            version = self.connection.execute('PRAGMA data_version').fetchone()[0]
            values = []
            for name in ('app.conf', 'workflows.json'):
                source = self.data / 'config' / name
                if name == 'workflows.json' and not source.exists():
                    values.append(None)
                    continue
                metadata = source.stat()
                if metadata.st_size > 1024 * 1024:
                    raise ValueError('Configuration exceeds checkpoint limit')
                hashed = hashlib.sha256(source.read_bytes()).digest()
                after = source.stat()
                if any(getattr(after, key) != getattr(metadata, key) for key in
                       ('st_dev', 'st_ino', 'st_mtime_ns', 'st_ctime_ns', 'st_size')):
                    raise ValueError('Configuration changed during checkpoint probe')
                values.append((hashed, metadata.st_dev, metadata.st_ino,
                               metadata.st_mtime_ns, metadata.st_ctime_ns))
            return identity, version, tuple(values)

    def unchanged(self, term, stamp, digest):
        with self.lock:
            return not self.closed and self.accepted == (tuple(term), stamp, digest)

    def accept(self, term, before, digest):
        with self.lock:
            self.accepted = (tuple(term), before, digest) if before == self.stamp() else None


def semantic_fingerprint(data):
    """Hash a consistent read transaction; unknown tables/columns stay included.

    Ordering includes SQLite storage types and BINARY collation, independently
    of rowid, page layout, insertion order or declared text collation.
    """
    import base64
    import json
    import time
    data = pathlib.Path(data)
    deadline = time.monotonic() + 45
    digest = hashlib.sha256(b'desk-network-semantic-v1\0')
    def add(value):
        if time.monotonic() >= deadline:
            raise TimeoutError('Semantic fingerprint deadline')
        value = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)
    def configuration():
        result = []
        for name in ('app.conf', 'workflows.json'):
            path = data / 'config' / name
            if name == 'workflows.json' and not path.exists():
                result.append(None)
            else:
                with path.open('rb') as stream:
                    value = stream.read(1024 * 1024 + 1)
                if len(value) > 1024 * 1024:
                    raise ValueError('Configuration exceeds checkpoint limit')
                result.append(hashlib.sha256(value).hexdigest())
        return result
    conf = configuration()
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    ignored = {'appevents', 'online_history', 'currentscan', 'sqlite_sequence'}
    from contextlib import closing
    with closing(sqlite3.connect((data/'db/app.db').resolve().as_uri()+'?mode=ro', uri=True, timeout=3)) as db:
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA temp_store=MEMORY')
            db.execute('PRAGMA cache_size=-2048')
            db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            db.execute('BEGIN')
            if db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0] > 64*1024*1024:
                raise ValueError('Database exceeds checkpoint limit')
            schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
            for kind, name, table, sql in schema:
                if table.lower() not in ignored:
                    add([kind, name, table, sql])
            for kind, name, table, sql in schema:
                if kind != 'table' or name.lower() in ignored:
                    continue
                columns = [row[1] for row in db.execute('PRAGMA table_xinfo('+quote(name)+')')
                           if not (name.lower() == 'devices' and row[1].lower() == 'devlastconnection')]
                add([name, columns])
                if not columns:
                    add(db.execute('SELECT count(*) FROM '+quote(name)).fetchone()[0])
                    continue
                fields = ','.join(map(quote, columns))
                order = ','.join('typeof('+quote(c)+'),'+quote(c)+' COLLATE BINARY' for c in columns)
                for row in db.execute('SELECT '+fields+' FROM '+quote(name)+' ORDER BY '+order):
                    add([[type(v).__name__, base64.b64encode(v).decode() if isinstance(v, bytes)
                          else 0.0 if isinstance(v, float) and v == 0 else v] for v in row])
            add(conf)
            if conf != configuration():
                raise ValueError('Configuration changed during fingerprint')
            return digest.hexdigest()
        finally:
            db.rollback()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    parser.add_argument('--run', required=True)  # Existing bounded-worker recovery marker.
    args = parser.parse_args()
    print(semantic_fingerprint(args.data))
