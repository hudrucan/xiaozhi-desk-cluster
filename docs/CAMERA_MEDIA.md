# Camera media workers

Scope: three Tapo C222 cameras at `.160` (Bed Cam), `.161` (Makeup) and `.163` (Guess). Core and authentication remain
single-active in the existing Home Assistant deployment. This is distributed live
media, not a universal distributed integration runtime.

For the remaining Core dependencies and a staged owner-side recording/session
service proposal, see [Tapo distribution research](TAPO_DISTRIBUTION_RESEARCH.md).

### Core media-handle guard — phase 0

The phase-0 patch subclasses the pinned SDK only in media workers. Workers
use the validated handle supplied by Core and reject explicit or implicit user-ID
allocation/refresh, including direct `executeFunction('getUserID')` calls. A denied
refresh stays latched until a subsequent request binds a Core-provided handle.
Native SDK classes on Core and vendor source files remain unchanged.

Expired playback reports `session_expired` only after the worker attempt drains.
Thumbnail IPC preserves the same typed reason; signed HTTP requests receive a
bounded 409 response after cleanup, while unauthenticated requests retain 404.
Core invalidates only a matching old handle and that entry's index cache under
its existing lock. It does not immediately allocate or replay an operation. A
later user request allocates through the normal Core path; a delayed failure
cannot discard a different newer handle. Handles remain in RAM, outside inventory
and persistent requests. Thumbnail session expiry does not poison negative cache.

Each new uncached playback request obtains a fresh handle from Core after
thumbnail cleanup, under the existing entry lock. Cached playback and joining an
existing job do not allocate. A live r50 canary exposed why this is necessary:
reusing Core's cached browsing handle failed with `session_expired` twice,
including when opening another clip after cancellation. An explicit folder
reopen/retry recovered playback, but should not be required for every clip.
Refreshing at the already serialized playback admission point avoids that stale
handle without permitting worker allocation or adding background polling.

Deployed to all three nodes on **2026-10-18**. Initial offline regressions passed (112 tests),
including cleanup, admission, signed HTTP and stale-handle behavior. Both expiry
branches from the installed pytapo 3.4.26 methods were separately checked offline
and stopped before user-ID allocation. The rollout staged the three changed Core
component files, then used a clean checkpoint/restart on deskb3x to certify r50
with the component included. It next rolled the six changed worker/controller
files one node at a time. All warm workers, voters and NATS connections recovered;
each camera had exactly one ready owner. Mounted worker and active Core source
hashes matched this checkout. Synthetic handle checks inside all three worker
containers rejected forced refresh and direct allocation without camera I/O.

The fresh-Core-handle follow-up passed 48 HA tests, including two new admission
regressions, and was certified through another clean checkpoint as r51. Only the
changed owned Core adapter needed updating; worker processes were preserved.

Core r51 is synced 3/3, ready on deskb3x with no pending disk changes. Protected
service PIDs/configuration and the additional network/dashboard/monitoring service
checks were unchanged. Native vendor source hashes were preserved. Two successive
uncached Guess `.163` clips played on r51 without folder reopen/retry. The first
was cancelled during HLS production; the worker acknowledged cleanup. The second
completed normally in 18.311 seconds of worker time; the browser reached its
28.032-second playlist end at 2560x1440, readyState 4, without a video error.
Thirteen visible previews returned Ready with zero Unavailable rows. All workers
were idle afterward, with no additional media failures during the r51 canaries.
This validates one camera's normal playback and cancellation, not Core failover
or third-party app coexistence; those remain **NOT RUN**. Evidence and limits are
in `CAMERA_MEDIA_SESSION_VALIDATION.json`.

Future deployment must update worker/controller sources and the owned Core
component together through the configuration-preserving lifecycle;
`camera-helpers.yml` alone only ships the controller and is insufficient. Lease
deadlines, placement policy, SDK version and device settings are unchanged.

## Paths and services

- UI: `http://192.168.1.186:8123/desk-cameras/cameras`; upstream WebRTC Camera card.
- Playback: native HA Media Browser → Tapo → camera → day → recording.
- Tapo Control 7.2.8 runs on Core; its source and WebRTC Camera v3.6.1 are unchanged.
  `roles/desk_camera/UPSTREAM.json` pins source revisions and bundle checksum.
- All three nodes run `desk-camera`, `desk-camera-relay`, and `desk-camera-proxy`.
- go2rtc is pinned by ARM64 image digest. Each camera has an independent lease and relay instance. Empty standby relays
  have no camera sources. The owner exposes that camera’s HD/SD sources on demand.
  One controller process shares node telemetry across the three camera placements.
  A score penalty for other active camera slots spreads healthy placements; it
  is a soft preference, so a surviving node can accept multiple cameras.
- Scoped signed NATS `desk.ha.media.*` RPC carries status and grants, never video
  or camera credentials. The adapter imports the existing Lease unchanged.
- Existing 15-second voter grants, 17-second boot quarantine and 10-second
  BOOTTIME permits are retained. An independent wrapper checks permits every
  250 ms and kills the old relay init PID before restarting an empty standby.
  Healthy placement is sticky; fresh node CPU/headroom and occupied camera slots
  select a successor. The original `.160` lease path and NATS subject are retained.
- Stable signaling `:1986` routes by the requested source name to its ready owner.
  RTSP ingress is `:18556` / `:18557` / `:18558` for `.160` / `.161` / `.163`.
  Source ACLs admit Core/cluster addresses; relay API and RTSP additionally use
  private credentials. WebRTC media advertises the selected worker LAN IP at
  ports 18555 / 18655 / 18755 respectively, reachable through LAN/VPN routes. MSE is a fallback through
  the authenticated Core WebSocket. No modem forwarding or public media exposure.
- `/run/desk-camera/public.json` is sanitized; private settings live under
  `/etc/desk-camera` and ignored `secret/camera/` (0600).
  Additional camera runtime/status/permit files live in workload subdirectories;
  `/status` on private port 20226 lists every camera and `/ready/<workload>` checks
  the local camera owner. `.160` retains the legacy `/ready` route.

## Playback and durability

Recordings stay on camera microSD. Tapo Control downloads a requested clip before
playback when it is not cached; this is not an instant Tapo-app timeline. Background
media sync, thumbnail preload, sound analysis and time synchronization start off.
Live RTSP sources use the relay; Direct streams remain an upstream optional path.

Cold playback cache lives in `/var/lib/desk-camera/cache/cold`, mounted at
`/media/tapo_control`. Hot files live in `cache/hot`, mounted at
`/config/www/tapo_control`. Neither is replicated. The owned Core Health component
adds only that hot-cache path to pinned HA native backup exclusions, and the
cluster snapshot/digest applies the same exclusion. No upstream HA or Tapo source
file is edited. Config entries, registries, custom integrations, auth and SQLite
continue through the existing certified journal. On takeover a missing clip is
fetched from the camera again; cache cleanup is supplied by upstream Tapo Control.

Core still cold-starts after owner failure. Media worker takeover is independent;
existing viewing sessions reconnect, not seamless handover. API-ready proves the
relay is up, not that an unreachable camera has recovered. Camera/network failure
is not automatically retried across every node.

