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
                if source.stat().st_size > 1024 * 1024:
                    raise ValueError('Configuration exceeds checkpoint limit')
                values.append(hashlib.sha256(source.read_bytes()).digest())
            return identity, version, tuple(values)

    def unchanged(self, term, stamp, digest):
        with self.lock:
            return not self.closed and self.accepted == (tuple(term), stamp, digest)

    def accept(self, term, before, digest):
        with self.lock:
            self.accepted = (tuple(term), before, digest) if before == self.stamp() else None
