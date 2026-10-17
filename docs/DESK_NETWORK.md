# LAN-only Network service

Open `http://192.168.1.186/network/`. All three existing UI proxies preserve the
route. One NetAlertX collector scans `192.168.1.0/24` on `wlan0`; two standby replicas
retain certified checkpoints and do not run owner scans. All three nodes are configured
for warm standby; exactly one runs the active native backend/PHP/cron groups. No new LAN VIP,
modem DHCP change or WireGuard discovery is involved.

## Deployment

```sh
python3 scripts/create-network-secret.py
ansible-playbook -i inventory.ini network.yml --syntax-check
ansible-playbook -i inventory.ini network.yml
```

Initial migration retains the stopped default canary's device data with a
SQLite online backup, including WAL. The initial manifest and compressed
checkpoint are kept in ignored `secret/network/seed/`, mode 0600. Keep this
material with peer TLS credentials for redeployment; the role never overwrites
an existing replica head with the initial seed. The first migration requires
`/var/lib/desk-netalertx-trial/data` on node 1. Later deployments reuse the saved
seed and each node catches up to the latest certified checkpoint.

For changes limited to the Python helpers or the checkpoint slice, use the faster
`network-helpers.yml` described in the 2026-10-17 section; use `network.yml` for
anything else.

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
other roles (see the 2026-10-17 section). Maintenance withdraws an owner, and an
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

## Live verification, 2026-10-15

- Guarded rolling deployment: all three nodes, no protected service/VIP changes.
- HTTP navigation after translation fix: Devices 0.24 s, Notifications 0.27 s,
  Settings 0.63 s; browser Devices/Notifications render with no console errors.
- Disposable device create/edit: native HTTP 200 and Synced; all three signed
  checkpoint receipts present.
- Forced Network controller SIGKILL on node 1: node 3 became native-ready after
  120.61 s. The test record/name/group survived, then disposable data was deleted
  with HTTP 200 and replicated. This tests controller loss, not a physical power
  cut. Native bootstrap dominates the delay; do not advertise instant failover.
- Normal navigation still uses NetAlertX's native full-page PHP transitions.
  It is not a SPA. The optimization removes repeated translation file parsing.

CPU sampling used cgroup CPU time divided by four cores. A 90-second sample
covering a manual ARP scan, six concurrent PHP page loads and browser cache
initialization measured native mean 10.86%, max 12.59%; HA controller mean
3.54%, max 16.51%. Concurrent navigation returned 200 in 0.84–2.96 seconds.
After initialization, a separate 60-second quiet sample with the UI open
measured native mean 1.48% (max 2.00%) and controller mean 1.64% (max 4.81%).
Native cgroup memory was about 114 MiB; controller memory about 165 MiB after
the burst. The controller showed NRestarts=0 after the final rollout. These
are short measurements, not a long soak test; the native CPU quota does not
cap the separate lease/replication controller.

Final deployment returned failed=0/unreachable=0 for all three nodes. Native
Settings finished rendering, with its spinner hidden, after fresh cache
initialization. The final temporary create/update/read/delete sequence returned
200 with Synced. Network had exactly one UP backend; all three Beszel hubs
were UP, with node 3 preferred and existing hub cookies preserved. Xiaozhi, DNS,
Monitoring and VPN routes all returned HTTP 200.

During the later dashboard-details rollout, node 1's Network controller hit its
256 MiB cgroup memory limit and was OOM-killed (NRestarts=1). Node 3 automatically
recovered the collector and the native UI returned 200. The dashboard update does
not issue a Network restart; this independently observed memory failure was initially tracked as an open
controller-stability issue and is addressed by the correction below. The short CPU and memory samples above must
not be read as proof of long-term stability.

## OOM root cause and correction, 2026-10-15

The OOM affected the custom Python HA controller, with a 256 MiB systemd memory
limit. SQLite connection context managers commit/rollback but do not close the
connection. The checkpoint path retained two open connections per snapshot until
GC, including SQLite native caches. The restore integrity check also lacked an
explicit close. Additionally, `read(limit+1)` on buffered gzip/HTTP streams
reserved the full byte ceiling (128 MiB for checkpoint expansion, 32 MiB for peer
responses) even when the actual response was small.

The corrected implementation closes SQLite handles explicitly on success and
exceptions and reads bounded streams in 64 KiB chunks. Limits, checkpoint format,
quorum rules, and resource ceilings are unchanged. A synthetic 2.8 MiB database
on ARM64, capped at 256 MiB and 0.3 CPU, reproduced these results:

| 12 repeated checkpoints | Previous | Corrected |
| --- | ---: | ---: |
| Process RSS after 12 | 73 MiB | 25 MiB |
| Open SQLite connections including harness fixture | 25 | 1 |
| Peak Python traced allocations | 137 MiB | 23 MiB |

The old path reached about 150 MiB RSS by 30 snapshots and retained 155 MiB after
GC; the corrected 12-round path stayed flat at 25 MiB before and after GC.
Regression tests assert handle closure, including failure/restore paths, and
reject allocating a 128 MiB buffer for a small checkpoint. This addresses the
reproduced causes; live stress/quiet verification results are recorded below.