## Deployment and cleanup

`camera-media.yml` maintains the prepared three-node media deployment and is never
included in `site.yml`. It requires the existing ignored `secret/camera/` files.
Use the original pinned Core image. Reuse those private files; do not regenerate
keys for a running cluster. First activation uses `camera_config.py` only while
Core is stopped, through the existing clean checkpoint/handoff. It preserves
unrelated entries/devices, certifies the new configuration, and writes an
idempotency marker. Camera additions use a preflighted, idempotent stopped-Core migration of native
  config entries, device/entity registry links and upstream video cards. Both the
  pinned HA singular `config_entry_id` device format and older list format are
  recognized. Existing entries, auth and user dashboard cards remain intact.
  Later camera settings are edited through the native UI.

The previous remote tplink worker, Core `desk_remote` proxy entities, service units,
NATS ACLs and deployment code are retired. r14 is the last pre-migration online
recovery copy; r15 preserves the last old Core configuration before activation.
Do not restore an older bundle while expecting the retired worker to be running.

## Verification

Focused offline checks cover deadline boundaries, empty standby configuration,
scoped ACL replacement, migration idempotency, preservation of unrelated HA
configuration and encrypted snapshot cache exclusions. Live results are recorded
in CAMERA_MEDIA_VALIDATION.json. Software failover is not a physical power-cut test.

### Measured result — 2026-10-16

Core stays ready on deskb1x and camera relay is on deskb3x. A reversible owner
drain recovered a ready relay in 17.05 seconds; a fresh HD media probe passed at
17.11 seconds. The browser decoded WebRTC HD 2560×1440 after transfer. One 65.949 s
microSD clip (7,145,504 bytes) downloaded, then played in native HA with a moving
timeline and decoded 2560×1440 frames. User confirmed Tapo app and shared-account
viewing remain normal. This does not validate audio, VPN media traversal or a
physical power cut.

r16 is synced on all three nodes, pending=false. All three encrypted recovery
copies were restored in isolated directories: identical digest, SQLite and file
checksums passed, native camera entries present, retired proxy absent and media
cache excluded. Active native auth files matched the restored copy byte-for-byte.
All six existing service UI routes returned HTTP 200. A short 6-second sample
measured Core 0.44% of one CPU core, active relay 0.28%, empty relays 0.01%, and
placement controllers 2.61–3.55%; no cgroup OOM kills. These are samples, not a
long-term performance guarantee.

The pre-migration recovery directory is retained root-only. Automatic approval
review rejected deleting this recovery artifact; temporary camera seeds and
retired pilot services/credentials were cleaned separately. No commit/push has
been performed by this deployment.

### Three-camera expansion — 2026-10-16

All three upstream live cards decoded WebRTC HD simultaneously (2560×1440,
readyState 4, playing). Placement was `.160` → deskb3x, `.161` → deskb1x,
`.163` → deskb2x. A drain of only `.161` selected a ready successor after
18.44 s; the other two owners and active Core PID stayed unchanged. Returning
the relay to the free node took 16.16 s for readiness; the first proxy probe
arrived before its next health check and returned 503, so this is not an
end-to-end return time. A subsequent source-routing check passed on all cameras.

Makeup `.161` has no microSD card (confirmed by the user); the SDK reports
`offline` as expected. Live works; playback is not applicable without a card. No SD
formatting, recording schedule or camera settings were changed. Guess `.163`
reports SD `normal`, with recordings dated 2026-10-14 through 2026-10-16. A
396.864-second microSD clip downloaded (98,299,511 bytes) and played in native
HA, decoding 2560×1440 at readyState 4 with the timeline past 28 seconds.

r18 is certified and replicated 3/3. Each encrypted recovery copy was restored
in isolation: all three camera hosts, WebRTC, native device/entity links, file
checksums and SQLite passed; active auth was byte-identical. Media caches stay
excluded. Temporary seed runtimes, duplicate credentials and deployment staging
were removed after this verification; canonical ignored secrets and the existing
pre-migration recovery backup remain.

With three live cards open, a six-second sample measured active relays at
4.92–14.36% of one CPU core and 10.6–13.3 MiB, empty relays around 0.01% and
3.3–3.6 MiB, fleet controllers at 9.14–11.18% and 17.6–17.9 MiB, and proxies at
2.28–2.39% and 3.5–3.7 MiB. Core measured 0.28% and 308.3 MiB. These are short
samples of individual services, not whole-node utilization or a stress test.
No sampled cgroup reported OOM kills.

### Camera control CPU optimization — 2026-10-16

The dedicated camera proxies now make one readiness check for each candidate
and camera. RTSP backend servers track their matching signaling server state;
HTTP and RTSP still use the same ready endpoint and the same one-second
interval/rise/fall settings. Across three nodes and three cameras this removes
nine duplicate checks per proxy: 54 → 27 HTTP checks per second cluster-wide.
`proxy_config.py` converts only backends with identical check policies and
matching hosts; it rejects differing policies instead of weakening them.

`transport.py` shares one NATS connection per node across all camera controllers.
Three bounded wildcard response subscriptions replace per-request subscribe,
flush and unsubscribe. Responses must match the signed sender, recipient,
request ID and workload prefix; pending requests are bounded at 32 and removed
on timeout/cancellation. Status requests return all three camera statuses in
one envelope and share at most one second of cached remote status. Local
placement/eligibility is recomputed for every read. Lease grants are never
cached or batched. Existing workload subjects, keys, ACLs, majority Lease,
15-second grants, 17-second voter quarantine, 10-second permits and independent
250 ms runtime fence are unchanged. Rolling upgrades fall back to per-camera
status only for a peer that explicitly rejects the new status action.

The rollout restarts only `desk-camera`, serially, with protected-service and
Core PID checks. Tapo Control and WebRTC vendor sources, camera settings, HA
configuration and recovery revision are unchanged. Warm relay supervision is
also unchanged. Runtime status and permits remain on tmpfs; no reduction in
physical storage writes is claimed from those files.

Measured before/after (percent of one CPU core; controller + supervisor + proxy):

| Node | Before, 20 s | After, 30 s | Reduction |
| --- | ---: | ---: | ---: |
| deskb1x | 14.38% | 9.34% | 35.0% |
| deskb2x | 15.00% | 10.07% | 32.9% |
| deskb3x | 14.32% | 9.54% | 33.4% |

The aggregate sampled reduction is about 34%. Final controller samples were
5.27–5.50%, against 9.25–9.79% before the two optimizations. Final proxies were
1.16–1.59%, against 2.24–2.31%. Shared-transport request rates were 1.87–1.93/s
per node, with no additional startup flushes or legacy fallbacks during the
30-second sample. All sampled OOM counters were zero and block I/O did not
change. This measures infrastructure overhead, not total board CPU or video
stream processing; short samples vary with other workloads.

