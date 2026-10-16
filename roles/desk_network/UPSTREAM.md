# Pinned native adaptations

NetAlertX 26.10.0, commit `19aa886432b5dc3ecf1077fe34b77bbd2f75c68e`,
https://github.com/netalertx/NetAlertX/tree/19aa886432b5dc3ecf1077fe34b77bbd2f75c68e

`files/api-loopback.py` retains `server/api_server/api_server_start.py` with the
listen address restricted to loopback. `files/lang-cached.php` retains
`front/php/templates/language/lang.php` with a request-scoped merged translation
cache. The upstream GPL-3.0 license is retained in `LICENSE-NetAlertX.txt`.

Both files are mounted only into the digest-pinned native container; the official
image is not modified or rebuilt. `override-policy.sh` retains the cluster's
read-only override instead of letting the startup hook rewrite it.

## PHP opcode cache

The official image remains unchanged. An official Alpine v3.22 aarch64
`php83-opcache-8.3.29-r0.apk`, matching its PHP 8.3.29, supplies one read-only
module mount and a separate read-only INI. No additional daemon, image build,
package manager or runtime download is used. Source:
https://dl-cdn.alpinelinux.org/alpine/v3.22/community/aarch64/php83-opcache-8.3.29-r0.apk

Package SHA256: `e6f46dec63fdab796e48cd0b3f10b868306dc415c0fc47706771e4ed69d47e38`.
Module SHA256: `ddb9a4d4bf278b975d4327f28b37f6777cb48ac4595f166064b2441a18a3bc22`.
Alpine package license metadata: PHP-3.01, BSD-3-Clause, LGPL-2.0-or-later,
MIT, Zend-2.0. Packages/binaries are downloaded to nodes, not vendored here.
The image, PHP ABI, module version, architecture and hashes must be reviewed
together when upgrading. OPcache uses 32 MiB shared RAM, JIT and disk cache are
disabled, and source timestamps are checked on every request.

## mDNS name resolver

`files/mdns.py` supplies bounded PTR/A mDNS queries through the native AVAHISCAN
CMD override and Plugin_Objects format. It uses dnspython already in the image,
without modifying upstream plugin code or installing a daemon. Native name
resolution and USER/LOCKED field-authority rules remain responsible for updates.

## Distributed execution

The AVAHISCAN adapter can forward cache misses over a group-limited local Unix
socket. The owner coordinator selects a node-specific Core NATS subject; workers
reuse the same bounded mDNS resolver without a database or another NetAlertX
instance. Native result imports and checkpoints remain unchanged. A dedicated
venv pins nats-py 2.13.1 and dnspython 2.8.0 with wheel SHA256 requirements;
package files and credentials are not vendored in this role.

`files/arp.py` adapts fixed-LAN distributed ARP results to the same ARPSCAN
Plugin_Objects schema. Empty/unavailable results clear the old result file and
are skipped by the pinned native execute_plugin path before CurrentScan
promotion. No upstream server/plugin code or image is changed. Host workers
use the distribution's arp-scan package with CAP_NET_RAW only; no modem login.

`discovery.py` reads native Online_History and adapter observations; it changes
neither native database schema nor checkpoint format. `scan-ui.js` adds a
compact sidebar disclosure and tracks native import completion. `bootstrap.py`
manages only a temporary verified libpod CPU budget; upstream startup code and
its services are unchanged.


## Opt-in warm import preloader

`warm_runtime.py` is a cluster-owned lifecycle adapter around the pinned
`server.__main__` module. `warm-backend.sh` replaces the native backend launcher
only on configured warm nodes; `warm-idle.sh` defers PHP/cron launch to that
lease-gated adapter. The image and upstream Python module sources are unchanged.
Forking is restricted to a checked, single-threaded import-only parent with no
open native database descriptor. Upgrade reviews must recheck import-time
threads/side effects, child process groups, native PHP/cron commands, API cache
paths and the normal native readiness contract. This is a warm import cache,
not upstream's scan-pause feature or a supported upstream HA mode.


`api_read_cache.py` wraps the pinned native API endpoint constructor during warm
preload. The parent holds an empty cache; each activation fills its own derived
read-model metadata. No connection is opened during installation. Static models
reuse their native endpoint only while the same SQLite connection's external
`data_version`, local `total_changes` and query match, for at most 30 seconds.
Force/ad-hoc updates and time-sensitive/custom queries retain native behavior;
the existing deferred JSON writer remains authoritative. Re-audit constructor
call sites and query semantics when upgrading the pinned image.
