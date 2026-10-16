# Camera media workers

Scope: three Tapo C222 cameras at `.160` (Bed Cam), `.161` (Makeup) and `.163` (Guess). Core and authentication remain
single-active in the existing Home Assistant deployment. This is distributed live
media, not a universal distributed integration runtime.

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