Two reversible `.161` drains passed after rollout: ready at 17.13 s / route at
17.66 s outward, and 16.35 s / 16.87 s on return. No overlapping ready owners
were observed; other camera owners and active Core stayed unchanged. Final
placement is one camera per node. Fresh browser sessions decoded all three
2560×1440 streams, readyState 4, after the test. The already-open Makeup session
stalled during the transfer and was verified again after page reload; no
seamless or automatic browser-session recovery is claimed. No physical
power-cut test was performed. Sixteen focused offline tests and the real
inventory playbook syntax check passed. Existing user Core changes remain
pending for the user's manual sync; the optimization does not alter or sync
Core configuration.


### Camera experience and worker enhancements — 2026-10-16

The owned `desk-camera-card` wraps the pinned, unmodified WebRTC Camera card.
A visible, playing stream that makes no progress for 20 seconds is remounted,
with bounded retry backoff and its selected HD/SD stream retained. Hidden cards,
intentional pauses and a disconnected Core are respected. The native Tapo button
entities supply four PTZ controls. Toolbar targets are 36 px with 8 px spacing;
Recordings is a compact text link. User card order and layout are preserved.

Card storage status and the existing **Tapo: Recordings** camera folders read
Tapo Control's existing `camData.sdCardData`, without another camera login or
polling loop. All three cameras use the same logic, including Makeup: `normal`
shows Recordings; `offline` hides the folder and shows Live only; formatting or
an unusable card requests attention in Tapo. Failed, missing or unrecognized
metadata is unknown, not proof of an absent card. Native polling is 30 seconds;
the cards share a cached, authenticated status request every ten seconds. Media
folders reflect the latest result when the user reopens/refetches the list.
Insertion/removal does not remount a live stream or alter the recording plan.
The user must initialize a new card in Tapo when needed; no auto-format exists.
Offline tests cover insertion, removal, unknown metadata and unchanged video
instances. Physical insertion/removal has not been tested by the agent.

Camera eligibility uses cached TCP reachability plus existing admission/memory
telemetry. CPU is smoothed; low available memory adds a placement penalty, and
another active local relay adds a slot penalty. Healthy ownership stays sticky;
there is no continuous load-driven reshuffle. A global camera drain and the
existing node maintenance admission withdraw all local camera workloads. The
existing node-control drain waits for ready successors and no active playback
job before releasing the node. Native HA Cluster shows placement, reachability
and observed owner changes. No new ownership, consensus or NATS service exists.

The single native `tapo_control` media URI keeps its upstream folder browser.
An owned factory adapter, applied before HA's lazy media-platform loading,
replaces only clip resolution with lease-routed workers. Vendor files remain
unchanged; no duplicate Camera recordings source is registered. This adapter
is coupled to pinned Tapo Control 7.2.8 and must be reviewed on upgrade.
An already-cached clip is reused on any worker; otherwise the current relay
owner runs the pinned native pytapo downloader/remuxer in an isolated bounded
job. NATS carries metadata, not video. Core streams an authenticated, signed
same-origin URL in bounded chunks with byte ranges; it does not remux or keep
the full clip in RAM. A pending player waits for preparation; notifications
change at start/completion/failure rather than flashing on each second.
Preparation can be close to clip duration, so long uncached clips are not
instant playback. Jobs are bounded to 30 minutes and 256 MiB, one per node,
with memory admission and cancellation/fencing when ownership changes.

Disposable clips use local eMMC under `/var/lib/desk-camera/playback`; legacy
Tapo hot/cold caches are also bounded by the owned collector. Cleanup checks
every five minutes, expires files after one hour without access and evicts
oldest files above 1 GiB per node. In-flight jobs are protected. Reused legacy
files are hard-linked rather than copied. Native backup/checkpoint excludes
these caches. Cleanup never deletes recordings from a camera's microSD card.

Unsynced disk configuration triggers one native persistent HA notification as
soon as existing fingerprint detection reports it (the disk scan remains
30 seconds, with a five-second reminder check). It is cleared after successful
sync, and user dismissal is respected until another clean-to-dirty transition.
Code deployment also changes the fingerprint; this does not imply the user
changed a setting. Clean checkpoints now load the component installer in a
fresh process. A failed handoff checkpoint retains the local generation and
restarts it while still owning the lease instead of releasing it for an older
cold restore. Offline regression tests cover that failure path.


Final enhancement verification: Core is active on deskb1x with r27 replicated
3/3 and no pending disk changes. Relay placement is Bed → deskb2x, Makeup →
deskb1x, Guess → deskb3x. A reversible Guess transfer reached a ready successor
in 16.76 seconds, with the signaling route returning HTTP 200; other camera
owners and the Core PID stayed unchanged, and the drain marker was removed.
All three browser videos decoded at readyState 4 before this test. The browser
tab closed before an existing-viewer recovery observation, so automatic live
recovery is covered by offline watchdog checks, not claimed as a measured
end-to-end result. Full node maintenance and the final native-source playback
adapter have not received another end-to-end live test in this batch.

Cluster now uses HA's native responsive `ha-menu-button` and the standard
56 px header (matching the measured History header), rather than a permanent
custom hamburger and a 64 px header. This static change requires a page reload,
not a Core restart. Focused tests and sampled measurements are recorded in
CAMERA_ENHANCEMENTS_VALIDATION.json. No sampled cgroup reported an OOM kill.


### Recording stability fixes — 2026-10-17

The distributed resolver initially allowed Core and each transient SDK worker to
allocate separate camera recording user IDs. Live logs showed `-71102
USER_ID_EMPLOYED` on Core index reads. Core is now the single allocator: the
existing native controller supplies its media user/player handle to the selected
worker. The signed, scoped metadata RPC carries it only between existing nodes;
the worker input is a root-only tmpfs file, removed at job completion. It is not
included in persistent requests, cache inventory, browser URLs or logs. This is
an adapter to pinned pytapo 3.4.26, not a new authentication or ownership service.
A cross-node canary read the same 321-item index with the same handle, then a
separate 20-second contained-range download/remux passed (44.15 seconds including
startup, 2,236,944-byte MP4). No camera setting or recording plan was changed.

Native date/index results are kept briefly in RAM and reused during a camera's
active preparation, rather than asking the camera for another index concurrently.
A recent last-known list is available on transient read failure. Per-entry locks
serialize index reads and handle allocation. Reopening an already-running clip
rejoins that job, including after Core reconnect, instead of creating a second
one. An accepted job reports Preparing even before its first progress file
exists; previously that race could return Missing and fail the player's first
request. Worker busy, low memory, disk-space and unavailable-session rejections
now have separate bounded messages. SDK/worker errors still avoid logging secrets.

The native folder browser joins a bounded cache inventory from reachable workers.
A clip shows Downloaded only when a complete artifact exists, Preparing while
its job runs, and Not downloaded when all nodes were checked and it is absent.
Incomplete/offline inventory reports unavailable status rather than claiming
absence. Existing completed native cache files can also be reused. Inventory
contains typed ranges/identities/status, never video bytes or session handles.
Markers refresh when HA refetches a date/parts folder and are removed after
cache eviction. Native Back navigation can reuse an old frontend list; reload
the page to force a refetch. The native list is not a live progress UI.

