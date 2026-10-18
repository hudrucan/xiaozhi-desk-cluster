# Tapo distribution: remaining Core dependencies

Research date: 2026-10-17. Code baseline: `2efc2bf`. This is an investigation and
staged design. No camera command, stream, recording read, Core restart or owner
transfer was initiated during this investigation. The subsequent phase-0 rollout
on 2026-10-18 is recorded separately in `CAMERA_MEDIA_SESSION_VALIDATION.json`.

Deployment update, 2026-10-18: owner-only recording metadata/session authority and
[Core-independent browser delivery](CAMERA_MEDIA_DELIVERY.md) are now deployed
for Bed/Guess. A live 1440p viewer survived planned Core handoff. Phase 3A also
moves all three cameras' periodic `getMost()` status batches to their owners;
native entity IDs and mapping are preserved. Recovery is now r62 synced 3/3.
See [owner-side observations and remaining Core dependencies](CAMERA_OBSERVATIONS.md).
SDK startup/authentication, ancillary reads and other native commands remain
in Core; plugs remain research only. The original audit and staged roadmap below
are historical.

Phase 3B's [PTZ-arrow/preset/privacy command pilot](CAMERA_COMMANDS.md) is now
deployed for Guess only, with native privacy routing and same-ticket recovery
checked live. Bed/Makeup commands keep their native Core path. The actual pinned
SDK also passed fixture tests with HTTP/socket I/O mocked. See the pilot's
validation evidence for the distinction between ACK/readback and physical motion.

## Conclusion

The next useful boundary is an **owner-side recording/session service**: move
recording dates/index reads and media handle allocation together, then make Core
consume typed results. Moving more decoding work will not remove the main Core
dependency; decoding, SDK playback and thumbnail fetching are already external.

Treat worker execution survival, browser playback continuity and Core-free camera
control as three different milestones. The first does not automatically provide
the other two. Prioritize Tapo before the lower-priority smart-plug framework.

## Evidence and current boundary

The deployment bundle checksum matches `roles/desk_camera/UPSTREAM.json`:
Tapo Control 7.2.8 at `057d7a8d0b47cbde283b77c19ca27bf4274b7594`, WebRTC
Camera at `6471e0039c5c285634fe207587673144b5d9dc53`, and installed worker
`pytapo==3.4.26`. The running Core's native `__init__.py` and `media_source.py`
match the bundle; its owned `camera_recordings.py` and `recording_sessions.py`
match this checkout. Current upstream SDK documentation can describe newer code;
the installed version, not the latest README, was used for retry analysis.

| Responsibility | Actual location | Dependency that remains |
| --- | --- | --- |
| Live RTSP/WebRTC relay and camera lease | Existing per-camera worker owner | Browser signaling/auth/card lifecycle still involves HA; do not infer uninterrupted viewing from relay readiness |
| Thumbnail fetch and one-frame decode | Warm SDK process on camera owner | Core allocates the native handle, schedules requests and retains JPEG cache |
| Playback download/decrypt/remux/HLS production | Warm SDK process on camera owner | Core supplies media handle and manages viewers/admission |
| Recording date/day index browsing | Core, through inherited native media source | Requires Core's SDK/controller and its local index/cache state |
| Storage status, control polling and device entities | Native Tapo Control on Core | Device sessions, status reads and PTZ/control actions remain Core-bound |
| HLS/MP4 delivery to the browser | Authenticated Core streaming views | Each playlist/segment or file request passes through Core |
| Viewer membership and renewal | Core RAM plus worker-side expiry | Clean Core shutdown cancels jobs; abrupt Core loss eventually expires managed jobs |
| HA UI, recorder and automations | Single-active Core | Remain Core responsibilities in this proposal |

Owned code establishing these boundaries:

- `camera_recordings.py:154` and `recording_thumbnails.py:127` call the Core
  controller's `getUserID()` and send `user_id`/`player_id` to a worker.
- `camera_recordings.py` inherits native browse/index paths. The pinned native
  `media_source.py` calls `getRecordingsList()` and its `utils.getRecordings()`.
- `playback_worker.py:21` installs the supplied handle, then line 26 calls the
  worker client's `getRecordings()` to validate the requested clip.
- `recording_sessions.py` keeps viewers in memory, sweeps every five seconds,
  and clears/cancels its managed jobs on `EVENT_HOMEASSISTANT_STOP`.
- `playback.py` gives managed viewers a 30-second deadline. The warm runtime has
  a separate eight-second controller heartbeat and camera-generation fence.
  Losing Core is not the same as losing that controller heartbeat.
- `recording_view.py` serves signed HA URLs and proxies bytes from workers;
  `camera-card.js` routes PTZ to existing HA button entities and pauses its
  reconnect watchdog while the HA connection is unavailable.