Corrected live rollout completed on all three nodes with failed=0/unreachable=0
and protected service guards passing. A real workload on node 2 performed 80 PHP
page reads (four concurrent at a time), 20 device-field edits plus disposable
record creation/deletion, then 120 seconds without artificial load. The controller
kept the same PID throughout, NRestarts=0. RSS during rounds varied and dropped
again instead of accumulating; the quiet window stayed at 98.35 MiB RSS for
all four 30-second checkpoints. Cgroup sampled peak was 121.43 MiB, final actual
memory.peak including cleanup was 155.53 MiB, against the unchanged 256 MiB
ceiling. Both oom and oom_kill counters were zero. Cleanup returned HTTP 200
with Synced. All existing service UI health checks returned 200 and exactly
one Network backend was UP.

Focused tests: 14 PASS. This is a reproduced-cause fix with repeated workload
verification, not a claim of a multi-day soak.

Final verification after cleanup: all three replicas held checkpoint
`[20, "deskb2x", 27]` and Synced. The disposable device was confirmed absent
directly from that certified checkpoint. The corrected active controller remained
PID 1313560 with NRestarts=0, MemoryMax=268435456, and all memory.events counters
(max/oom/oom_kill/oom_group_kill) zero.


## Browser asset cache, 2026-10-15

The UI proxy previously attached `Cache-Control: no-store` to static assets as
well as application responses. The Network HAProxy backend now overrides this
only for successful GET/HEAD requests to JS, CSS, font and image extensions under
`/network/js/`, `css/`, `lib/`, `img/` and `images/`: browser-private caching for
300 seconds. PHP pages, API responses, application data and errors remain
`no-store`. The short TTL bounds stale assets across a future pinned-image upgrade.

All three proxies were gracefully reloaded with protected-service guards passing;
Network controller PID and restart count were asserted unchanged on every node.
Live headers verified JS/CSS HTTP 200 with `private, max-age=300`, Settings and
controller status HTTP 200 with `no-store`, and missing static assets HTTP 404
with `no-store`. Focused tests: 15 PASS including static whitelist exclusions.

Browser navigation, timed until the Devices table was visible and the loading
spinner disappeared, measured 3.887 seconds before the cache change and 2.731 /
2.909 seconds afterwards. A warmed Settings transition measured 3.406 seconds
(first cache-filling transition: 6.090 seconds). These few samples include browser
automation overhead and are not a controlled latency benchmark. Native full-page
PHP navigation and frontend initialization remain visible; this change does not
remove that architecture or shorten the measured 120.61-second cold failover.


## PHP opcode cache and recovery screen, 2026-10-15

The matching official Alpine PHP 8.3.29 ARM64 OPcache module is checksum-pinned
and extracted as a single verified regular file. It and its INI are mounted
read-only into the unchanged official image. OPcache has a 32 MiB shared-memory
budget, CLI cache/JIT/disk cache are off, and source timestamps are checked on
every request. All three nodes passed an isolated no-network ABI preflight;
standbys were refreshed before the recorded active collector. Protected-service
guards passed on every node and the rollout had failed=0/unreachable=0.
Actual PHP-FPM status, not merely CLI extension loading, confirmed enabled cache,
about 4.52 MiB used, 27 cached scripts, 97.75% hit rate in that short sample,
and zero cache OOM/hash/manual restarts. This does not establish an end-to-end
navigation speedup by itself.

The baseline on node 2 measured median HTML response Devices 247 ms / Settings
526 ms at 0.5 core. The first post-boot sample on new owner node 1 was noisy:
239 ms / 1042 ms. These are different hosts and startup workloads, so that pair
must not be advertised as a controlled OPcache comparison. A later same-owner
quota comparison (six timed reads per page, after a warm-up) measured:

| Native quota | Devices median | Settings median |
| --- | ---: | ---: |
| 0.5 core | 241 ms | 484 ms |
| 0.75 core | 244 ms | 570 ms |
| 0.5 core, repeated | 213 ms | 550 ms |

The short samples do not justify increasing the CPU ceiling. The experiment
restored 0.5 core in a finally block and confirmed `cpu.max=50000 100000`;
memory.events max/oom/oom_kill remained zero. Native memory stays limited to
512 MiB and the separate controller to 256 MiB. Full-page PHP navigation and
frontend initialization remain; no SPA conversion or extra warm standby was added.

During cold boot, the Network backend now serves a self-contained recovery page
from HAProxy, independent of the collector. HAProxy-generated 503s use the managed
HTTP error response; a native 503 is replaced only for top-level HTML GET/HEAD
navigation. HTTP status remains 503, with no-store and Retry-After: 3. Successful
API responses and all existing health probes are unchanged. There are no external
asset dependencies, so the spinner, elapsed time and dashboard/retry controls work
while the backend is down. A single bounded status fetch runs every three seconds
(ten in a hidden document, four-second request timeout). Only active readiness
triggers `location.reload()`, preserving path, query and hash; prolonged waits show
an explicit message instead of a fake percentage.

The recovery page and backend rules were deployed by graceful proxy reload on all
three nodes. The controller PID/restart count and protected services were unchanged
by this UI rollout. A temporary loopback-only HAProxy using the deployed rules
verified 503 recovery HTML, unchanged 503 for unavailable API calls, then native
200 after enabling its backend. Browser verification showed the actual loading UI
and automatic return to the original Devices URL including query and #my_devices.
The isolated proxy, socket and SSH tunnel were removed afterwards; the production
collector was not interrupted for this check. An earlier attempt to preview the
standby LAN IP found that management HTTP binds only the shared VIP; that attempt
restored its temporary admin states immediately and was not a successful recovery
test. Focused Network tests: 21 PASS, including HTTP framing, polling bounds,
ready-only reload, package checksums/ABI metadata, and prior HA regressions.


