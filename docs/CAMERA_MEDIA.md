# Camera media workers

This page describes the supported implementation in this checkout. Configuration
and guarded activation policies determine enablement; this document does not establish the current state of live nodes.

## Supported capabilities and authority

Three Tapo C222 cameras are configured: Bed `.160`, Makeup `.161` and Guess `.163`.
The native Home Assistant integration, entities, unique IDs, dashboards and Media
Browser remain in the single-active Core. Camera relays and warm SDK workers run
on the existing three-node cluster with independent per-camera ownership.

The shared selections in `group_vars/deskboxes.yml` are:

| Responsibility | Selected workloads | Implementation and contract |
| --- | --- | --- |
| Live HD/SD relay | All three cameras | Per-camera lease-selected go2rtc relay |
| Recording dates/index, media-session allocation, thumbnails and playback | Bed and Guess | [Owner recording contract](CAMERA_RECORDING_CONTRACT.md); Makeup remains live-only |
| Admitted browser media and viewer lifetime | Bed and Guess | [Independent delivery](CAMERA_MEDIA_DELIVERY.md) through the existing 8123 ingress |
| Periodic native `getMost()` status reads | All three cameras | [Owner observations](CAMERA_OBSERVATIONS.md) |
| Native PTZ arrows, preset selection and privacy commands | Guess only | [Command contract](CAMERA_COMMANDS.md); Bed/Makeup and unselected methods retain the native Core path |

Role defaults leave recording/observation/command authority inactive. The ordinary
camera role seeds policies without overwriting an existing selection. Dedicated
cutovers validate and activate authority; changing a variable alone does not
prove that a running cluster has completed that cutover. `desk_camera_progressive`
defaults to `true` for configured cameras with usable storage.

Core still owns authentication/UI admission, native SDK startup and ancillary
reads, entity publication, recorder and automations. Smart-plug distribution is not implemented.

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

## Ownership, transport and media isolation

A healthy camera owner stays sticky. Fresh reachability/admission/memory telemetry,
smoothed CPU and role/relay costs influence successor placement. Global camera
drain and node maintenance withdraw workloads; node drain waits for ready
successors and playback cleanup. A surviving node may own more than one camera.

`transport.py` shares a NATS connection and three bounded response subscriptions
per node. Signed sender/recipient/request/workload checks remain in place, with
at most 32 pending requests and timeout/cancellation cleanup. Remote status can
be batched and cached for one second; eligibility is recomputed and lease grants
are never cached. The explicit unsupported-status fallback remains for rolling
compatibility. HTTP and RTSP proxy checks share matching signaling health only
when hosts and check policies match; differing policies are rejected.

Selected recording cameras allocate media handles only in the warm owner worker.
Core consumes typed metadata and cannot fall back to its native allocator on RPC
failure. The startup guard is installed before native integration setup and is
required by readiness. Unselected paths retain the Core-provided handle guard:
media workers reject direct or implicit user-ID allocation/refresh, latch a denied
refresh until a later request binds a supplied handle, and drain before reporting
`session_expired`. SDK objects and camera handles never enter browser URLs, public
status, persistent request metadata or logs.

Recording metadata, thumbnails and playback share SDK admission. Cancellation
waits for actual executor/session/muxer cleanup; a late cancellation targets only
its original attempt. Generation/BOOTTIME checks and independent runtime fencing
remain mandatory. Observation reads and command dispatch have separate bounded
lanes so media work does not block status or lease callbacks. Their distinct
allowlists, durable command receipts and uncertainty rules are specified in the
linked contracts; camera ACK alone does not establish physical motion.

## Recording playback and browser lifetime

The existing native `tapo_control` source renders typed owner dates/index results
for selected cameras. Pages are bounded to 128 items and retain raw camera epoch
seconds, separate display offset/SDK correction and explicit freshness. Failed,
stale or incomplete results cannot masquerade as an empty SD card.

Uncached long recordings are presented as contiguous parts of at most ten minutes;
a completed full cached MP4 remains playable. Workers validate requested ranges
against real camera index entries and never split or alter the SD source file.
Jobs are bounded to thirty minutes and 256 MiB, with one playback per node,
memory/disk admission and cancellation on lost authority.

The warm `desk-camera-thumbnails.service` handles recording and thumbnail work.
SDK/Pillow imports finish before root-only Unix socket readiness. Camera login is
lazy and clients may be reused for ten minutes within an ownership generation;
warm standby alone does not authenticate to cameras. No per-click container boot
or clip prefetch is required. The worker checks permits plus an eight-second
controller heartbeat. Podman limits are 0.5 CPU / 512 MiB; outer service limits
are 60% CPU / 640 MiB. The controller retains 40% CPU / 96 MiB limits.

Progressive playback packages HLS EVENT/fMP4 incrementally: video is copied and
G.711 audio encoded to AAC by bounded FFmpeg. Two complete segments permit
Playable; Downloaded requires ENDLIST, all files and duration validation. Existing
completed MP4 cache remains reusable when progressive generation is disabled.
HEVC browser support is not guaranteed, and seeking into data not yet received
is unavailable. Startup includes camera authentication/index work and is not instant.

The native player opens with a local loading SVG while authenticated HA admission
prepares the clip. For selected owner recordings, Core issues a scoped opaque
viewer capability; subsequent media, five-second heartbeat and close requests
route directly through 8123 to the exact serving controller. An admitted viewer
can continue through Core outage, but a new open requires ready authenticated
Core. Viewers expire after twenty seconds without renewal and have a two-hour
absolute lifetime. A controller restart revokes its capabilities; a full page
reload does not restore them. Closing one tab releases only that viewer; the last
viewer waits for its worker attempt to drain. An active download remains fenced
on camera owner loss. Completed-cache readability does not imply cross-owner
download migration. See the delivery contract for scope, capacity and rollback.

