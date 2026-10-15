# LAN-only Network service

Open `http://192.168.1.186/network/`. All three existing UI proxies preserve the
route. One NetAlertX collector scans `192.168.1.0/24` on `wlan0`; two cold standby
replicas retain certified checkpoints and do not run scans. No new LAN VIP,
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
no longer an immutable override. `Scan now` queues only ARPSCAN. Native
name-lookup scans, private-subnet scans and WireGuard-subnet scans are excluded.
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
stays put. New placement uses fresh CPU plus existing UI/DNS/VPN/voice role
costs. Maintenance withdraws an owner; a telemetry refresh alone does not.

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
latest verified database/config before starting the native service. Because
standbys are cold, failover includes native bootstrap time; it is not seamless
session migration. One remaining node cannot elect a collector or acknowledge
shared edits. Returning nodes catch up without preempting a healthy owner.

The dashboard shows `Network owner` only for fresh public metadata from a
native-ready owner. Each node's details also shows collector/starting/cold standby,
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

Coverage includes lease expiry/restart quarantine, checkpoint certificates,
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

## Hostname collection status

The experimental authenticated modem collector was not deployed. The final
approved probe reached the real modem index, but the device page returned HTTP
404; a complete unattended DHCP read was not established. The login reader,
credential setup helpers, deployment playbook and DHCPLSS wiring have been
removed from the maintained code. No scheduled modem login is enabled. The local
ignored credential file is left untouched and is not deployed by the role.

Reverse DNS probes to the modem returned NXDOMAIN. mDNS/NetBIOS remain possible
partial sources for devices that publish names, but neither has been enabled
as a replacement. Manual device names remain supported by native NetAlertX.