## Editable scan schedule and truthful Settings behavior, 2026-10-15

The initial cluster policy forced `ARPSCAN_RUN_SCHD=*/5 * * * *` in two places:
the read-only native override and restore-time policy application. Native Save
could return success, then reload would overwrite the user's selected cron.
The cron key is now absent from both enforced policies; restore seeds the
five-minute default only when an older configuration lacks the key. A saved
cron is retained in the certified checkpoint and after promotion/restore.

The Settings integration marks protected controls as Cluster managed, disables
them, and explains the distinction from editable settings. The controller rejects
changed protected values before native saveSettings rewrites app.conf. Equivalent
plugin selections in a different native display order remain accepted. The native
page lacks its own AJAX error callback; the injected integration surfaces rejected
saves explicitly. User configuration still uses the same native Save and majority
checkpoint path, without a new settings service or a separate configuration copy.

Active Network metadata now reads the actual cron when app.conf changes, using
literal parsing without executing configuration and a stat-version cache. It
reports scan_schedule and a uniform scan_minutes interval when applicable; a
standby reports no current running schedule. The Go dashboard accepts bounded
variable intervals instead of requiring exactly five, and displays general cron
expressions without inventing a fixed interval. Periodic observation checkpoints
remain five minutes independently of the user's discovery interval.

Focused Python tests: 25 PASS, including a one-minute cron through a certified
checkpoint and restore, a default for missing legacy settings, protected-write
rejection, equivalent plugin order, and Settings-only labels. Go dashboard tests
and JavaScript detail-render tests PASS for one-minute and general cron values.


Live verification after the schedule correction: native Save from the browser
persisted `*/1 * * * *`; a fresh Settings page showed the same value with the
cron input enabled. app.conf, SQLite Settings and the authenticated native
settings API all reported that cron. The active node and dashboard reported
scan_minutes=1. All three replicas held revision `[22, "deskb3x", 14]` with the
same digest and three signed receipts; each checkpoint's configuration was
checked directly for the saved cron. No additional failover was forced for this
final verification; the focused certified-restore regression covers preservation.

A real browser Save also exposed a native dropdown mismatch: SYNC_RUN was forced
to `disabled`, but the native selector omitted that option and submitted `unused`.
The managed-control helper now inserts the actual cached value as a locked option
when missing, preserving full-form round trips. This passed a dedicated JavaScript
regression and a subsequent native Save. Protected SCAN_SUBNETS changes were
confirmed rejected with a named HTTP 400 before native writes. These UI helpers
were installed on all three nodes without service restarts.


## Settings-to-Notifications navigation correction, 2026-10-15

The managed dropdown repair used `trigger('change.select2')`. jQuery also invokes
the native inline onchange handler for that namespaced trigger, so the native
`settingsChanged()` installed an unsaved-change guard without user edits. Clicking
the notification bell then displayed the generic navigation spinner while the
navigation was cancelled, leaving Settings visible with an indefinite overlay.
The notification page and readiness API themselves returned HTTP 200 promptly.

The helper now temporarily removes only that input's inline onchange while
refreshing the Select2 widget, restoring it immediately in finally. Actual user
change handlers remain intact. Before-unload recovery also dismisses the overlay
on the next event-loop turn if the document stays alive after cancelled navigation;
back-forward cache restoration dismisses stale overlays as well. No unsaved-change
warning is bypassed or removed. A focused JavaScript regression asserts no false
dirty state, retained real-change handlers, cancelled-navigation recovery, and
persisted-page recovery in addition to prior managed-field save checks.

The helper was deployed on all three nodes without restarting services. The exact
browser path Settings -> notification bell subsequently reached userNotifications.php
with 20 rendered rows and hidden spinner in a 670 ms warm sample; no warning/error
console entries were observed. Proof: temporary local notifications-navigation-fixed.png.

## Browser UI audit, 2026-10-15

The audit exercised actual sidebar navigation and waited for populated page content,
not just a hidden global spinner. Covered Devices (populated and empty search),
Device details, Presence, Events, Sent reports, in-app notifications, Change log,
Network topology, App events, Plugins, Workflows, all five System info tabs, and
Maintenance Tools/Backup/Logs/Multi-edit presentation. Init check reached its
success state. Destructive tools, imports, workflow execution and device wake/restart
were not invoked. Settings Save and certified cron preservation are covered by the
preceding live checks; save/navigation helpers also have focused regressions.

Additional reproducible faults found during this audit:

- The pinned Treeviz IIFE includes its demo after the library export. The demo
  throws because `#tree` is absent, even though the real `#networkTree` can render.
  The response adapter removes only the demo of the exact SHA-256-pinned asset;
  unknown versions remain untouched. A versioned asset URL avoids stale browser
  copies. The real upstream bundle passes JavaScript syntax checking after removal.
- Shared sidebar links call `initializeTabs()` on pages such as Workflows that
  do not define it. Inline handlers now check for the function before calling;
  destination page initialization and the function definition remain unchanged.
- The cluster toolbar was under NetAlertX's fixed header. Clicking its home link
  actually hit the native logo and navigated to Devices. The toolbar now reserves
  44 px above the native header/sidebar. Its status text can truncate on narrow
  screens, while both controls remain visible. The Settings policy notice is
  inserted inside the content wrapper, below the fixed navigation.

Focused Python suite: 28 PASS; Settings helper JavaScript regression PASS. This
is a functional navigation audit, not exhaustive verification of every destructive
operation, external integration or long-duration availability.