Internal artifact access verifies exact camera/job/generation/filename scopes,
strict playlist paths, sizes and symlink rejection. Legacy Core artifact requests
also require the exact running protected Core container address after signature
validation; the whole Podman bridge is not admitted. Byte ranges are bounded.
The extended proxy timeout applies only to `/api/desk_cluster/recording/`.

## Thumbnails and native UI

Thumbnail generation is lazy for actual clips/parts in the opened folder;
part folders themselves do not request images. Event search/JPEG retrieval,
bounded short preview reads and frame decode run on the selected camera owner.
The older explicit `thumbnail_check=1` diagnostic is separate from normal browsing.
A representative image may come from anywhere in the clip range, not frame zero.
Normal browsing does not trigger whole-day preloading or another Core media login.

The authenticated focus API binds HA user/tab nonce/monotonic sequence and folder
scope. Navigation/pagehide revokes prior scope; late requests cannot reactivate
it. Other valid tabs can retain shared work. Focus heartbeats run every fifteen
seconds and abandoned scopes expire after ninety seconds. Cancellation drains
before the next SDK operation. Core permits two global image jobs, 64 pending jobs
and one fetch per node, with a forty-second source deadline including queueing.

Playback preempts thumbnails and awaits cleanup. Cached JPEGs remain visible;
busy previews return a transient paused SVG without poisoning cache, retry every
five seconds and resume after cleanup. Native row spinners cover queue time,
pause in hidden tabs and stop on a result or forty-five-second UI deadline; a late
JPEG can recover. Recycled rows cannot display another clip's cached completion.
Failures retain an accessible explanation without exposing SDK details.

Final JPEGs use a Core RAM LRU capped at 16 MiB / 512 entries / one hour from
creation, with twenty-second negative entries. Scope rejection/cancellation is
not negative-cached. Responses are private with five-minute browser caching;
Core restart/failover drops the RAM cache. No thumbnail images enter replicated
config or persistent eMMC cache. `thumbnail_stats=1` reads sanitized counters;
transport cancellations and runtime failures are distinct measurements.

The owned camera card wraps the unmodified WebRTC card, retains stream selection,
and remounts a visible stalled stream after twenty seconds with bounded backoff.
Hidden/paused cards and disconnected Core are respected. Native storage metadata
distinguishes ready, absent, attention and unknown; read errors do not imply no
card. Storage changes do not remount live streams or change recording plans.
The native HA header/menu and existing card order/layout remain intact.

## Durability, cleanup and recovery

Recordings remain on camera microSD. Background media sync, vendor thumbnail
preload/cache, sound analysis and time synchronization remain disabled. Local
playback artifacts under `/var/lib/desk-camera/playback`, cold cache under
`cache/cold` and hot cache under `cache/hot` are disposable and unreplicated.
The five-minute collector enforces a one-hour idle TTL and one-GiB node quota,
protecting active jobs/viewers and evicting whole HLS generations together.
It never deletes microSD recordings. Legacy cached files may be hard-linked.

Native HA backups and certified snapshots exclude the camera hot cache while
preserving unrelated `www` files, auth, registries, integration sources, SQLite
and checksums. Core takeover can refetch a missing clip. Live viewers reconnect
on relay transfer; seamless camera-owner handover is not guaranteed. Readiness
proves the local runtime is ready, not that an unreachable camera has recovered.

Pending disk configuration uses the existing manual Sync config workflow. Its
native notification respects dismissal until the next clean-to-dirty transition.
A clean checkpoint loads the component installer in a fresh process. A failed
handoff checkpoint retains the owned generation and restarts local Core instead
of releasing authority to an older cold restore.

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

Keep these deployment boundaries:

| Entrypoint | Scope and required order |
| --- | --- |
| `camera-media.yml` | Maintain the prepared worker deployment, with existing policy retained |
| `camera-helpers.yml` | Controller-only refresh; insufficient for coordinated worker/Core changes |
| `camera-recording-owner.yml` | Compatible workers, certified early Core guard, then owner authority; reverse cutover required for removal |
| `camera-media-delivery.yml` | Idle media, controller support, ingress, then certified Core viewer client |
| `camera-observations.yml` | Read-only worker validation before certified Core source switch |
| `camera-commands.yml` | Compatible inactive workers, certified Core write guard, then explicit activation; ordered disable rollback |

Close all viewers before controller maintenance, including cached-media viewers.
Keep matching recovery copies and compatible adapters across all three nodes.
Follow each linked contract's rollback order; do not replace these cutovers with
an ordinary role run or restore an old snapshot over current user configuration.

Existing installations and recovery nodes reuse the retained private
`core-migration.json` and optional `core-additions.json` exports. The camera role
copies these inputs; `camera_config.py` applies them only through the existing
stopped-Core lifecycle, preserving migration markers and rollback backups.
Do not regenerate camera identities or replace the retained exports as part of
routine deployment.

Manually invoked node tools live in `roles/desk_camera/tools/`, outside offline
test discovery. `verify_transfer.py` and `verify_camera_transfer.py` create real
owner drains. `verify_bundle.py` reads/decrypts an installed recovery bundle;
measurement/fleet/control tools also require node state and credentials. These
are operator tools, not offline tests, and are never auto-run.