Uncached original recordings longer than ten minutes expand into contiguous
parts of at most ten minutes, using the same native HA folder UI. This handles
the C222's roughly 34-minute files without submitting a >30-minute job or
buffering the full original on a 4 GB node. A completed full cached clip remains
directly playable. The worker validates that each requested range is contained
in an actual camera index entry. It does not change or split the SD source file.

The existing HA proxy's 60-second response wait could also cut off a slow
uncached part. A validated `set-timeout server 15m` rule now applies only to
`/api/desk_cluster/recording/`; other HA request timeouts, health checks and
routing remain unchanged. Local bridge read deadlines cover the existing bounded
RPC fan-outs. Signed byte ranges and cache cleanup regression tests pass.

At that stability-fix stage this remained **download/remux a whole part before playback**. It was not progressive
YouTube-style playback. Immediate start while downloading would require a worker
pipeline producing fragmented MP4/HLS as packets arrive, and a corresponding
player/proxy path. Reducing part size and extending a scoped timeout do not
implement that feature.

The completed-artifact HTTP path also rejected local Core requests: Core runs on
Podman's bridge, so a same-node request arrives from the container IP rather
than a cluster member IP. The same signed 32-byte request returned 206 from the
host but 404 from Core. Artifact authorization now verifies its signature and
exact clip scope first, then admits only the exact IP of the running, protected
`desk-homeassistant-core` container. It does not admit the entire bridge subnet
or change status/health endpoint access. A five-second in-memory lookup cache
avoids Podman subprocesses for every range request; lookups run only on signed
artifact access, with a bounded five-second deadline accounting for the existing
15% controller CPU cap. Missing/stopped Core or failed inspection fails closed.
All three camera controllers received this fix serially, preserving Core PID.
The actual Core container subsequently fetched 32 bytes with HTTP 206, and the
native HA player decoded the cached 76-second clip at 2560×1440, readyState 4,
with currentTime advancing from 1.26 to 40.54 seconds. Five artifact authorization
and byte-range regressions pass. This demonstrates the tested path, not all
camera/card/network-failure scenarios.

A second uncached 98-second recording completed in 84 seconds (14,385,387 bytes),
but the initially opened native video load remained blank/failed. Retrying the
completed clip decoded normally at 2560×1440. Therefore the longer proxy timeout
alone did not establish reliable first playback. The resolver now waits for its
bounded, shielded background preparation task and verifies worker Ready before
returning a PlayMedia URL. Cached requests still return immediately. Disconnecting
the requesting page does not cancel the SDK job, and reopening rejoins it. Ten
focused resolver/browser regressions pass; progressive playback is still separate.

After a guarded clean checkpoint/restart (r30, all user config preserved), the
corrected native resolver was verified with a fresh 114-second Bed recording on
node 2. The worker reported Ready within 89 seconds, producing a 12,219,335-byte
MP4. Without another click or reopening the clip, HA then opened its native player
and decoded at readyState 4, 2560×1440, currentTime 19.76 seconds, duration 113.92,
paused false and no video error. This verifies first-click download-then-play
across nodes as well as the earlier same-node cached path. It does not establish
progressive playback or fault recovery under every network/camera failure.


### Progressive recording playback — Bed canary, 2026-10-17

Bed (`tapo_c222_160`) now packages microSD packets incrementally as HLS EVENT
with fragmented MP4 segments on its existing lease-selected worker. HA keeps its
native Tapo: Recordings browser and HLS player. The same existing NATS metadata
RPC selects/rejoins the job; media bytes use authenticated HTTP, not NATS.
Makeup and Guess retain the MP4 path. Existing completed MP4 cache is reused.
Set `desk_camera_progressive_bed: false` to return new Bed jobs to MP4; existing
HLS cache remains readable until normal eviction. No camera schedule changes.

The owned `hls_worker.py` adapter uses the pinned pytapo media transport and
request builder, rather than its whole-file BytesIO converter. A single bounded
FFmpeg process copies video and converts G.711 audio to AAC. Actual cached Bed
output is HEVC/AAC 16 kHz mono; synthetic tests use H.264/AAC. This does not promise
HEVC support on every browser or perform expensive video transcoding. Packaging
uses independent completed segments, atomic playlist replacement, bounded pipes,
byte limits and deadlines. A job becomes Playable after two completed segments;
Downloaded requires ENDLIST, all files and duration validation. Unsupported
firmware request fallback exists, but was not exercised on this camera.

Each init/segment URI gets its own HA signature. Internal worker authorization
is scoped to camera, job, generation and filename; temporary files, symlinks,
remote playlist URIs and other generations are rejected. Files are opened before
Content-Length is committed. Whole-playlist validation is reused for at most one
second, invalidated by a changed atomic playlist; each actual file request still
checks its scope, path and size. This avoids O(segments squared) repeated stats
under the existing 15% controller CPU cap. The native growing-stream player is
kept at the recording start with EXT-X-START and a hold-back beyond the bounded
job duration. The hold-back is removed once ENDLIST is present.

The first 134-second cross-node canary published two segments about **41.4 seconds
after the job request**. At 76.3 seconds after the click, HA decoded 2560×1440 at
readyState 4 while the job was still Playable/downloading. It completed as 66
segments / 16,359,478 bytes. Its internal 6.7-second first-playable metric excludes
container startup, camera authentication and index reads; it is not click-to-play
latency. This improves waiting for the whole clip, but does not provide instant
YouTube-like startup or seeking into data not yet received.

After a clean config-preserving activation (r32), a fresh 64-second clip decoded
from 0.52 seconds at playbackRate 1 with a growing 17.97-second playlist, then
advanced to 18.23 seconds at 1× while the playlist grew to 45.82 seconds. The worker
was observed still Playable at 49.97/64 seconds. It completed with 31 segments /
7,210,668 bytes and removed its transient worker/session marker. A second viewer
played the completed same clip; native backward seeking in buffered media also
passed. Two viewers both joining before completion were not established live;
shared-job reuse/cancellation is covered by focused offline resolver tests.

104 samples on RK3528 node 2 measured worker CPU averaging **49.3% of one core**
(about 12.3% of the four-core node), peaking at 52.96%, and peak cgroup memory
**81.45 MiB**. Controller CPU averaged 11.95% of one core. These numbers precede
the validation-cache optimization; no unmeasured CPU reduction is claimed.
Cgroup I/O counters are included in CAMERA_PROGRESSIVE_VALIDATION.json; they are
not total physical flash writes including delayed writeback. The original
sampler did not retain OOM counters, so no OOM guarantee is inferred from it.

Cache lives on the worker filesystem under `/var/lib/desk-camera/playback/` and
is disposable; it is not in the replicated HA config or on camera microSD.
The existing one-hour idle TTL and one-GiB node quota now account for whole HLS
generations. Active jobs and recent HTTP viewers are protected; eviction removes
the whole generation together. Offline tests cover quota/TTL/active-reader
protection, authorization, path validation, early resolution and worker cleanup.
Cache labels refresh on a native folder refetch; an offline/busy RPC may show
Cache status unavailable instead of claiming a clip is absent. iPhone/Chrome
compatibility, listening for A/V sync, and live network/worker/full-disk fault
injection remain outside this canary verification. Protected recovery backups
are retained on all three nodes.