Post-rollout verification: all three installed controller checksums matched the
candidate. Network owner deskb3x and both standbys reported synced revision
`[24, "deskb3x", 1]`, with three checkpoint receipts. The current controller
cgroups reported zero OOM kills and zero automatic restarts; the active native
container reported OOMKilled=false. This is the observation window of the audit,
not a long-term soak result. All existing root/Xiaozhi/DNS/Monitoring/VPN health
smoke checks returned 200.

Fresh-browser regression passed Settings -> notification bell -> Desk Cluster
home -> Network, then topology -> Workflows -> System info with no JavaScript
errors. The fixed bar occupied y=0..44, native header y=44..94, and Settings notice
started at y=94. At a 390 px viewport the document scroll width stayed 390 px,
both toolbar controls remained inside the viewport, and the device table retained
its own horizontal scrolling. The temporary viewport override was reset. Proofs:
`/private/tmp/desk-network-source/ui-audit-desktop.jpg` and `ui-audit-mobile.jpg`.

## Sidebar navigation and slim header correction, 2026-10-15

The Devices and Settings treeview parent anchors originally carried both a page
href and an inline openUrl() call. From another page the inline call navigated
before AdminLTE could expand the menu. The response adapter now changes only
those two parent anchors into non-navigating treeview toggles. The native logo,
child destinations and tab links retain their behavior.

Following visual feedback, the extra fixed cluster toolbar and its 44 px layout
offset were removed. Cluster home, sync status and Scan now are now one compact
sidebar block. The original NetAlertX header height and positioning are retained.
The Settings policy notice remains in the content wrapper. The focused Python
suite now has 29 passing tests, including parent-only navigation correction and
sidebar placement without another fixed header; Settings helper JavaScript checks
also pass.

## DHCP hostname investigation, 2026-10-15

The authenticated Huawei HG8041X6-12 DHCP Information page exposes leases with
hostnames. Browser observation included `zerow` at .115, `itsnotk1c` at .28,
`iPhone` at .127 and `ZENGGE-da062f` at .8. Some entries have no hostname and some
long names are truncated in the table. Four bounded PTR queries to the modem and
the same four to DNS VIP all returned NXDOMAIN. Enabling reverse-DNS discovery
alone would therefore not recover these observed DHCP names.

The bundled DHCPLSS plugin imports lease files; it is not a Huawei authenticated
UI/API collector. No Huawei-specific importer was found in the pinned plugin tree.
No modem settings, credentials, DHCP service or name-discovery plugin settings
were changed during this investigation.

Further authenticated browser inspection found the modem's reader in
`/html/bbsp/common/lanuserinfo.asp`: GetOldLanUserDhcpInfo() POSTs without a body to
`/html/bbsp/common/GetLanUserDhcpInfo.asp`. Opening that known endpoint through the
same browser session also returned data. Its payload is JavaScript containing
`new DHCPInfo(domain,name,ip,mac,remaintime,devtype,interfacetype,AddressSource,ConnectedTime)`
records, rather than JSON. A future collector must parse the string arguments as
data and must never evaluate the response as code. The browser session was not
copied to nodes. No modem credential was persisted during that browser read. Automated authentication and scheduled import were investigated later but never
deployed; see the hostname collection status below.

The sidebar regression browser test passed expand/collapse from Presence without
changing URL, opening a Settings child, expanding Devices while staying on
Settings, then opening Connected at devices.php#connected. Header geometry was
50 px high at y=0, with no extra top toolbar.

That cold-start navigation additionally exposed the native getApiBase() fallback
`/server` when BACKEND_API_URL had not reached the settings cache. The prefix
adapter previously matched only roots followed by slash/dot and missed a quoted
base URL. It now handles a quoted root too, preserving already-prefixed and
external URLs. The API script URL is versioned for this fix. Focused Python suite:
30 PASS. This corrects the observed Presence API 404 without changing native API
authentication or allowing global /server routes into the cluster ingress.

## Managed Settings readability, 2026-10-15

The native `setting_overriden_by_env::after` overlay placed a repeated opaque
warning across every overridden control. Cluster policy made that native styling
especially noisy. The managed-control helper now marks only cluster-owned rows
and replaces that presentation with a small lock badge below the setting label.
The native overlay/border is suppressed only on those marked controls; readonly
values keep full opacity. Disabled Add/Remove/Change controls and unused list-entry
fields are hidden, while the actual underlying controls remain disabled and the
server-side protected-setting validation remains in force. Editable settings,
including ARPSCAN_RUN_SCHD, retain their original controls and save path.

The follow-up cold-browser Presence test exposed a second bootstrap issue beyond
the /server prefix: Presence and Events invoked main() before API token and
settings finished loading. Both now use native callAfterAppInitialized(), already
used by Device details. Events page size is read after the cache is ready too.
The focused suite has 31 passing tests, including delayed API initialization;
managed-widget tests still verify retained change handlers, disabled policy fields,
editable cron, idempotence and visible save errors.

Final visual preference replaced the label badges with a lighter field overlay:
value at the left, compact “Cluster managed” text at the right, a subtle border
and tint, with no opaque black mask. Input padding reserves label space. The
control surface intercepts clicks, and underlying inputs/selects/buttons remain
disabled. Matching `__metadata` rows are now disabled too; the earlier key-only
loop missed these native override textareas. Disabled Add/Remove controls remain
hidden. Settings helper regression covers metadata lock and overlay hit blocking.
This final presentation supersedes the badge design described above.


