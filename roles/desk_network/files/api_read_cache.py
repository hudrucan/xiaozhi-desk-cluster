"""Reuse native read models with connection-local table revisions.

TEMP triggers track the backend's own writes without modifying the replicated
schema. External commits and schema changes invalidate conservatively through
SQLite data_version. Unknown SQL retains the original database-wide stamp.
"""
import time

MAX_AGE = 30
STATIC_FILES = frozenset('table_' + name + '.json' for name in (
    'appevents', 'events', 'events_pending_alert', 'settings', 'plugins_events',
    'plugins_history', 'plugins_objects', 'plugins_language_strings',
    'notifications', 'online_history'))
# Exact pinned queries only; custom SQL and time-dependent views never use a
# guessed dependency. Table names are constants, never network input.
TABLE_QUERIES = {
    'select * from appevents order by datetimecreated desc': 'AppEvents',
    'select rowid, * from events order by evedatetime desc': 'Events',
    'select * from events where evependingalertemail is not 0': 'Events',
    'select * from settings': 'Settings',
    'select * from plugins_events': 'Plugins_Events',
    'select * from plugins_history order by datetimechanged desc': 'Plugins_History',
    'select * from plugins_objects': 'Plugins_Objects',
    'select * from plugins_language_strings': 'Plugins_Language_Strings',
    'select * from notifications': 'Notifications',
    'select * from online_history': 'Online_History',
}


class ReadModelCache:
    def __init__(self, factory, clock=time.monotonic):
        self.factory, self.clock, self.entries = factory, clock, {}
        self.connection = None
        self.schema = None
        self.tables = set()

    def stamp(self, conn, query):
        external = conn.execute('PRAGMA data_version').fetchone()[0]
        schema = conn.execute('PRAGMA main.schema_version').fetchone()[0]
        table = TABLE_QUERIES.get(' '.join(query.split()).casefold())
        if table is None:
            return external, schema, conn.total_changes
        if self.connection is not conn or self.schema != schema:
            self.connection, self.schema, self.tables = conn, schema, set()
        if table not in self.tables:
            conn.execute('CREATE TEMP TABLE IF NOT EXISTS _desk_api_versions '
                         '(name TEXT PRIMARY KEY, revision INTEGER NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO temp._desk_api_versions VALUES (?,0)', (table,))
            for action in ('INSERT', 'UPDATE', 'DELETE'):
                name = '_desk_api_' + table + '_' + action
                conn.execute('CREATE TEMP TRIGGER IF NOT EXISTS "' + name + '" AFTER ' + action +
                             ' ON main."' + table + '" BEGIN UPDATE _desk_api_versions '
                             'SET revision=revision+1 WHERE name=\'' + table + '\'; END')
            self.tables.add(table)
        revision = conn.execute('SELECT revision FROM temp._desk_api_versions WHERE name=?', (table,)).fetchone()[0]
        return external, schema, table, revision

    def __call__(self, db, force, query, path, ad_hoc=False):
        if force or ad_hoc or path.rsplit('/', 1)[-1] not in STATIC_FILES:
            self.entries.pop(path, None)
            return self.factory(db, force, query, path, ad_hoc)
        try:
            conn = db.sql_connection
            stamp = self.stamp(conn, query)
        except Exception:
            self.entries.pop(path, None)
            return self.factory(db, force, query, path, ad_hoc)
        now = self.clock()
        cached = self.entries.get(path)
        if (cached and cached[0] is conn and cached[1] == (stamp, query)
                and now - cached[2] < MAX_AGE):
            return cached[3]
        result = self.factory(db, force, query, path, ad_hoc)
        # Keep the BEFORE stamp so a write during materialization forces the
        # next update; the native endpoint/debounce writer remains the owner.
        self.entries[path] = (conn, (stamp, query), now, result)
        return result


def install(native_api):
    if not isinstance(native_api.api_endpoint_class, ReadModelCache):
        native_api.api_endpoint_class = ReadModelCache(native_api.api_endpoint_class)