Final health: Core is Ready on deskb1x, r32 is Synced on 3/3 nodes with no pending
disk changes. All cameras are Ready, no temporary drains remain, and the Bed
cache inventory completed (7 items, 1.456 seconds) after the serial controller
update. The transient playback session was removed. These readiness checks do
not replace the unperformed fault/browser/audio checks listed above.


### Native recording feedback and thumbnail audit — 2026-10-17

The pinned HA frontend's `ha-bar-media-player.showResolvingNewMediaPicked()`
resets its spinner after two seconds. Resolving an uncached microSD clip takes
longer, so the native list looked idle even while the existing worker job ran.
The owned extra module now observes the public `media-picked` / `show-dialog`
events for ranged Tapo recordings in the browser player. An inline native
`ha-alert` / `ha-spinner` stays visible while resolution waits. Repeated picks on
that page are suppressed while its first recording resolve is pending; native
HA still owns the actual resolve and player dialog. The banner clears on the
native success/error dialog, navigation or a bounded timeout. No frontend class
methods or WebSocket methods are patched, and there is no new status polling.
Other sources and external media players retain their normal behavior. A reload
loads the asset; all three nodes received it without restarting Core or changing
camera settings. Offline tests cover scope, duplicate picks and cleanup.

All three Tapo entries currently have `media_thumbnail_cache=false` and
`media_thumbnail_preload=false`. The installed pytapo 3.4.26 contains
`getRecordingSnapshots`, a separate JPEG media request (`media_type=2`). Its
contract associates stored JPEGs with detection-event timestamps; it does not
promise an image for a continuous recording or each arbitrary split part. A
nonmatching timestamp can consume the full eight-second timeout. The native
Tapo integration can look up detection events and preload their JPEGs, and can
fall back to extracting thumbnails from downloaded files. These mechanisms are
not an already enabled, distributed per-part preview feature. No thumbnail
preload was enabled, no camera JPEG session was opened and no bulk scan was
started in this audit. In particular, an event image somewhere inside a long
recording must not be presented as a frame exactly at every part's start.
A future preview implementation should first test one genuine C222 event image,
then bound batches/cache and share playback admission/session handling before
adding any video/keyframe fallback for continuous ranges.

Source references: pinned frontend
https://github.com/home-assistant/frontend/blob/20260930.3/src/panels/media-browser/ha-bar-media-player.ts
and the SDK's documented snapshot request
https://github.com/JurajNyiri/pytapo/blob/main/pytapo/media_stream/snapshot.py .
The installed SDK and live entry flags were inspected locally; support for actual
C222 JPEG responses was not established by this read-only audit.


Live feedback verification: a fresh 109-second Guess clip displayed the native
preparation banner immediately, retained it beyond 50 seconds, and a repeated
pick did not produce a Busy dialog. Worker metadata showed exactly one new job.
Without another play click, the native player subsequently opened and decoded
2560x1440 at readyState 4, paused=false, error=null, currentTime=32.21 seconds,
reported duration=110.208 seconds. The preparation banner was removed. A screenshot
of the list/loading state was saved separately; no camera frames were captured.
The temporary verification tab was closed after the check. This verifies visible
feedback and first-click completion, not faster Guess downloads or progressive
playback on Guess (which remains MP4).


### Single JPEG capability check — 2026-10-17

An explicit `thumbnail_check=1` diagnostic date folder uses the existing native
Tapo controller, the adapter's per-camera lock and a complete worker inventory
busy check. Normal browsing does not execute it. It reads one recording index
and one bounded detection-event page (up to 128 entries), chooses one actual
in-range event timestamp and requests at most one JPEG with an eight-second
no-data timeout. The SDK generator is explicitly closed on success or failure.
Only metadata is retained in the report cache; no JPEG is exported or persisted.
Repeated diagnostic folder requests reuse that camera/date's cached result. Busy
or incomplete worker inventory refuses the probe without sending a JPEG request.

The live Bed C222 returned a verified **640x360 JPEG, 34,784 bytes** from an actual
2026-10-16 detection event. Snapshot request/close took **0.126 seconds**; index,
one event-page lookup and JPEG together took **1.127 seconds**. One JPEG request,
zero video downloads and zero bulk-preload changes were used. This establishes
native event-image support on this tested camera, not every continuous recording
or arbitrary split range. It does not benchmark batches, other cameras, or CPU
usage. The check reused Core's media user/player handle; no independent SDK client
allocated a competing camera media user ID.

The diagnostic adapter was activated with the existing config-preserving r34
checkpoint. Configuration and thumbnail-preload flags were retained. Focused
unit checks cover no-event/unsupported responses, one-image session cleanup,
sanitized errors, repeat-result reuse and refusal during active playback.
Actual numbers are in CAMERA_THUMBNAIL_VALIDATION.json. A future list-thumbnail
feature should fetch/cache a bounded set of event JPEGs lazily and leave clips
without matching events unillustrated unless a separate keyframe fallback is
explicitly implemented. Enabling the vendor's entire preload loop is not this
feature and remains disabled.

After closing the JPEG session, the normal Bed date index loaded successfully
and cached HLS playback decoded at 2560x1440, readyState 4, paused=false,
error=null, currentTime 2.626 seconds of 64 seconds. This verifies sequential
index/playback after the probe; simultaneous snapshot and download were refused
by design and were not tested live.


### Lazy thumbnail feature — deployed and measured

The new owned thumbnail path attaches a representative image URL to each native
clip and parts-folder entry. The native virtualized Media Browser fetches local
thumbnails with its existing authenticated client. Normal list construction does
not start a full-day scan. Each request first checks a RAM-only LRU: maximum
16 MiB / 512 entries, one-hour creation TTL, cleanup every minute. Placeholder
results expire after 20 seconds. No image cache is written into HA config,
replicated snapshots or eMMC; restart/failover intentionally drops it.

On a miss, the existing native Tapo controller queries a bounded event page for
the requested interval and attempts one matching event JPEG. If unavailable,
a complete cached clip can supply a frame on its holding worker. Otherwise a
short range request (up to four seconds, collected transport at most one MiB)
uses the same native camera media user/player handle; its early transport bytes
are decoded on the existing lease-selected worker. The frame decoder uses the
already-pinned image, network=none, read-only filesystem, no camera credentials,
0.25 CPU, 256 MiB memory inside a transient systemd job (25% CPU / 320 MiB / 24-second runtime), 12-second FFmpeg / 20-second launcher deadlines, fixed
scale up to 320x180, one output frame and a per-node nonblocking admission lock.
The HTTP signature binds member, camera, job, generation, input source and SHA256
of the body. Video bytes travel by authenticated HTTP, never through NATS.

Image generation is serialized per camera, with at most two active requests and
32 total pending requests. A normal playback resolve preempts thumbnail tasks
and closes their media sessions before allocating/using playback's handle.
Busy/low-memory/offline cases return a generic placeholder with no-store rather
than provoking another camera login or claiming a real representative image.
A thumbnail may be any event/frame inside its range, not necessarily frame zero.
The provider stores sanitized counters and worker cgroup metrics for measurement.
`thumbnail_stats=1` on an explicit diagnostic date folder reads those counters.