## Save-time controller OOM, 2026-10-15

A real Settings save was followed by a `desk-network.service` memory-cgroup OOM,
not a node reboot. The kernel recorded the controller at roughly 252 MiB anonymous
RSS against its unchanged 256 MiB limit. Losing that controller fences the native
container and triggers a cold promotion elsewhere, explaining the minutes-long UI
interruption. A real ~6 MiB SQLite checkpoint used 43.8 MiB Python peak scratch
memory; validation used another 29 MiB.

Checkpoint creation and envelope validation now run in short-lived Python workers.
The parent still waits for the existing majority stage/commit acknowledgment;
checkpoint integrity, WAL backup, signatures, lease renewal and fencing rules are
unchanged. Workers have a 15-second deadline and retain the compressed/decompressed
byte limits. Invalid validation exits fail closed before persistence or receipts.
The 33-test focused suite includes a 6 MiB database worker round trip with parent
allocation bounded below 4 MiB and a corrupt-envelope rejection before staging.

The first live Save tests exposed further retained proxy-buffer RAM despite worker
isolation. The controller unit therefore also bounds glibc arenas to two and fixes
mmap/trim thresholds at 128 KiB. MemoryMax remains 256 MiB. A separate comparison of 36
concurrent HTML rewrites reduced peak RSS from ~65 MiB to ~52 MiB and
settled RSS from ~29 MiB to ~23 MiB. This avoids increasing the service limit to
hide allocation retention.

Native settings-import SSE can precede the synchronous Save response because the
backend imports the file while the HA adapter is still certifying its checkpoint.
The Settings helper defers cache clearing while a Save XHR is in flight. A successful
acknowledgment permits the deferred refresh; a failed Save preserves its error UI.
Tests cover successful deferral, rejected saves and unaffected ordinary refreshes.

Vietnamese was saved correctly during investigation, but the pinned upstream
`vi_vn.json` has 853 empty values and thus falls back to English. Per user request,
no translation changes were made. The modem DHCP hostname source is identified;
no unattended modem login or recurring hostname collector has been enabled yet.


Final rollout and live checks passed on all three nodes. Four Settings saves with
both mitigations retained HA PID 1729263, native PID 1731209 and deskb1x ownership.
The preference round trip UI_DEFAULT_PAGE_SIZE 20 -> 25 -> 20 was observed in both
SQLite and the reloaded form. Controller RSS settled at 33-35 MiB; total unit peak
(including transient workers) was 114,634,752 bytes (~109 MiB), with zero cgroup OOM
or service restart events. The final checkpoint revision [31, "deskb1x", 12] was
synced on 3/3 nodes with three receipts; exactly one Network HAProxy backend was UP.
Dashboard, Network, Xiaozhi, DNS, Monitoring and VPN HTTP checks remained successful.
This is a bounded live Save test, not a long-term memory-soak guarantee.

## Checkpoint worker OOM, tmpfs leak and role balancing, 2026-10-17

On node 2 the kernel repeatedly OOM-killed checkpoint workers (292 kills in the log)
and `/run/desk-network` (tmpfs, RAM) grew to 274 MiB with 257 orphaned `sqlite-*.db`
files. Three causes combined:

- The checkpoint slice and each worker were capped at 256 MiB. Measured peak for a
  27.3 MiB database is about 148 MiB of data path (read 39 MiB, base64 112 MiB,
  `json.dumps` 148 MiB) plus 20-30 MiB interpreter/import overhead, so roughly
  170-180 MiB.
- Cleanup relied on file age (mtime of at least 5 seconds). A worker killed by
  SIGKILL left a partial file that never aged out of the heuristic's reach.
- A failed checkpoint was retried after about 30 seconds, every time, so a node
  below the memory budget produced a continuous OOM loop.

Corrections (checkpoint format, quorum, signatures and lease rules are unchanged):

- Each `create` worker gets a private `ckpt-<token>/` scratch directory under the run
  directory. The parent removes it with `shutil.rmtree` in `finally`, after
  `subprocess.run` has returned, so it also runs when the worker was OOM-killed or
  timed out. No age heuristic remains.
- At controller start, before any worker exists, `sweep_stale_sqlite` removes
  `sqlite-*.db` (and journal/WAL/SHM) left by the previous version, plus orphaned
  `ckpt-*` directories.
- The slice and worker `MemoryMax` are 384 MiB (`desk-network-checkpoint.slice.j2`,
  `checkpoint_budget.py`). Tighten it after reading `VmHWM` from a successful worker:
  deskb2x's slice later showed a 359 MiB `memory.peak`, so the margin is thinner than
  the estimate above (create and validate workers can overlap inside the slice).
- Checkpoint creation streams the database. `checkpoint_blob` base64-encodes the
  SQLite file in 3-byte-aligned 192 KiB chunks and deflates them with
  `zlib.compressobj`, instead of holding raw, base64 and JSON copies. The output is
  byte-for-byte what `gzip.compress(encoded(payload), compresslevel=1, mtime=0)`
  produced, so digests, quorum certificates and mixed old/new replicas are
  unaffected. The gzip header is taken from `gzip.compress` itself because its OS
  byte differs between Python versions (3.11.2 on the nodes writes `03`, 3.14 writes
  `ff`); `GzipFile` would have changed the digest. `unpack` drops the raw JSON and the
  base64 text as soon as they are decoded, with the same checks in the same order.
  On a 20 MiB database the worker's peak RSS fell from 221 to about 134 MiB
  (macOS, Python 3.14; includes ~30 MiB interpreter), and traced allocations for a
  11.5 MiB database fell from 72.7 to 45.9 MiB. The remaining peak is the
  self-check, which still decompresses the whole payload.
