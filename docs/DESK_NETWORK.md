# LAN-only Network service

Open `http://192.168.1.186/network/`. All three existing UI proxies preserve the
route. One NetAlertX collector scans `192.168.1.0/24` on `wlan0`; two standby replicas
retain certified checkpoints and do not run owner scans. All three nodes are configured
for warm standby; exactly one runs the active native backend/PHP/cron groups. No new LAN VIP,
modem DHCP change or WireGuard discovery is involved.

Source and shared configuration define the supported state: distributed ARP/mDNS,
three warm replicas, bounded proxying and Rust checkpoints. Inspect live status
before operating; no owner, revision or measured failover time is pinned here.

## Deployment

Build the local ARM64 release artifact as described in [Rust checkpoint worker](#rust-checkpoint-worker) before either deployment playbook.

```sh
python3 scripts/create-network-secret.py
ansible-playbook -i inventory.ini playbooks/network/network.yml --syntax-check
ansible-playbook -i inventory.ini playbooks/network/network.yml
```

Initial import can retain existing device data with a
SQLite online backup, including WAL. The initial manifest and compressed
checkpoint are kept in ignored `secret/network/seed/`, mode 0600. Keep this
material with peer TLS credentials for redeployment; the role never overwrites
an existing replica head with the initial seed. The first migration requires
`/var/lib/desk-netalertx-trial/data` on node 1. Later deployments reuse the saved
seed and each node catches up to the latest certified checkpoint.

For helper/checkpoint changes use `playbooks/network/network-helpers.yml --skip-tags homeassistant`.
Use `playbooks/network/network.yml` for the complete deployment.

The official ARM64 26.10.0 image is digest-pinned. Podman is packaged, daemonless
and has no automatic image update/restart jobs. The systemd controller starts
after networking and dashboard telemetry. Its stop hook kills the container PID
namespace before cleanup. Container root is read-only, with bounded temporary
RAM storage, a 512 MiB memory ceiling, 256 PIDs, low CPU weight and a 0.5 CPU
ceiling (12.5% of the four-core node). The controller has a separate 256 MiB
ceiling and bounded concurrent HTTP handlers. These are ceilings, not measured
idle use.

Only native required network/UID capabilities are retained in the container.
The native API has a narrowly patched loopback bind; its port 20212 is not
exposed on the network. The frontend at private port 20211 is reached through
the controller proxy at 20213. Peer RPC uses pinned TLS on private port 20215,
exact source membership, HMAC body signatures, timestamp and replay checks.
Public access follows the existing trusted LAN/VPN UI model. No WAN HTTP port
is added. API tokens, device lists and peer keys are not logged.

## Discovery and UI

ARP discovery defaults to every five minutes. The native Settings scan cron
(`ARPSCAN_RUN_SCHD`) is user-editable and replicated with configuration; it is
no longer an immutable override. `Scan now` queues only ARPSCAN. Native mDNS name resolution runs every five minutes, bounded as described below.
Other name-lookup scans, private-subnet scans and WireGuard-subnet scans are excluded.
LAN scope, service addresses, enabled scan engines/arguments and resource limits
are cluster managed. Those controls are explicitly marked and disabled on the
native Settings page; an attempted change is rejected before the native write
with a named HTTP 400 error, surfaced on the page. Other settings retain the
native save path. History retention is bounded.
This provides inventory, names/groups, presence and in-app events; it does not
control modem DHCP reservations or disconnect clients from Wi-Fi.

The native PHP translation helper caches its merged language data once per
request, preserving language selection and next-request config updates. This
avoids repeatedly parsing every language file for each menu label. Browser
storage is namespaced `desk_network:` so native cache reset cannot erase
Xiaozhi, AdGuard, Beszel or VPN browser state on the same origin. Subfolder URLs
are rewritten only for the NetAlertX boundary. On owner change the native page
reloads to refresh its client cache/session.

## Ownership and recovery

A candidate needs two of three lease grants. A voter cannot replace an
unexpired grant, persists its term only when it changes, and waits 17 seconds
after restart before voting. Idle renewals remain in RAM. CLOCK_BOOTTIME includes
suspend time. The active lease expires five seconds before voter grants; a
watchdog kills the container namespace when renewal is lost. A healthy owner
stays put. New placement uses fresh CPU plus the Network, Home Assistant, voice,
DNS and VPN role costs and an overload penalty for a node already holding two
other roles. Maintenance withdraws an owner, and an
overloaded owner releases Network after a certified checkpoint; a telemetry refresh
alone does not.

Two replicas are required for election and mutations. Before forwarding a
native mutation, the boundary verifies a current lease quorum. A SQLite online
backup, app.conf and optional workflows.json snapshot is then staged durably on at least two replicas,
with signed receipts, before a certified checkpoint is acknowledged. Native
errors/status codes are retained. A native edit whose replication becomes
unavailable is explicitly labelled `Sync pending`; do not treat it as a durable
cluster commit until the status becomes `Synced`. Discovery/history snapshots
are periodic (five minutes); unreplicated observations can be lost if the owner
disappears. Live SQLite/WAL files are never copied between running collectors.

Standbys download and verify committed checkpoints. Promotion restores the
latest verified database/config before starting the active native runtime. Cold
replicas include full native bootstrap time; warm replicas retain imports
between activations as described below. Neither mode is seamless session
migration. One remaining node cannot elect a collector or acknowledge
shared edits. Returning nodes catch up without preempting a healthy owner.

The dashboard shows `Network owner` only for fresh public metadata from a
native-ready owner. Each node's details also shows collector/starting/standby,
LAN scope, the collector scan interval, and its certified checkpoint sync status.
The dashboard reads the bounded public metadata every collection cycle; data older
than 15 seconds is unavailable. A replica behind another observed checkpoint is
shown as Pending. Voter restart quarantine is shown as Warming up. An Open network
link goes to the existing HA route. No additional discovery or RPC polling is
introduced, and updating this view only restarts the dashboard collectors.
Private files under `/etc/desk-network` remain inaccessible
to the read-only dashboard collector.

## Hostnames and distributed scans

No unattended modem login or DHCP collector is installed. Devices without mDNS
can retain native manual names; reverse DNS/NetBIOS are not scan sources.


The Network owner runs `AVAHISCAN` every five minutes through the native plugin
scheduler, using `files/mdns.py` instead of the pinned resolver's system-DNS
lookup. There is no additional daemon, modem login, NetBIOS scan or dependency
installation: dnspython 2.8.0 is already in the pinned image. Inactive warm standbys do not run native jobs; the separate scan workers
remain available to the owner.

ARP/mDNS adapters write the pinned native 13-column result format directly, with
UTC timestamps and empty results on scan failure. They do not import the native
plugin helper's settings/timezone stack for each job. Native plugin ingestion,
distributed worker selection and the configured scan schedules remain in place.

Only present, non-archived IPv4 LAN devices with unknown names are eligible.
USER/LOCKED names are excluded. Each run selects at most 32 devices; one UDP socket
bound to wlan0 sends multicast and direct mDNS PTR queries, then checks matching
A answers from the same target before publishing one unambiguous `.local` host
name. DNS compression and RDATA use dnspython; mDNS cache-flush class bits are
normalized before decoding. Query IDs, reply source/port, TTL and LAN/MAC identity
are checked; native name matching by IP alone is disabled to avoid DHCP reuse; identity and manual-name eligibility are checked again after lookup.

Two receive windows total at most five seconds, with a 256-packet limit.
The native plugin timeout is thirty seconds (including distributed retries), and the existing 0.5 CPU/512 MiB
container limits are unchanged. Failed lookups are cached for ten minutes;
positive entries expire at the shortest PTR/A TTL, capped at ten minutes. Cache
is RAM-only, keyed by MAC plus IP, and disappears on owner restart/failover.
There is no persistent retry queue. Device names use native AVAHISCAN result
objects and SET_EMPTY authority; names are included in ordinary certified HA
checkpoints. Lookup results never create devices or mark them online.


For an existing installation, deploy the resolver with
`ansible-playbook -i inventory.ini playbooks/network/network-mdns.yml`. This stages all replicas,
refreshes cold standby voters one at a time, refreshes the original owner last,
and waits for one native-ready owner. Only Network is restarted; its UI can
briefly show the recovery page during promotion/bootstrap. Full `playbooks/network/network.yml`
installations include the same adapter and policy.

Offline resolver tests use dnspython from `roles/desk_network/tests/requirements.txt`
(the same version already bundled in the production image). They use packet
fixtures and temporary SQLite files; they do not contact LAN devices or the modem.

## Owner-routed distributed LAN scans over Core NATS

Enable `desk_network_scan_enabled` and create ignored credentials once with
`python3 scripts/create-network-scan-secret.py`. Deploy existing nodes using
`ansible-playbook -i inventory.ini playbooks/network/network-distributed.yml`; new installations
use the same worker tasks from `playbooks/network/network.yml`. The NATS role template retains
these users when reapplied. Existing Xiaozhi users, credentials and routes are
preserved. The narrow rollout validates the existing broker config and reloads
authorization with SIGHUP; it does not restart NATS.

Each node has one `desk-network-scan` service with two scoped connections:
coordinator credentials may publish only scan/status requests and subscribe
to replies addressed to that node; worker credentials subscribe only to that
node's scan/status subjects and may send one response per delivered request.
Subjects are `desk.network.scan.<node>`, `desk.network.status.<node>` and
`desk.network.reply.<owner>.<random>`. Jobs/results are bounded HMAC-signed JSON,
with fresh attempt IDs, current owner lease term, selected worker, plugin,
LAN-only target MAC/IPs and an eight-second mDNS / eighteen-second ARP deadline. These signatures provide
integrity; the existing private Core NATS transport is not encrypted. No
JetStream, durable task queue or second NetAlertX instance is introduced.

The native AVAHISCAN adapter sends eligible cache misses through a group-limited
Unix socket into the local coordinator. Only the current Network owner can
dispatch. It queries worker availability and fresh node telemetry in parallel,
rejects stale/offline/missing-admission, maintenance and active voice nodes, then
selects the lowest CPU/role-cost score with a preference for off-owner work. It
publishes to that node's exact subject, without queue-group selection. Workers
recheck the live owner lease and their own admission before and after scanning.
They run only the mDNS and fixed LAN ARP allowlist entries, at most one job per node, and have no
access to the NetAlertX database or modem credentials.

A failed attempt may be retried on one other eligible node after the first
attempt deadline, preventing overlapping retries. Replies from the old term,
wrong attempt/worker, expired jobs or malformed results are discarded. A failed
transport/worker never creates negative hostname cache entries or device-offline
events. No eligible workers leaves existing names untouched until a later cycle.
The owner adapter rechecks current MAC/IP and user-name eligibility before
feeding the unchanged native naming/checkpoint pipeline. Ownership and durable
state continue to use the existing quorum lease and certified checkpoints; NATS
is only the execution transport.

The worker runs as a separate non-root user with only CAP_NET_RAW for arp-scan, with
CPUQuota=15%, MemoryMax=64 MiB, TasksMax=16 and a RAM-only runtime directory.
The dedicated venv pins SHA256-verified nats-py 2.13.1 and dnspython 2.8.0 wheels.
Existing native container CPU/RAM limits are unchanged. Both mDNS and ARPSCAN
are owner-routed over the same NATS transport. ARP always scans only
192.168.1.0/24 on wlan0, using three retries, 128 kbit/s bandwidth and a
12-second subprocess timeout. Host vendor files are deliberately skipped; the
native VNDRPDT cached MAC lookup still enriches vendors after import. No worker accepts arbitrary command arguments.
The native ARPSCAN CMD adapter retains the user schedule and imports the same
Plugin_Objects / CurrentScan format. It clears the previous result before dispatch.
A failed worker, malformed result or scan missing gateway 192.168.1.1 produces
no rows, so the pinned native execute_plugin skips CurrentScan promotion rather
than fabricating an all-offline cycle. Successful scans deduplicate VIP aliases
by MAC, prefer physical node IPs, and include the scanning node's verified local
interface identity (ARP cannot discover itself). An inconclusive cycle retains
last-known presence until a later successful scan; this is not fresh health.
ARP jobs get at most two sequential 18-second attempts (45-second local socket
timeout, 60-second native plugin timeout). Scheduled mDNS remains cached.
If distributed execution is disabled, the adapter delegates to the unchanged
native ARPSCAN script and subnet parameter.

Validation includes offline protocol/selection/retry/fencing/admission tests
and `roles/desk_network/tests/verify_nats_transport.py`, which uses an isolated
loopback NATS broker, fake credentials and fake resolver. It tests real directed
request/reply and rejects worker/coordinator access to foreign subjects. It does
not connect to the production broker or perform a LAN scan.

Use `playbooks/network/network-scan-workers.yml` for worker-only refreshes; it preserves mounted
runtime directories and does not restart the native UI. Mount/policy changes
require `playbooks/network/network-distributed.yml` or the full deployment.

## Scan progress and inventory freshness

`GET /network/desk/scan-status` exposes only bounded public discovery metadata:
owner, ARP/mDNS phase, selected worker, timings, counts and the age of the last
native inventory import. It never exposes MAC/IP lists, arbitrary errors or
credentials. Coordinator observations live in RAM and are fenced by owner term.
A job cannot remain Running indefinitely after its observer disappears.

Inventory freshness comes from the native SQLite `Online_History` record, not
the last HTTP 200 or NATS result. That record already travels in certified
checkpoints; a new owner can label restored data as stale without inventing a
fresh scan. A missing/unreadable timestamp remains Unknown. For a uniform cron,
freshness expires after three scan intervals, with a three-minute floor; custom
cron uses a fifteen-minute threshold. A failed scan retains the previous import
timestamp. It never refreshes presence by itself.

Scan now returns 202 with a request timestamp and suppresses overlapping requests.
Progress distinguishes Queued, Selecting worker, Scanning, Retrying, Importing
and Complete. Complete requires native history to confirm import, not just a
worker response. New-device/IP-change counts compare the observation to the
owner's inventory read-only; they are shown as results after native import.
Native presence processing can finish after the worker scan because it follows
the upstream scheduler. Polling pauses when hidden and uses request deadlines.

The native sidebar has an expandable Discovery section; no extra sticky header
is added. The root dashboard puts the same information only in the Network
owner's node details. Other node cards and the resource-monitoring UI remain
unchanged. mDNS cache-only cycles are labeled Cached, without implying another
LAN scan was dispatched.

## Warm standby and startup

The digest-pinned image is unchanged. Read-only service-script bind mounts let
`warm_runtime.py` preload upstream Python modules and OpenAPI/schema registration
without calling `main()`. It suppresses only import-time asynchronous logging,
then verifies exactly one OS/Python thread and no open native database handles.
The parent retains imports in RAM; it is not a paused live database connection or
a checkpoint of a running process. Idle nginx remains available only on the
existing internal listener; the HA proxy never routes to an inactive replica.
PHP, cron, native API, backend jobs and owner scans are inactive on that replica.
The separate distributed scan worker remains usable by the current owner.

Promotion first restores the newest majority-certified checkpoint while all
writer groups are stopped. A pristine fork then runs native `main()` with fresh
SQLite/config/cache state, and new PHP/cron process groups start. Settings/API
initialization still runs on each activation; full application initialization
is not cached. Readiness requires the current activation identity, the normal
authenticated API health check and regenerated settings/language API files.
A root-owned, read-only mounted control file carries an absolute BOOTTIME lease
deadline. The native parent checks it independently of the controller. Expiry or
demotion kills and reaps all three writer groups before acknowledging inactive;
RAM API files are then cleared. If the host cannot obtain that acknowledgment
within 1.5 seconds, it destroys the entire native PID namespace instead of
preserving an uncertain warm process. Expired/superseded leases cannot be
reauthorized by a late UI request or renewal response.

A local root-only `touch /run/desk-network/warm-drain` requests a planned handoff:
the controller checkpoints, withdraws readiness, stops writers, retains the
preloaded parent and excludes itself from election. Removing that file makes it
eligible again without preempting a healthy owner. Loss of lease still uses the
existing majority/expiry rules; planned handoff does not bypass voter TTLs.
Warm-ready replicas have a candidate preference; existing maintenance/admission
checks still apply. A cold start or controller restart must preload again.

Shared `desk_network_warm_nodes` selects all three deskboxes. `playbooks/network/network-warm.yml`
prepares standby nodes serially, then refreshes the original owner, with readiness,
voter quarantine and protected-service gates. RAM imports are lost on container
restart or power loss. Warm promotion still includes restore, application setup,
election and routing; it is not seamless failover.

The container is still created at 0.5 core. On a node with fresh telemetry,
CPU below 50% and no ASR/LLM/TTS activity, the controller may temporarily raise
only that verified container's cgroup to one core. It pins an open descriptor
after checking PID start identity and the libpod cgroup path. Other cgroups or
unexpected existing quotas are left untouched. The existing watchdog restores
0.5 core on readiness, voice activity, stale telemetry or the 60-second deadline;
container stop closes the descriptor. The burst's own CPU usage does not cancel
itself. No added service, dependency, image rebuild, relaxed memory limit or
steady-state CPU increase is required. `boot_ready_seconds` in Network metadata
reports actual local native readiness time for the current container.

## Proxy and read budgets

The controller now relays non-rewritten read responses in 64 KiB chunks, including
unknown-length/chunked upstream responses. It retains the 16 MiB total response
ceiling; an oversized or truncated stream closes the connection rather than
appending a second HTTP response. SSE uses bounded chunks instead of unbounded
line reads and stops on owner loss. These streamed responses use close framing;
HEAD retains upstream Content-Length without reading a body. Cookie paths,
redirect prefixes and owner/sync headers remain in the proxy path.

UI proxy admission is capped at eight requests before reading their bodies.
Whole-body text rewrites and mutation responses share two buffer slots, with a
two-second bounded wait and a 2 MiB input ceiling per response. The slots remain
held through the client write and release on errors. Excess admission returns
503 with Retry-After and closes the unread request connection. Health/status
shortcuts and peer/lease workers do not acquire this UI budget. The text and
mutation response ceiling can reject unusually large custom pages/results.

Mutation responses still finish reading their bounded body, attempt the existing
checkpoint, then return the native result and current sync header. A failed
checkpoint retains the existing pending-replication behavior. Native SSE import
semantics, authentication, managed-settings checks and quorum rules are retained.
Mutation buffer admission occurs before forwarding the write, so a full buffer
budget cannot reject an edit after applying it. An SSE response releases that
reservation before the long-lived bounded-chunk relay.

Cold startup and warm activation now invoke restore in the existing bounded
checkpoint worker slice. Compressed input and credentials go through private
stdin, with no token in argv/environment. The original restore routine still
validates the envelope and SQLite image before writing target files. Scratch
directories are private and removed after worker exit; errors propagate without
authorizing native activity. The warm activation lease check still follows
successful restore. MemoryMax, worker resource budgets and lease timing are
unchanged. Restore adds worker startup overhead; no failover timing is guaranteed.

The Network peer client reuses at most two pinned-TLS connections per peer and
channel. Lease/status and checkpoint traffic have separate connection budgets and
executors. Every request still verifies its HMAC, timestamp and nonce; uncertain
POSTs are never replayed automatically. HTTP/1.0 peers remain compatible during
rolling updates. Idle pooled sockets are discarded on checkout after eight seconds; server-side
request admission and idle timeouts remain bounded. TLS responses disable Nagle buffering.

Periodic checkpoints may skip backup/compression only after a successful certified
snapshot of the same source, term and head digest. A retained read-only SQLite
connection observes external commits through `data_version`, and database inode,
configuration and workflow contents are checked too. Missing/broken metadata
falls back to a full checkpoint. UI writes, rebalance and drains always request
full snapshots. The detector never holds a read transaction across calls. Fencing retires its
connection, and both cold restore and warm promotion close it before replacing
the database; late jobs cannot reopen a retired detector.

Static API read models use connection-local TEMP table-revision triggers for the
exact pinned SELECTs. Resource_History writes do not invalidate Settings or
language metadata on the same connection. Unchanged exact table queries reuse
their native read models without periodic JSON rebuilds. External commits and
schema changes invalidate immediately; unknown SQL retains the thirty-second age
bound. Force/ad-hoc requests still bypass the cache. TEMP state is not replicated and disappears when
the native connection closes. No persistent schema or scan cadence changes.

The same-origin `/network/server/` namespace forwards to the authenticated native
API on loopback port 20212. Static/PHP pages continue to nginx. API writes still
require the existing lease quorum and certified snapshot; native token checks are
unchanged.

For noisy devices, native **Alert Events** can be disabled per MAC. This filters
ordinary connection/disconnection reports without deleting event history or
changing device presence, New Device or Device Down semantics. Avoid disabling
all event reports globally merely to quiet a few randomized phone identities.

Use `playbooks/network/network-helpers.yml` for helper/checkpoint changes. Camera controller
metadata caching has a separate `playbooks/camera/camera-helpers.yml` rolling update, which leaves
relay definitions and Home Assistant Core unchanged. Its existing
`PartOf=desk-camera.service` relationship also restarts the thumbnail/playback
SDK worker; the helper waits for that worker to finish preloading. It checks
protected services and one ready owner per configured camera before completion.

## Rust checkpoint worker

Rust handles create/validate/restore for configured replicas, through the existing
50% CPU / 384 MiB checkpoint budget. Lease/quorum/fencing and replication protocol
are unchanged. SQLite online backup/WAL, integrity checks, byte/digest
compatibility, Python policy/workflow semantics, ownership and fsync/atomic
replacement are preserved. Limits: 16 MiB compressed, 128 MiB expanded, 64 MiB DB,
1 MiB each configuration/workflows. Credentials remain in private bounded stdin.

Parent-owned 0700 scratch covers all three operations. The unit marker is
published before launch; cleanup requires confirmed cgroup termination; startup
recovers marked orphans before serving peers. Uncertain termination fails closed.
The controller uses `KillMode=control-group`, including pending launchers.
Same-directory restore temps outside runtime scratch still require cleanup/replay
with writers stopped; restore atomicity is per file, not a cross-file transaction.

### Build and deployment

Cross-build the locked ARM64 release on the controller with
[`build-arm64.sh`](../tools/desk-checkpoint/build-arm64.sh), `DESK_ZIG` and
`DESK_ARM64_LIBS`. Link against target Python's SQLite/zlib; changing these can
change backup bytes. No node toolchain is installed. The full role and helper
playbook install binary and bridge together under
`/usr/local/libexec/desk-network/rust` before their caller.

```sh
ansible-playbook -i inventory.ini playbooks/network/network-helpers.yml --skip-tags homeassistant
```

This entrypoint requires all three nodes, uses warm drain and serial controller
refresh, then checks warm readiness, sync, voters and protected services. Without
the skip tag it can also update the Home Assistant election agent on standbys.
Routine deployment does not retain worker/helper rollback copies; recovery needs
a compatible binary/bridge/controller set and the retained certified state and keys.
Do not restore an obsolete Python worker. The controller and Rust policy bridge
remain Python; seed/bootstrap helpers are still required by `seed.py`.

## Focused verification

```sh
python3 -m unittest discover -s roles/desk_network/tests -v
```

Coverage includes lease expiry/restart quarantine, byte-identical streamed checkpoints, checkpoint scratch cleanup,
startup sweep, worker-failure backoff, overload scoring and rebalance deferral, checkpoint certificates,
WAL and workflow inclusion, corruption/expansion rejection, policy preservation, native
mutation classification, bounded peer TLS handshakes, isolated browser storage
and subfolder rewriting. Live rollout additionally
checks native health, navigation, edits, replica catchup and owner interruption.

The warm direct-child integration test requires `waitid` with `WNOWAIT`. It runs
with a real subprocess on Linux and is explicitly skipped on hosts without that
API, including macOS. A separate portable test checks the exact non-reaping,
nonblocking syscall flags, running/exited/reaped results and absence of a `/proc`
group scan. The portable check does not replace real Linux process validation.
To run only the warm tests, set `PYTHONPATH=roles/desk_network/files` and use
`python3 -m unittest discover -s roles/desk_network/tests -p test_warm.py -v`.
