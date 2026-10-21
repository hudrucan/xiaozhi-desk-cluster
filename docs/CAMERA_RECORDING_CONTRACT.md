# Owner recording API

The current implementation selects Bed `.160` and Guess `.163` through the
coordinated `playbooks/camera/camera-recording-owner.yml` cutover. This contract covers the installed
SDK backend, signed RPC and Core renderer. See [current camera capabilities](CAMERA_MEDIA.md)
for the separate observation, command and browser delivery boundaries.

`roles/desk_camera/files/recording_contract.py` defines the metadata boundary.
`recording_jobs.py` admits and fences asynchronous tickets. `recording_owner.py`
adapts the installed SDK in the existing warm worker. Selected cameras allocate
media handles only in that process; Core routes typed metadata and renders the
native browser. Unselected cameras retain the guarded Core allocator.

## Why tickets

The camera NATS transport times out after 1.5 seconds. Camera control HTTP/index
operations can exceed that budget.
Submission must return a ticket immediately; polling returns bounded metadata.
SDK work belongs in the warm process and must never execute in the lease callback.
No binary media, credentials, user ID or player ID crosses this new boundary.

The transport binding verifies the existing signed NATS envelope first,
then passes its verified `sender` to `RecordingJobs.handle()`. The request body cannot
choose its sender or turn on owner authority. Outer envelope IDs remain transport
nonces; `request_id` is a separate application ticket that survives a lost reply.
This contract defines no new NATS subject, ACL, HTTP route or unauthenticated endpoint.

## Request v1

All requests contain exactly these common fields:

| Field | Meaning |
| --- | --- |
| `version` | Integer `1`; booleans and unknown versions are rejected |
| `workload` | Existing `tapo_c222_160`, `tapo_c222_161` or `tapo_c222_163` |
| `generation` | Existing owner generation, 32 lowercase hexadecimal characters |
| `request_id` | New 32-character hexadecimal ticket ID, scoped to verified sender/workload/generation |
| `operation` | `dates`, `index`, `result` or `cancel` |

`dates` and `index` additionally require `deadline` and `query`:

```json
{
  "version": 1,
  "workload": "tapo_c222_163",
  "generation": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "request_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "operation": "index",
  "deadline": 1792278825,
  "query": {"date": "20261017", "cursor": null}
}
```

The timestamp above is illustrative. The caller generates a Unix-seconds deadline
per new request; use a 25-second budget to leave clock-skew margin below the
worker's strict 30-second future limit. Request/envelope timing assumes the same
synchronized clocks as the existing signed RPC. Once admitted, an independent
monotonic deadline prevents a backwards wall-clock adjustment extending SDK work.

`dates.query` is exactly `{"cursor": null}` initially. `index.query` additionally
contains an actual calendar date in camera `YYYYMMDD` form. A later page supplies
an opaque 32-character cursor bound by the SDK backend to an immutable snapshot,
query, camera and generation; a cursor is not an arbitrary SDK offset.

`result` and `cancel` contain only the common fields and refer to the original
read ticket. They do not extend its deadline. An identical read retry returns the
same ticket; changing its body/deadline returns `request_conflict`. Completed and
cancelled records are retained through their retry deadlines. Expired submissions
cannot restart after their records are pruned. There is no durable replay ledger
or cross-generation job recovery in this API.

Existing playback prepare/status/cancel and browser viewer APIs remain unchanged.
They share the same internal authority/admission; this contract does not add
another competing playback or viewer framework.

## Response and time semantics

Every response echoes `version`, `workload`, `generation` and `request_id`.
The states are `queued`, `running`, `cancelling`, `done`, `failed` and `cancelled`.
Only `done` carries `data`. Failures/cancellation carry an allowlisted `reason`,
never SDK exception text, a private handle, an authorization token or media URL.

A completed page has these fields:

| Field | Meaning |
| --- | --- |
| `items` | At most 128 sorted unique camera dates, or `{start,end}` clips |
| `snapshot` | Opaque 32-character snapshot ID |
| `next_cursor` | Opaque continuation cursor or `null` |
| `observed_at` | Time the backend observed this snapshot, Unix seconds |
| `freshness` | `fresh` up to 30 seconds old; explicitly `stale` up to 300 seconds |
| `complete` | Complete snapshot enumeration; cannot be true with a next cursor |
| `storage` | `ready`, `absent` or `needs_attention` |
| `time_basis` | Literal `camera_epoch_seconds` |
| `camera_offset_seconds` | Signed seconds used by the native display conversion |
| `clock_correction_seconds` | Separate SDK clock correction in seconds |
| `date` | Index pages only: the exact requested camera date |

Clip boundaries remain the existing raw camera epoch-seconds values. Native
display currently uses `utc_from_timestamp(start - camera_offset_seconds)` before
conversion to HA local time. SDK media preparation receives its separate clock
correction. Do not add/subtract either field twice or round-trip through host
local-time parsing. Milliseconds, booleans, reversed ranges, ranges over one day
and clips inconsistent with their date are rejected. Adjacent UTC days are allowed
for a camera-local date, matching the existing playback boundary.

Reading a retained ticket ages its observation honestly: a formerly fresh page
becomes stale, and a snapshot beyond its stale budget returns `index_expired`.
Partial enumeration remains `complete=false`, including when no continuation is
available. `storage=absent/needs_attention` requires an empty completed page and
cannot be inferred from a failed SDK call. Unknown storage/read failures produce
`storage_unavailable`; invalid media handles produce `session_expired`. In
particular, do not turn a mapped SDK `-71105` into proof of an empty SD card.

Requests are limited to 2 KiB and complete response bodies to 32 KiB, within the
existing signed 64-KiB envelope. The IPC host enforces byte limits before JSON
parsing. The maximum page also passes through the actual existing
`protocol.pack/unpack` in offline tests.
Metadata responses have a separate bounded path from thumbnail IPC headers.

## Lifecycle and admission

`RecordingJobs` defaults to a closed authority gate. A trusted, local
`owner_authorized(workload,generation)` callback must confirm a coordinated cutover;
it is not supplied by an RPC. The existing permit callback must independently
confirm the exact generation and unexpired BOOTTIME lease. Gate/permit read errors
fail closed. No second lease algorithm or durable session store is introduced.

Submission/polling does no SDK I/O. The async backend runs under a node admission
lock, rechecks its fence after waiting, and has an independent 250-ms fence/deadline
watcher. Ownership and authority are checked again before publishing results;
even a backend that returns late success after cancellation cannot expose it.
The same sender/ticket in another camera or generation cannot cancel a successor.

At most 32 retained/active read tickets occupy the node. Terminal expired tickets
are pruned on new admission, without another idle maintenance loop. Reaching the
bound fails with `capacity`; valid retry records are not silently evicted.

The warm host injects the **same admission lock** used by playback and
thumbnail SDK mutations. Before playback acquires that lock, it calls
`await jobs.pause()`: new reads fail busy, queued reads stop without SDK access,
and an in-flight read drains before the pause acknowledges. Existing completed
metadata can still be read without SDK access. The host resumes new reads after
the media cleanup path. The contract exposes neither pause nor authority changes
to remote callers.

Cancel of running work reports `cancelling` until the backend actually finishes
its cleanup. Repeated cancel does not inject another cancellation into that
cleanup. A queued cancellation can acknowledge immediately because no SDK work
started. Shutdown rejects new work and waits for running SDK cleanup, including
when another subsystem holds the shared lock.

## SDK admission and coordinated authority

Metadata, thumbnails and playback share the warm worker's admission lock. Playback
pauses queued metadata and awaits actual executor cleanup before starting. The
lease subscription only authenticates and schedules metadata IPC; it never waits
for SDK execution. Four outgoing metadata RPC slots reserve capacity within the
existing 32-request transport, including when Core and owner share one node.