Offline checks passed: RAM TTL/LRU limits and provider behavior (8), native
adapter browsing/resolver regressions (13), worker authorization/resource/
synthetic frame/cleanup checks (5), existing artifact regressions (6), and ticket
scope/member/digest checks (3). The installed Core HTTP view contract was also
read without changing production. These are not RK3528 live performance results.

The user explicitly approved rollout and live measurements on 2026-10-17.
The feature is now on all three nodes, activated with the existing certified
r35 checkpoint/handoff. One Core is ready on deskb1x, recovery copies are synced
3/3, cameras are ready and there are no temporary maintenance drains. User auth,
config entries and dashboard title were checked for preservation before activation.

Live measurements (details in CAMERA_THUMBNAIL_FEATURE_VALIDATION.json):
- Cold list: 5 JPEGs already present by the 6.723-second observation; 44 rendered
  JPEGs and no placeholders by 23.703 seconds. These are observation upper bounds,
  including frontend navigation, not exact first-frame timestamps.
- Two-part folder: both event/keyframe images present; warm reload displayed the
  list and both JPEGs in 0.603 seconds. The browser may satisfy these from its
  private five-minute cache. Core RAM hits were independently observed (4).
- Observed cache: 169 entries / 6,049,002 bytes, under both limits. Counters include
  concurrent browsing; they do not establish a zero-request global delta for the
  warm reopen. No thumbnail images were persisted in a config or eMMC cache.
- Whole Core, 100 samples over 125.0 seconds during mixed browsing: mean 11.96% of one
  CPU, p95 36.96%, max 64.47%, RAM from 274.70 to a 290.05 MiB peak, no OOM.
  This includes normal Core work; it is not isolated incremental thumbnail CPU.
- Worker fallback succeeded through the signed cached-HLS endpoint (HTTP 200,
  9,477-byte JPEG, 15.387-second round trip). A short camera preview also produced
  a 9,324-byte JPEG from 325,240 transport bytes; total provider time 18.478 s.
  The latter worker sample consumed 1.624 CPU seconds and peaked at 48.66 MiB,
  with no OOM. This fallback is slower than event JPEG retrieval.
- Cached 64-second playback still reached its end at 2560x1440, readyState 4,
  error=null after thumbnails were introduced; no new clip download was required.

The initial live keyframe attempt exposed a cgroup inheritance error: launching
Podman directly from the controller inherited its 15% CPU / 96 MiB parent limit
and timed out. The decoder now uses the existing isolated transient-job pattern,
keeping controller limits unchanged. The focused five decoder tests passed again,
and the corrected decoder hash matches on all three nodes. Worker frame resource
numbers were measured on node 1, rather than claimed for unmeasured nodes.
One-hour TTL and byte/entry eviction passed time-controlled offline tests; no
one-hour wait or artificial production cache fill was performed. Placeholder
results during quarantine/busy/error conditions stay disposable and expire after
20 seconds. Event JPEG and one-frame fallback generation remain demand-driven.


### Distributed thumbnails and current-folder cancellation — 2026-10-17

This supersedes the earlier hybrid Core-fetch thumbnail path above. Core now
only allocates the existing native media handle, checks camera/storage admission,
routes signed requests and caches final JPEGs in RAM. Event search, JPEG retrieval,
bounded preview reads and one-frame decoding run on the camera's existing owner.
Recording-index browsing and the rest of native Tapo integration stay on Core.
The explicit old capability probe is separate and is not used by normal thumbnails.

Each node runs `desk-camera-thumbnails.service`, a warm Python runtime in the
already-pinned image. It reuses protected `/etc/desk-camera/playback.json`, accepts
only root-local Unix IPC and binds each job to the existing camera generation.
The controller validates signed HTTP scopes and body hashes; no media goes into
NATS, and no new broker user, subject, consensus or placement framework was added.
The worker imports SDK/Pillow before publishing socket readiness (about 47 seconds
for a cold preload at the 0.3-CPU cap), then stays warm. It never allocates another
camera media user ID: Core supplies the native user/player handle transiently.

The native Media Browser retains its UI. A small extra module consumes public
`media-browsed` events and attaches tab-scoped query parameters to the image URLs.
The matching authenticated focus endpoint tracks HA user, tab nonce, monotonic
sequence, camera/date and optional parent clip bounds. Navigation/pagehide revokes
that tab's prior scope; late POSTs/requests cannot reactivate an older sequence.
A job shared with another tab remains until its last valid consumer leaves.
Heartbeat is 15 seconds; abandoned tab scopes expire after 90 seconds. A bounded
DOM attribute retains only ten diagnostic event phases/ranges/HTTP statuses,
without credentials, media handles, nonce values or image contents.

Cancellation sends a signed worker cancel, closes SDK media generators and FFmpeg
children, and waits for the original HTTP handler to release admission before
running the next job. One currently executing SDK control HTTP call finishes first.
An independent 250ms owner-generation watch and 32-second worker task limit remain.
Core shares its camera lock with playback, permits two global active image jobs,
64 pending jobs and one fetch per node. The 40-second source deadline includes
queue time: a large continuous-recording folder may still have placeholders while
expensive keyframes are generated. Playback continues to preempt thumbnails.

RAM cache remains 16 MiB / 512 images / one hour from creation, with 20-second
negative cache and five-second housekeeping. Scope rejection/cancellation does not
negative-cache the image. Scope changes keep completed image keys reusable. Browser
responses remain private, max-age 300 seconds. No thumbnail cache is persisted to
eMMC or config replication; Core restart/failover drops it.

Live rollout used a config-preserving r37 checkpoint/handoff. Core is on node1;
final placement was Bed on node3, Guess on node2 and Makeup on node1. The native
Guess date-to-parts interaction changed scope from sequence 2 to 3, both focus
POSTs returned HTTP 200 and both new images were JPEGs by the 20.841-second
observation. Node2 recorded 31 completed jobs, four real cancellations, no runtime
failures and idle state after that folder finished. A single keyframe worker
sample took 6.396 seconds. The transport's failed count includes cancelled HTTP
requests; use runtime cancelled/failed counters to distinguish these.

Actual Podman cgroups were sampled 80 times over about 79 seconds during mixed
browsing: Core mean 2.50% of one CPU, p95 12.82%, peak sampled RAM 293.29 MiB;
active node2 worker mean 13.67%, p95 30.25%, peak sampled RAM 89.88 MiB. Warm idle
workers were about 46.8 MiB with effectively zero CPU in that window. No cgroup OOM
occurred. Container limits are 0.3 CPU / 320 MiB. Host launcher limits are separate
(0.35 CPU / 384 MiB); its earlier ~10 MiB reading was not total worker memory.
These are mixed-workload samples, not a controlled old/new CPU reduction benchmark.