- A worker that fails or times out (`subprocess.SubprocessError`) backs the next
  attempt off exponentially: 30, 60, 120, 240, then 300 seconds, reset by the next
  success. Other failures, such as "Checkpoint needs two replicas", keep the previous
  fixed retry. `sync_state` stays `pending` while backing off.

### Role placement and rebalancing

The election score previously gave Network and Home Assistant little or no weight,
so a node already holding several roles looked idle. Node 1 ended up with the
WireGuard VIP, Home Assistant, Network collector and Xiaozhi voice VIP.

| Role held by the node | Score cost |
| --- | ---: |
| Xiaozhi voice VIP | 20 |
| Home Assistant (`starting`, `active` or `checkpointing`) | 20 |
| Network collector (`starting` or `active`) | 20 |
| DNS VIP | 10 |
| VPN VIP | 5 |
| Each ASR/LLM/TTS activity | 30 |
| Two or more *other* roles (`OVERLOAD_PENALTY`) | +100 |

`ha.py` (Network election) counts voice, DNS, VPN and Home Assistant as "other"
roles; `roles/desk_homeassistant/files/agent.py` counts voice, DNS, VPN and Network.
The two penalties must stay equal. A lower score wins.

The Network owner voluntarily gives up the lease when it holds two or more other
roles and a peer is eligible with a score below `OVERLOAD_PENALTY`. It first takes a
certified checkpoint; if that fails it keeps the lease, logs
`Rebalance deferred`, and tries again after 60 seconds. Only forced drains
(admission closed or `warm-drain`) release without a fresh checkpoint. After a
voluntary release the node holds a 60 second cooldown so it does not reclaim the
role immediately. A healthy owner that holds at most one other role is not moved.

### Rollout

Helper-only changes use `network-helpers.yml` instead of the full `network.yml`
(about 60-90 seconds instead of 5-8 minutes; image pull, PHP opcache, TLS, NATS
workers, dashboard and ingress are skipped):

```sh
ansible-playbook -i inventory.ini network-helpers.yml --syntax-check
ansible-playbook -i inventory.ini network-helpers.yml --check --diff
ansible-playbook -i inventory.ini network-helpers.yml
```

It freezes and re-checks the protected services, validates and copies `ha.py`,
`checkpoint_budget.py`, `bootstrap.py` and `discovery.py`, templates the checkpoint
slice (reloading systemd only if it changed), restarts `desk-network` one node at a
time waiting for `voter_ready`, then requires one native-ready owner through
`verify_ready.py`. Nodes whose files did not change are not restarted. It also
installs the Home Assistant `agent.py` (score weights only) and restarts
`desk-homeassistant` on standby nodes; the ready Home Assistant owner is skipped
because restarting it restarts Core, and it picks up the new score at its next
planned handoff (`homeassistant-online-sync.yml`). The playbook requires all three
deskboxes, so `--limit` cannot be used for a canary.

After rollout check `systemctl show desk-network-checkpoint.slice -p MemoryMax`
(`402653184`) and that `/run/desk-network/` holds no `sqlite-*` file.

### Observed result

Reported after the first rollout: node 2 released 237 MiB of stale tmpfs data
(70% to 10% of the tmpfs, available RAM 1.68 to 2.2 GiB) with no new OOM kills, and
node 1 handed Network to node 2. Final placement was node 1: WireGuard VIP; node 2:
Xiaozhi voice VIP and Network collector; node 3: DNS VIP and Home Assistant Core,
with all three nodes synced. This is a short post-rollout observation, not a
long-term soak; the 384 MiB limit has not been validated against a larger database.

## Hostname collection status

The experimental authenticated modem collector was not deployed. The final
approved probe reached the real modem index, but the device page returned HTTP
404; a complete unattended DHCP read was not established. The login reader,
credential setup helpers, deployment playbook and DHCPLSS wiring have been
removed from the maintained code. No scheduled modem login is enabled. The local
ignored credential file is left untouched and is not deployed by the role.

Reverse DNS probes to the modem returned NXDOMAIN. mDNS is now the bounded replacement described below; NetBIOS stays disabled.
Manual device names remain supported by native NetAlertX.

## Bounded mDNS hostname discovery

The Network owner runs `AVAHISCAN` every five minutes through the native plugin
scheduler, using `files/mdns.py` instead of the pinned resolver's system-DNS
lookup. There is no additional daemon, modem login, NetBIOS scan or dependency
installation: dnspython 2.8.0 is already in the pinned image. Standby nodes do not
run the native container or resolver.

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

The earlier no-auth probe identified zerow.local at .115, with matching forward
address. Coverage is partial: devices that do not advertise mDNS can keep a
manual name. The earlier authenticated DHCP collector remains removed.

For an existing installation, deploy the resolver with
`ansible-playbook -i inventory.ini network-mdns.yml`. This stages all replicas,
refreshes cold standby voters one at a time, refreshes the original owner last,
and waits for one native-ready owner. Only Network is restarted; its UI can
briefly show the recovery page during promotion/bootstrap. Full `network.yml`
installations include the same adapter and policy.

Offline resolver tests use dnspython from `roles/desk_network/tests/requirements.txt`
(the same version already bundled in the production image). They use packet
fixtures and temporary SQLite files; they do not contact LAN devices or the modem.