The adapter snapshots dates/index results for 30 seconds, pages 128 items, bounds
snapshots/cursors and rejects a day that reaches the 2048-entry SDK fetch ceiling
rather than claiming a truncated list is complete. Raw camera timestamps remain
unchanged. Offsets are separate integer seconds. Unknown SD state, expired session
and lost authority are errors, not empty recordings. No media handles are returned.

Every owner allocation checks the existing permit and generation. The installed
requests-based `pytapo` transport is guarded before and after each HTTP call,
including recursive transport retries, with one-second connect/read timeouts and
at least three seconds of permit remaining before admission. An unaudited
transport fails closed. SDK implicit handle refresh remains forbidden; an explicit
serialized new media operation is the allocation boundary. Cancellation waits for
the current executor call to drain; it is not acknowledged while a thread can
still mutate the camera. Lease and voter TTLs are unchanged.

The Core class guard covers existing and future native SDK instances for selected
hosts, including raw method calls and recording/media-session entry points.
`recording_bootstrap.py` is mounted as `sitecustomize.py` in the camera-enabled
Core, preserving the image entrypoint. It imports no native integration and
constructs no SDK client. It installs the SDK guard before HA starts, checks that
Core policy covers the node's active worker policy and terminates on bootstrap
failure. Native readiness requires this early guard for selected cameras.
The later owned integration skips the native media scheduler for selected entries.
Already-created startup probes may be denied locally by the SDK guard; these do
not send camera media requests. Control coordinator mapping remains enabled;
selected periodic status reads use
[owner observations](CAMERA_OBSERVATIONS.md), and selected commands follow their
[separate contract](CAMERA_COMMANDS.md). Native media sync, thumbnail preload/cache,
sound detection and time sync must remain disabled.

Rollout order is enforced by `playbooks/camera/camera-recording-owner.yml`:

1. Require idle media, healthy quorum and certified recovery on all nodes; freeze
   unrelated services. Install compatible workers with initially empty authority.
2. Stage the Core policy/client/guard on all nodes. Refresh standby HA agents,
   cleanly checkpoint and hand off Core, and verify guarded native readiness plus
   the same certified recovery revision on all three nodes.
3. Atomically update the directory-mounted worker policy only after that check.
   Selected cameras then reject Core handles; mode/generation changes invalidate
   cached worker clients. Core never falls back to native allocation on RPC errors.
4. Verify metadata, playback and cancellation for the selected workloads. Makeup
   `.161` remains live-only.

Worker policy is `/etc/desk-camera/recording-policy/owner.json`. The separate
`recording-core-policy.json` is installed into the certified Core config at clean
checkpoint/activation. The ordinary camera role seeds policies only once and does
not silently switch or revoke live authority. The forward playbook refuses to
remove existing selections. To roll back, first drain media and remove worker
selection on **all** nodes; only then remove Core selection through a clean
certified restart. Reverse order would permit competing allocators.

These guards prove bounded SDK admission, cleanup and result fencing. Tapo does
not consume our generation token; a camera rejecting an already-sent old-owner
request is not guaranteed by the API. A software owner-transfer test must not be
presented as a power-cut/network-partition proof. Admitted browser viewers use the separate
[independent delivery lifecycle](CAMERA_MEDIA_DELIVERY.md); a full page reload
does not restore an in-memory capability.

## Offline validation

The focused contract/lifecycle tests cover the request/result bounds, signed-envelope compatibility,
raw second units and adjacent-day interpretation, stale/partial/no-card metadata,
lost/reordered lifecycle actions, duplicate/conflicting starts, wrong caller or
generation, cancel-before-start, actual cleanup acknowledgement, authority loss,
late success, retained-result owner fencing, monotonic deadlines, shutdown and
playback preemption. Additional tests cover guarded SDK allocation/HTTP retry,
immutable SDK snapshots, Core bypass rejection, same-node async IPC, reserved lease
capacity and a stalled metadata IPC alongside an immediate lease reply. No camera
command or media read is needed for these tests.

```bash
PYTHONPATH=roles/desk_camera/files:roles/desk_camera/tests \
  python3 -m unittest test_recording_contract test_recording_jobs
```