The 51 focused checks cover scope/cache/routing/cancel transport, runtime media
handle reuse and fencing, admission drain, native adapter behavior, signed tickets,
legacy frame decoding, native event ordering and playback feedback. Full details
and verification limits are in CAMERA_DISTRIBUTED_THUMBNAILS_VALIDATION.json.
Reload an already-open HA page once to load the new extra module. Pending user
config changes remain governed by the existing manual Sync config workflow.

Final native counters after closing the test tab: zero image jobs, 28 cancellations,
60 event JPEGs, 27 preview frames and 161 RAM hits; cache 87 entries / 2,539,186 bytes.
These global totals include rollout/debug iterations. The last Bed event image
came from node3 in 2.212 seconds. Cached HLS playback still decoded at 2560x1440,
readyState 4, error=null, playing at 47.612 seconds of 64 after preemption.
All three managed source hashes matched local code. r37 is synced 3/3; new local
config changes reported after that checkpoint remain pending under manual sync.

After SDK preload, final node3 runtime recorded 30 completed event thumbnails,
zero runtime failures and idle state, with the last SDK fetch taking 0.226 seconds
(0.232-second worker HTTP execution). Node2 finished 31 jobs and four cancellations.
All workers were idle at the final check. The existing Overview tab was reloaded
and its Connection lost banner cleared; temporary measurement tabs were closed.


### Native thumbnail loading feedback (2026-10-17)

The current native Media Browser thumbnail now shows only a centered 20px HA
`ha-spinner` while its image request is unresolved, including admission/worker
queue time. No extra thumbnail fetch, backend polling, Core restart or camera
controller restart is introduced. Only rendered rows in the current folder are
checked; checks pause in hidden tabs. JPEG results remove the overlay; fallback
SVG or a 45-second UI deadline stops the spinner. A late JPEG can still recover.
The visible label was removed after visual review; the loading label remains
accessible to screen readers. `--ha-spinner-size` sizes the native SVG correctly.

Six focused JS checks pass. Live native date-to-parts navigation produced two
JPEG thumbnails. For a fresh two-part folder, DOM verification found two native
spinners, each 20x20px, centered within 1px, with no visible loading text. This
loading state does not distinguish queued work from active SDK execution.
The JavaScript asset was updated on all three nodes without restarting Core;
already-open browser tabs must reload once to use it.


### Cached-row feedback and Play sizing follow-up (2026-10-17)

Fix the loader masking cached JPEGs after Back: a native rendered JPEG for the
same clip is authoritative even when the finite Resource Timing buffer no longer
contains its request. Completion is latched. A recycled row that still contains
a different clip's old image waits for a changed result or request completion.
Seven focused JS checks pass, including back/cache without Resource Timing,
recycled-row protection, placeholder timeout and late-success recovery.

Scope the native Play CSS to camera thumbnails only. List: 32px button / 18px
icon. Grid: 36px / 20px with a translucent dark background. Fixed square hosts
and translate(-50%,-50%) remove the native stretched-host/inner-button offset.
Keep hover and native action handlers; multi-part folders do not get Play.
Live DOM measurements found both button/inner-button sizes correct and zero
center offset in list and grid. A native date → parts → Back check, without a
reload between navigation, showed 42 rendered JPEGs and zero JPEGs covered by
loading overlays. The JS-only update reached all three nodes without a Core
restart. This does not prove every browser-specific cache case is eliminated.


### Browser playback sessions and paused previews — 2026-10-17

The native browser player now opens immediately with a local inline SVG while
its authenticated HA WebSocket request prepares the recording. Closing the
native dialog or leaving the route releases that tab's viewer. Only the last
viewer cancels its managed worker attempt; a bounded worker lease handles a lost
Core/browser. Cancellation waits for process/session cleanup before admitting a
new clip. An old cancellation cannot kill a newer attempt. Completed cache and
external media players keep their existing behavior.

The earlier progressive canary was restricted to Bed; this made Guess wait for
a complete MP4. `desk_camera_progressive: true` now enables the same HLS path for
all configured cameras with microSD storage, replacing the Bed-only flag. Makeup
still requires an actual ready card before recording playback is offered.
Existing completed MP4 files remain reusable. Packaging copies video and encodes
only the small audio stream; it runs on the selected camera worker.

Thumbnail arbitration returns an explicit transient paused SVG instead of a
failed preview. It does not poison the RAM cache. Cached JPEGs still win; paused
rows retry every five seconds and resume after the camera is released. Native
folder scope registration/cancellation also uses the existing authenticated HA
WebSocket, avoiding the browser HTTP pool occupied by thumbnail requests.
Thumbnail fetching and decoding continue on workers. No new service or NATS
subject is introduced by these fixes.

Focused checks cover last-view cancellation, late-result suppression, shared
viewers, stale attempt protection, worker cleanup acknowledgement, thumbnail
pause/cache behavior and the native frontend hooks. Live measurements and their
limits are recorded separately; packager time excludes container imports,
authentication and camera index reads and is not click-to-play latency.


Live viewer validation (r43): Guess `.163` used HLS on node 2 for an uncached
122-second clip. The native loading popup appeared in 223 ms. Two completed
segments were observed at worker age 45.517 seconds (9.885 seconds inside the
packager). At 66.705 seconds after the click the browser was playing at 20.006
seconds, 2560×1440, readyState 4, with a growing 38.088-second duration and no
video error. A later worker sample was still Playable at 66.154/122 seconds,
establishing real progressive playback rather than a completed-file replay.
Cold worker imports/authentication/index remain about 35 seconds of startup;
this is not instant playback.

Closing the player removed its container and ephemeral session; the next clip
was admitted on its current lease-selected owner without the busy-camera error.
During playback the page held one cached JPEG and 29 paused previews with zero
Unavailable rows. A two-part folder subsequently recovered both JPEGs without a
folder reload. Exact resume latency was not established because opening a
diagnostic tab hid the test tab for part of this check. The resume cycle retries
through a transient health/negative-cache result for at most two minutes; a
persistent failure still ends as unavailable. Live evidence and limits are in
`CAMERA_VIEWER_SESSIONS_VALIDATION.json`.


### Warm playback runtime — 2026-10-17

Playback now reuses the SDK process of `desk-camera-thumbnails.service` on each
node. The service name is preserved; it is a warm media worker for both thumbnail
and recording jobs. Imports finish before its root-only Unix socket becomes
ready. There is no per-click container/Python boot and no clip prefetch. Camera
login is lazy, only on an admitted owner job; authenticated clients can be reused
for ten minutes within the same ownership generation. Standby workers perform
no camera authentication or stream I/O merely by being warm.

The controller submits validated clip metadata, the existing native Core media
handle and an attempt ID through local IPC. Handles stay in RAM. The existing
one-playback-per-node admission is preserved. Playback cancels and awaits the
node's thumbnail cleanup before using its client. The warm process independently
checks the existing BOOTTIME permit/generation and an eight-second controller
heartbeat. Closing the last viewer, ownership loss and worker shutdown close the
media stream and muxer; late cancellation targets only its exact attempt.
Buffered HLS packets yield to heartbeat/cancel processing. Video remains copied,
with bounded AAC audio encoding. The worker is capped at 0.5 CPU and 512 MiB in
Podman (outer service 60% / 640 MiB), rather than adding another resident service.

