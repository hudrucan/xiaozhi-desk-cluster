"""Bounded verified peer connections; lease traffic has independent capacity.

A failed POST is never retried: its outcome can be uncertain. Old HTTP/1.0 peers
remain compatible, and every request still carries the caller's HMAC envelope.
"""
import http.client
import queue
import threading
import time


class PeerHTTP:
    def __init__(self, context, max_bytes, capacity=2, idle_seconds=8):
        self.context, self.max_bytes = context, max_bytes
        self.capacity, self.idle_seconds = capacity, idle_seconds
        self.lock = threading.Lock()
        self.pools = {}
        self.sessions = {}
        self.closed = False
        self.created = self.reused = self.errors = 0

    def connection(self, host, port, timeout):
        outer = self
        class Connection(http.client.HTTPSConnection):
            def connect(self):
                http.client.HTTPConnection.connect(self)
                self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host,
                                                     session=outer.sessions.get((self.host, self.port)))
        return Connection(host, port, timeout=timeout, context=self.context)

    def post(self, host, port, path, body, headers, timeout, channel='control'):
        deadline = time.monotonic() + timeout
        key = (host, port, channel)
        with self.lock:
            if self.closed:
                raise OSError('Peer transport closed')
            pool = self.pools.get(key)
            if pool is None:
                pool = (queue.LifoQueue(self.capacity), threading.BoundedSemaphore(self.capacity))
                self.pools[key] = pool
            idle, slots = pool
        if not slots.acquire(timeout=max(0, deadline-time.monotonic())):
            raise TimeoutError('Peer channel busy')
        conn = None
        keep = False
        try:
            try:
                conn, at = idle.get_nowait()
                if time.monotonic()-at >= self.idle_seconds:
                    conn.close()
                    conn = None
            except queue.Empty:
                pass
            with self.lock:
                if self.closed:
                    raise OSError('Peer transport closed')
                if conn is None:
                    conn = self.connection(host, port, timeout)
                    self.created += 1
                else:
                    self.reused += 1
            def budget():
                left = deadline-time.monotonic()
                if left <= 0:
                    raise TimeoutError('Peer deadline elapsed')
                conn.timeout = left
                if conn.sock is not None:
                    conn.sock.settimeout(left)
            budget()
            conn.request('POST', path, body=body, headers=dict(headers, Connection='keep-alive'))
            budget()
            response = conn.getresponse()
            chunks, size = [], 0
            while True:
                budget()
                part = response.read(min(65536, self.max_bytes+1-size))
                if not part:
                    break
                chunks.append(part)
                size += len(part)
                if size > self.max_bytes:
                    raise ValueError('Peer response exceeds limit')
            keep = not response.will_close
            if conn.sock is not None:
                session = conn.sock.session
                if session is not None and session.has_ticket:
                    with self.lock:
                        self.sessions[(host, port)] = session
            return response.status, b''.join(chunks)
        except Exception:
            with self.lock:
                self.errors += 1
            raise
        finally:
            if conn is not None:
                with self.lock:
                    if keep and not self.closed:
                        idle.put_nowait((conn, time.monotonic()))
                    else:
                        conn.close()
            slots.release()

    def public(self):
        with self.lock:
            return {'connections_created': self.created, 'connections_reused': self.reused,
                    'request_errors': self.errors}

    def close(self):
        with self.lock:
            self.closed = True
            for idle, _ in self.pools.values():
                while True:
                    try:
                        conn, _ = idle.get_nowait()
                        conn.close()
                    except queue.Empty:
                        break