The read-only status sample found Core ready on deskb3x, certified r49 synced on
three nodes, with no pending Core changes. Camera `.160` was on deskb1x and
`.161`/`.163` on deskb2x; all had one local-ready owner, all voters were ready and
NATS was connected. All warm SDK sockets responded and no playback was active.
Sticky healthy ownership permits this 1/2/0 placement; it is not proof of a fault.
Two owners on one node also share the explicit one-active-playback-per-node limit.

Native entry settings showed 30-second main control polling and disabled sound
detection, time sync, native thumbnail cache and native thumbnail preload. Media
sync is stored separately in the native per-entry Store, not in config entries;
all three deployed Stores also had media sync disabled. The migration must
recheck those flags and other native media entry points rather than assume that
future native options cannot create a competing session.

## Finding 1: handle authority is not enforced across SDK retry paths

The documented policy says Core is the sole media handle allocator. That describes
the normal request path, but the installed `pytapo.getRecordings()` catches mapped
`-71103`/`-71105` errors and calls `self.getUserID(True)` before retrying. A worker
can therefore replace its supplied Core handle internally.

An offline probe executed the two extracted installed methods against a stub
transport. Both injected error branches produced:

```text
searchVideoOfDay(core-issued-handle)
getUserID()                         # automatic forced refresh on the worker
searchVideoOfDay(worker-issued-handle)
```

Both probes passed. This proves that code path exists, not that these errors are
currently occurring on a camera. The previous measured `USER_ID_EMPLOYED` incident
is documented in `CAMERA_MEDIA.md`; a new production incident was not reproduced.

The installed SDK constructor and `getMost()` were also inspected statically.
Their direct calls/string literals do not show recording-handle allocation. That
supports evaluating a media-only first phase while ordinary control polling stays
on Core, but is not proof that every native feature is independent of media state.

A near-term hardening patch could keep Core authority and stop workers refreshing
handles implicitly: detect invalid media handles, fail with a typed result, drain
the attempt, and let the designated allocator refresh under admission control.
That improves correctness but does not distribute the allocator. A future owner
service must centralize the SDK's implicit refresh as well as explicit allocations.

## Finding 2: moving the allocator alone is insufficient

Native day-index reads can themselves allocate or refresh the handle. Core's
thumbnail diagnostic, inherited browse paths, manual native downloads and optional
background media tasks also need an explicit routing/disable decision. Keeping
these paths direct while moving only `getUserID()` would leave multiple authorities.

Move these operations as one per-camera authority boundary:

1. Recording dates, day index, device time correction and their bounded caches.
2. Handle allocation, refresh and lifetime; never expose the raw handle to Core.
3. Thumbnail/playback admission, serialization, cancellation and cleanup.
4. Storage-status observations needed to decide whether recordings are available.

Use the existing camera owner/generation, not a second independent lease for the
same SDK session. Start with a small versioned RPC allowlist such as recording
dates/index, prepare, job status, attach, detach and cancel; never expose arbitrary
SDK methods or Python attribute access. Keep binary media on existing scoped HTTP
paths, outside NATS and the election loop.

The warm process can host a per-camera session manager and cached SDK client.
All media-handle mutations go through its serialized admission path. Refreshing
an expired handle while a playback stream is using it must either wait for that
stream or drain it explicitly. Cache by camera identity, generation and query;
report stale/partial results honestly instead of treating an unavailable index as
an empty SD card. Preserve device-clock/timezone interpretation and clip bounds.

Core then renders the native browser from typed dates/index/storage results. It
can retain media URIs and existing registry links without carrying SDK handles.
Ordinary native Tapo control remains temporarily on Core in this first phase.

Phase 1A's [recording contract and ticket lifecycle](CAMERA_RECORDING_CONTRACT.md)
now has a phase-1B SDK/transport/Core binding. Guess was cut over first, with real
metadata pagination, playback/cancellation and a controlled owner-transfer test.
The same coordinated guard-before-worker cutover promotes Bed after that canary;
Makeup remains live-only. Native control polling still runs in Core. Final source
hashes, deployment state, measurements and remaining limits are kept in
`CAMERA_RECORDING_OWNER_VALIDATION.json`.

## Finding 3: execution survival and seamless playback require different work

Today a clean Core stop intentionally cancels browser-managed work, and an abrupt
loss stops managed work when viewer renewal expires. Simply deleting cleanup or
extending its timeout would trade this for orphaned SDK jobs and stuck admission.

To let an existing viewer outlive Core, move viewer membership to the worker and
define authenticated attach/touch/detach across Core generations. The worker must
still distinguish closing the last viewer from losing a Core connection, reject
late messages for an old attempt, and expire abandoned jobs. Index/session changes
alone do not deliver this milestone.

Browser playback still traverses Core's authenticated HLS/MP4 views. Even a
surviving producer cannot keep those HTTP requests alive through a Core outage.
Uninterrupted delivery would additionally need stable media ingress with scoped,
expiring viewer/read capabilities validated outside Core, plus reconnect and
revocation rules. This is a separate security/lifecycle design, not permission to
expose current worker endpoints directly to browsers. Current HA authentication
and private worker ACLs remain appropriate until that design is validated.