Live verification (2026-10-15): all three replicas applied the final policy,
with deskb1x active and the other two cold standby. Native inventory learned
`zerow` at .115 and `eWeLink_10025890d7` at .5 with AVAHISCAN provenance. A
subsequent HA-proxied native run returned `synced`; all replicas agreed on the
same certified checkpoint with three receipts. All seven existing UI/health
route checks returned HTTP 200. Protected service/VIP guards passed on all nodes.
A measured lookup body took 5.147 s wall / 0.0953 s CPU (excluding Python/module
startup). The entire native container, not just mDNS, was ~141.7 MiB current /
204.8 MiB peak with zero OOM/oom_kill events, under the unchanged 512 MiB and
0.5 CPU limits. This is bounded rollout verification, not a long-term soak test.

## Owner-routed distributed LAN scans over Core NATS

Enable `desk_network_scan_enabled` and create ignored credentials once with
`python3 scripts/create-network-scan-secret.py`. Deploy existing nodes using
`ansible-playbook -i inventory.ini network-distributed.yml`; new installations
use the same worker tasks from `network.yml`. The NATS role template retains
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
native VNDRPDT cached MAC lookup still enriches vendors after import. On the
canary, bypassing redundant OUI parsing reduced the scan from a 12-second
timeout under CPUQuota=15% to 4.20 seconds (~0.136 seconds child CPU).
No worker accepts arbitrary command arguments.
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

Distributed rollout verification (2026-10-15): all three scan workers are active;
Network owner deskb3x dispatched a real zerow lookup to deskb1x in 5.58 s wall
(~0.041 s worker CPU). With deskb1x's scan service temporarily stopped, the owner
selected deskb2x and resolved the same name in 5.39 s (~0.039 s worker CPU).
The stopped worker was restored. After restarting the dispatcher, an explicit
health handshake and a call from the existing native container both passed;
RuntimeDirectoryPreserve keeps its mounted socket directory valid across restarts.
A socket file's existence alone is not used as readiness evidence.

Final five-second idle samples measured 18.18 / 17.96 / 18.36 MiB worker memory
and 0.039 / 0.000 / 0.000 percent of one CPU core on nodes 1/2/3, respectively,
with no OOM events. These short samples are not a long-term CPU/RAM soak test.
The focused offline suite passed 53 tests; isolated real-NATS routing and ACL
checks passed. All three Network replicas agreed on certified revision
[35, "deskb3x", 2] with three receipts, one active HAProxy backend, and all seven
existing UI/health routes returned HTTP 200. Protected service/VIP guards passed.

The rollout also exposed slow native collector coldboot across promotions before
it became ready. Coldboot remains a separate limitation of the cold-standby
NetAlertX lifecycle; the distributed execution result above measures a ready
system and must not be interpreted as collector failover/recovery time.

ARPSCAN rollout verification (2026-10-15): owner deskb1x routed a real scan
to deskb3x in 4.80 seconds (~0.134 seconds child CPU plus ~0.031 seconds
worker Python CPU). With that off-owner worker temporarily stopped, the owner
selected deskb2x and completed in 4.92 seconds; the stopped worker was restored.
Both observations included the gateway and all three physical node addresses.
The native Scan now action returned 200 and produced a ready distributed result
(22 devices in that observation); ARPSCAN_RUN_SCHD remained */1 * * * *.
The native database retained 37 ARPSCAN objects and 33 known-vendor devices
(these inventory totals include earlier observations, not only this scan).

The updated focused suite passed 59 tests. The pinned native execute_plugin
check verified successful serialization/import and that unavailable/empty output
skips CurrentScan promotion; the real isolated NATS test passed directed ARP,
mDNS, invalid signatures and subject ACL checks. Workers used
18.54 / 18.20 / 18.27 MiB current memory with zero OOM events. A five-second
sample measured 0.256 / 0 / 3.455 percent of one CPU core, including an ARP job
on node 3; these are short samples, not a sustained utilization benchmark.
All three replicas agreed on certified revision [36, "deskb1x", 1] with three
receipts; one Network backend was UP and all seven existing UI/health routes
returned 200. Protected service guards passed.

Use network-scan-workers.yml for subsequent worker-only refreshes: it preserves
the mounted runtime directory and does not restart the native UI. Changes to
the container mounts/policy still require the guarded network-distributed.yml
rollout and can incur the native coldboot delay described above.

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

## Conditional bootstrap CPU budget

An isolated, no-network startup profile of the pinned backend measured 48.68 s
wall / 21.84 s CPU at 0.5 core. The one-core candidate measured 30.49 s wall /
25.46 s CPU. These profiled backend-initialization samples exclude container
prechecks and are not end-to-end HA recovery figures. Imports/API/schema setup
accounted for most of the measured work; normal ARP scan time is a separate cost.

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

Validation (2026-10-15): 74 focused Python tests, native settings/scan JS checks,
dashboard template/route tests and the isolated pinned-native import/NATS ACL
checks passed. A real Scan now request progressed through queue, off-owner
execution and native import in 24.8 seconds; a second immediate request reused
the same acknowledgment. The final rollout reported native readiness after
60.05 seconds on deskb1x. Kernel verification found `50000 100000` in the actual
container `cpu.max`; the enclosing scope has `max 100000`. Checking only the
parent scope would not establish the effective container limit on this platform.
Browser checks confirmed Complete/Fresh after a UI-triggered scan, no JavaScript
errors and no horizontal overflow at desktop and 480-pixel portrait widths.


