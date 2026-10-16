"""Avoid rebuilding unchanged pinned native API read models.

SQLite data_version detects commits by other connections; total_changes detects
writes on the backend's own connection. Keep the native endpoint/debounce writer
and explicit refreshes. This is derived JSON only, never a database replica.
"""
import time

MAX_AGE = 30
# Only static SQL endpoints: custom/time-dependent queries keep native behavior.
STATIC_FILES = frozenset('table_' + name + '.json' for name in (
    'appevents', 'events', 'events_pending_alert', 'settings', 'plugins_events',
    'plugins_history', 'plugins_objects', 'plugins_language_strings',
    'notifications', 'online_history'))


class ReadModelCache:
    def __init__(self, factory, clock=time.monotonic):
        self.factory, self.clock, self.entries = factory, clock, {}

    def __call__(self, db, force, query, path, ad_hoc=False):
        if force or ad_hoc or path.rsplit('/', 1)[-1] not in STATIC_FILES:
            self.entries.pop(path, None)
            return self.factory(db, force, query, path, ad_hoc)
        try:
            conn = db.sql_connection
            stamp = (conn.execute('PRAGMA data_version').fetchone()[0],
                     conn.total_changes)
        except Exception:
            self.entries.pop(path, None)
            return self.factory(db, force, query, path, ad_hoc)
        now = self.clock()
        cached = self.entries.get(path)
        if (cached and cached[0] is conn and cached[1] == (stamp, query)
                and now - cached[2] < MAX_AGE):
            return cached[3]
        result = self.factory(db, force, query, path, ad_hoc)
        # Record the BEFORE stamp. A write concurrent with materialization must
        # cause a refresh next loop, even if it happened during the SQL query.
        self.entries[path] = (conn, (stamp, query), now, result)
        return result


def install(native_api):
    if not isinstance(native_api.api_endpoint_class, ReadModelCache):
        native_api.api_endpoint_class = ReadModelCache(native_api.api_endpoint_class)