Worker owner loss remains different again: its old camera session is fenced, and
a successor must acquire a fresh one. Cross-owner playback is reconnect/resume,
not preservation of the old TCP/media session.

## Recommended sequence and tradeoffs

| Phase | Deliverable | Main benefit | Cost/risk |
| --- | --- | --- | --- |
| 0 | Explicit SDK handle-refresh authority in the current path | Close the implicit-refresh gap with a narrow change | Low/medium; expiry behavior and cleanup must be tested |
| 1 | Owner-side recording/index/session service | Remove the main media SDK dependency on Core; one authority per camera | Medium/high; coordinated native media cutover |
| 2 | Worker-owned viewer lifecycle and HA reattachment | Core reconnect does not inherently kill an admitted job | High; close/cancel/auth behavior across generations |
| 3 | Selected control/status operations outside Core | Distributed polling/PTZ/settings with a thin HA entity layer | High; feature parity and explicit registry migration |
| Optional | Core-independent media ingress | Existing viewer can continue through a Core outage | High; capability validation, revocation and routing |

For phase 3, inventory the actually used entities/actions first. Current cards
reference `button.bed_move_*`, `button.makeup_move_*` and `button.guess_move_*`.
Changing integration platform/domain does not automatically preserve their
registry identity, automation references, areas or dashboard configuration.
Prefer a small supported capability contract over a universal proxy for the full
upstream integration. Do not resurrect the retired pilot or run full HA instances
inside workers. Move optional/destructive device operations only with explicit
feature scope and matching validation.

Avoid aggressive automatic rebalancing or increasing concurrent playback while
changing session authority. Both would add new failure modes to the same cutover.
Preserve independent lease renewal, current TTL/quarantine, bounded SDK processes,
node memory admission and playback priority over thumbnails. Investigate splitting
artifact HTTP serving from election CPU budgeting if load measurements warrant it;
the prior controller-throttling result does not justify another unmeasured rewrite.

## Cutover and acceptance criteria

Deploy backward-compatible workers first while keeping Core authority. Only enable
owner authority for a camera after all eligible nodes and the Core adapter support
the same contract. Stop new media admission, drain the old attempt/handle, then
switch authority once. Ensure native media/background paths cannot allocate a
competing handle. Replicate the compatible Core configuration before relying on
Core failover. Rollback drains the new authority before restoring the old one;
avoid restoring an old HA snapshot over unrelated user changes.

Validate in this order:

1. Offline expiry/retry, stale generation, lost start reply, late cancel, index
   freshness, bounded responses and timestamp/unit behavior.
2. Read-only index/session canary on one SD-equipped camera; compare normalized
   results to the old path. `.161` has no microSD and must retain live-only behavior.
3. Thumbnail versus playback contention, cancellation and app coexistence. A
   third-party app is not governed by our lease; retain clear busy/retry results.
4. Reversible worker handoff and Core restart/reconnect as distinct scenarios.
   Check unique ownership, no concurrent handle allocators, job cleanup and
   unchanged unrelated cameras. Do not claim seamless playback from readiness.
5. Measure time to first playable segment, cancellation-to-admission latency,
   owner recovery, controller throttling, peak RAM and whole-node idle CPU. SDK
   exceptions must be counted by type without logging credentials/handles.

The expected benefit is a clearer fault boundary and fewer conflicting sessions.
No new CPU-saving percentage, uninterrupted-playback claim or failover duration
has been measured for the proposed design.

## Validation performed during research

Phase 0 has subsequently been implemented and deployed to all three nodes; see
the [Core media-handle guard](CAMERA_MEDIA.md#core-media-handle-guard--phase-0).
Core r51 is synced 3/3 after a clean restart. A live r50 canary exposed stale Core
handles between media operations; the follow-up now obtains a fresh handle on Core
at each serialized uncached-playback admission. Two consecutive Guess recording
canaries, cancellation and thumbnail recovery then passed on r51. Worker readiness
and synthetic guard checks passed on all nodes. The research observations below
describe the pre-patch deployment; Core failover and app coexistence remain untested.

- Pinned bundle verification and four deployed source checksums: **PASS**.
- Installed SDK version/source inspection: **PASS**, pytapo 3.4.26.
- Two offline injected SDK refresh branches: **PASS**.
- Read-only three-node owner/voter/warm-worker status: **PASS**.
- Selected native feature flags and per-entry media-sync Stores: **PASS**;
  background media sync was disabled on all three entries.
- New stream/thumbnail/index operations, physical control, handoff, Core restart,
  power-cut behavior and proposed migration: **NOT RUN**.

Public references: [pytapo](https://github.com/JurajNyiri/pytapo),
[HA entity lifecycle](https://developers.home-assistant.io/docs/core/entity/),
[HA data fetching](https://developers.home-assistant.io/docs/integration_fetching_data/).
These inform the design; deployed behavior above was established from pinned and
installed source rather than assuming the latest upstream code is installed.