## Initial warm standby canary (historical)

The first canary used `desk_network_warm_nodes: [deskb2x]`; it preceded the
three-node rollout below. `network-warm-canary.yml` installs only that standby,
retains root-only original controller/config files for rollback, and verifies
that protected services and the existing owner remain unchanged during preload.
The role installs the helpers and renders the opt-in flag for reproducibility.

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

A local root-only `touch /run/desk-network/warm-drain` requests a canary handoff:
the controller checkpoints, withdraws readiness, stops writers, retains the
preloaded parent and excludes itself from election. Removing that file makes it
eligible again without preempting a healthy owner. Loss of lease still uses the
existing majority/expiry rules; planned handoff does not bypass voter TTLs.
Warm-ready replicas have a candidate preference; existing maintenance/admission
checks still apply. A cold start or controller restart must preload again.

Measured on 2026-10-15, with only deskb2x configured warm:

| Observation | Result |
| --- | --- |
| Initial import preload | 43.08 s (paid once per native container lifetime) |
| Warm activation to authenticated native readiness | 19.03 s |
| Stop old Network controller to devices.php HTTP 200 on warm owner | 43.37 s |
| Graceful return to a cold owner to devices.php HTTP 200 | 81.14 s |
| Settled inactive container CPU, 60.27 s sample | 1.39% of one CPU core (about 0.35% of four cores) |
| Settled inactive container memory.current | 65.09 MiB |
| Inactive native API listener / database changes / OOM kills | None / none / zero |

The first post-preload minute reported 31.89% of one core for the container;
it was not a settled-idle measurement. A later process sample was about 1.1%,
followed by the separate 60-second settled sample above. Initial/background
startup work can therefore still consume CPU and must not be presented as flat
idle usage. Memory includes the resident preloader/nginx and container accounting;
it is not the resource usage of a fully initialized active collector.

The round trip retained the same container init PID and preloaded-parent PID,
with one activation and no second import phase. Both healthy voters remained
available for elections. Only the Network controller on the old owner was
stopped temporarily; it was restored in `finally`. This tests service-level
failover and graceful demotion, not removal of node power. The warm and cold
end-to-end numbers are individual handoff measurements, not a latency guarantee
or a controlled benchmark across identical owners. Lease/election, restore,
health polling and HAProxy admission add time beyond native initialization.


## Warm standby on all three nodes

The user authorized the full rollout after the single-node round trip.
`group_vars/deskboxes.yml` now declares:

```yaml
desk_network_warm_nodes: [deskb1x, deskb2x, deskb3x]
```

Reproduce the guarded rollout with:

```sh
ansible-playbook -i inventory.ini network-warm.yml
```

The playbook requires one ready owner and synced voters, saves existing runtime
files and credentials under a root-only rollback directory on each node, warms
standbys serially, then refreshes the original owner. An already current warm
standby is left running. Each standby must finish import preloading and voter
quarantine before continuing. A failed phase restores its prior runtime; shared
VIP configuration and Xiaozhi/DNS/VPN service state are guarded. It does not build
an image, reboot nodes, change memory/CPU ceilings or bypass lease expiry.

On the 2026-10-15 rollout, deskb2x retained its canary import cache, deskb3x was
warmed first, and the original deskb1x owner was refreshed last. deskb2x became the
new ready Network owner while deskb1x completed its own preload. All three
finished in warm mode with one active backend and two inactive import parents;
protected-service guards passed on all nodes.

| Same observable milestone | Earlier cold measurement | Full warm rollout measurement | Reduction |
| --- | --- | --- | --- |
| Native authenticated API/settings readiness, after launch/activation | 60.05 s | 19.95 s | 40.10 s |
| Handoff action to devices.php HTTP 200 from new owner | 81.14 s | 38.89 s | 42.25 s (52.08%) |

The handoff timer starts immediately before restarting the original Network
controller. An independent observer records the first complete HTTP 200 response
from a different, confirmed warm owner. This includes election/lease wait, restore,
native initialization, health observation and routing. It is a service-level
handoff measurement, not a node-power-loss test. These are separate observed
handoffs rather than a statistical latency guarantee or a controlled benchmark
on identical owners. Native and end-to-end savings overlap and must not be added.

Warm imports live in RAM. Restarting the native container/controller or removing
power loses that cache; the first startup must preload again. The improvement
applies to later owner changes while the other nodes remain running.

Final read-only checks confirmed one ready owner, all three voters synced to
checkpoint `41/deskb2x/1` with three certificate receipts, and all seven UI/API
routes HTTP 200. A real Scan now request completed through a distributed worker
and native import, including duplicate-click suppression and fresh inventory.

| Node | Final role | Initial import preload | Container memory.current | 20-second sample, CPU as % of one core |
| --- | --- | --- | --- | --- |
| deskb1x | standby / inactive | 89.49 s | 60.84 MiB | 1.37% |
| deskb2x | active / active | 43.25 s | 178.45 MiB | 36.80% |
| deskb3x | standby / inactive | 42.83 s | 60.74 MiB | 1.43% |

Both inactive database/WAL sets remained unchanged during the sample, their native
API ports were closed, and all container OOM-kill counters were zero. The active
CPU sample includes verification/scan work and is not a steady-idle benchmark.
The initial preload durations vary with startup load and the conditional CPU
budget; they are not the warm activation times shown in the handoff table.