Native media part folders no longer receive a thumbnail URL. Only actual clips
and parts inside the opened folder fetch an image. Closing the native player
explicitly releases thumbnail admission after worker cleanup, clears only
negative thumbnail cache for that camera and refreshes uncached rows. The UI
switches to a centered spinner while resuming. A small HA image-off icon replaces
visible Preview unavailable text, retaining the explanation in its tooltip and
accessible label. Cached JPEGs remain visible.


The first warm canaries played early but were revoked by the existing owner
fence and are not treated as stable successes. Under the original 15%-of-one-core
controller quota, authenticated HLS HTTP and control-plane work had substantial
CPU throttling. The existing controller now has a 40% CPU ceiling, with unchanged
weight, niceness, memory bound, lease TTL, quorum and fencing. This is headroom
for bursts, not a CPU reservation or an idle workload. The successful run averaged
12.13% of one core for the controller and 49.80% for the media worker. Sixteen
health samples retained at least 7.804 seconds of lease and no generation change.

The stable 122-second Guess canary published playable HLS at sampled worker age
8.229 seconds, versus the earlier cold 45.517 seconds: 37.288 seconds / 81.92%
saved. The complete warm job finished after 77.814 seconds. At 79.515 seconds
after click, the browser decoded at 70.071 seconds with the full 122.279-second
playlist, readyState 4 and no error. The baseline and warm run used different
RK3528 nodes, so these are observed runs, not a controlled hardware benchmark.
The earlier failed generations were not reused as a first-seconds cache.

A subsequent clip reused the same warm process; closing it cancelled only its
media job. Cached thumbnails stayed Ready. A fresh two-part folder recovered both
JPEGs after a close, without reload; the final sample at 25.611 seconds is only an
upper bound on resume latency. Nineteen native part folders had zero thumbnail
elements, and visible Preview unavailable text was absent. The retained tooltip
and accessible label still explain a failed preview.

All three nodes are warm, with the tested limits persisted, canary drains removed
and Core r45 synced 3/3. Ten-second idle samples measured 0.0270–0.0298% of one
core and 47.23–108.66 MiB resident memory. The completed playback run peaked at
117.55 MiB; node 3 later peaked at 149.86 MiB across additional thumbnails/jobs.
These are measured process/cgroup values, not reserved per-node memory. Details
and limits are recorded in `CAMERA_WARM_PLAYBACK_VALIDATION.json`.

### Owner-only recording service — phase 1, 2026-10-18

Bed `.160` and Guess `.163` now read their microSD dates/index and allocate media
sessions in the existing warm camera-owner worker. Core renders the native media
browser from typed metadata and routes playback/thumbnail requests with no camera
handle. Makeup `.161` remains live-only. Ordinary native control polling remains
on Core; this is not the final distributed control adapter.

The new read-ticket API uses the existing signed NATS subjects and root-local IPC.
SDK work stays outside lease callbacks; metadata shares admission with thumbnails
and playback, with real executor-drain acknowledgement on cancel. Pages contain
at most 128 entries, snapshots last 30 seconds, timestamps stay in camera epoch
seconds, and SD/owner/session errors cannot masquerade as empty recordings. A
selected camera has no native allocator fallback. Core's SDK guard is installed
at Python startup before native integration setup and required by readiness.

The rollout installs compatible inactive workers, certifies the Core guard across
all recovery nodes, then atomically enables worker authority. The dedicated
`camera-recording-owner.yml` preserves this order and refuses forward authority
removal. See [the contract and rollback order](CAMERA_RECORDING_CONTRACT.md).

Live canaries read Guess's 301 clips and Bed's 380 clips for the closed day
2026-10-17, each over three pages. Same-node IPC and remote NATS yielded identical
Guess clip hashes. Guess playback at 2560×1440, cancellation, a following completed
10-second clip and Bed's completed 24-second clip passed. A controlled Guess owner
transfer fenced the old result at the 1.375-second sample, published a new valid
generation at 18.210 seconds, rejected old-generation work and isolated a late
cancel. Core and other camera owners stayed unchanged. A relay can become ready
before its new generation is public; Core treats that interval as unavailable.

A separate planned idle Core handoff from deskb3x to deskb2x reached guarded
readiness after 143.181 seconds, with certified recovery 3/3 and unchanged camera
owners. The metadata list still had the same raw bounds hash afterward. This is
recovery validation, not uninterrupted video or a power-cut/network-partition
proof. Core-independent HTTP delivery and viewer reattachment remain phase 2.

Twelve-second samples during the idle-media handoff measured the warm container
at 48.38–62.11 MiB current cgroup memory and 0.000% of one CPU core at the sample's
resolution. Controller samples were 6.412–8.136% of one core. These short samples
include Core startup activity on the cluster and are not a before/after CPU
benchmark. Source hashes, individual timings and remaining tests are recorded in
`CAMERA_RECORDING_OWNER_VALIDATION.json`. No firmware, native vendor source,
lease/quorum timing or unrelated service restart was part of this phase.

### Independent browser delivery — phase 2, 2026-10-18

Bed and Guess browser sessions now receive an opaque, scoped viewer capability
after authenticated HA admission. Media and five-second viewer heartbeat/close
requests use the existing 8123 ingress to the exact serving camera controller,
independently of Core readiness. Controller-owned refs expire after twenty seconds
without heartbeat; explicit last close waits for real SDK cleanup. Core shutdown
and a newly discovered Core waiter cannot cancel another detached viewer. Native
control polling/commands remain on Core.

A live Guess 1440p viewer continued through Core handoff deskb3x → deskb2x
(158.607 seconds to ready), with unchanged SDK attempt/camera owners. It kept
playing and loading new data while no Core was ready. Closing it drained the
worker; a following 26-second clip completed and the browser played to its HLS
end. Final Core is deskb2x, r60 synced 3/3, no pending disk changes, all workers
idle. No additional service/listener or whole-node CPU saving is claimed.

Full-page reload does not restore the in-memory capability, and camera owner loss
still fences a running download. See [delivery lifecycle, rollout and limits](CAMERA_MEDIA_DELIVERY.md)
and `CAMERA_MEDIA_DELIVERY_VALIDATION.json`, including the preliminary cancelled
preparation and tests not run live. The next boundary is owner-side control
observations/polling and then commands through a thin HA adapter.

### Owner-side status observations — phase 3A, 2026-10-18

The periodic native `getMost()` batch now comes from the current camera owner
for Bed, Makeup and Guess. The existing warm process has a separate bounded
read-only lane; it keeps polling every thirty seconds while Core is absent and
can refresh during managed playback. Native mapping and all 327 entity bindings
(228 enabled) retain their identity. The early Core adapter refuses stale or
mismatched owner data without falling back to direct SDK polling.

SDK construction/authentication, startup and ancillary reads, command methods,
entity publication and automations still require Core. Bed/Guess recording
authority and independent browser delivery are unchanged; Makeup is live-only.
See [the observation boundary, rollout and validation](CAMERA_OBSERVATIONS.md).
