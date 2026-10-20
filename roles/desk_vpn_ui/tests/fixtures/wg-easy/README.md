# Pinned wg-easy migration fixture

These files are copied without modification from wg-easy v15.3.0, revision
`2dc8ba779216929c10c1998341d36963fe0eca7a`, matching `scripts/build-vpn-ui.py`:

- `migrations/*.sql` and `migrations/meta/_journal.json` come from
  `src/server/database/migrations/` at that revision.
- `LICENSE` is the upstream GNU Affero General Public License v3 file.
- `manifest.json` records the source revision and SHA256 of every copied file.

Source: https://github.com/wg-easy/wg-easy/tree/2dc8ba779216929c10c1998341d36963fe0eca7a/src/server/database/migrations

`test_sync.py` applies the real migration chain through `seed.make()` to temporary
SQLite databases. It generates synthetic keys at runtime; no database, seed,
credential or production state is stored here. Drizzle schema snapshots are
omitted because the migration runner does not consume them.

The default tests need no upstream checkout or network access. An explicit
`WG_EASY_SOURCE` still selects an external checkout's migration directory; a
missing or incompatible external source must fail rather than silently fall back.
The bundled fixture integrity check runs in either mode. When changing the
application pin, refresh the SQL, journal, license and manifest together, then
run the seed/sync regressions against that exact revision.
