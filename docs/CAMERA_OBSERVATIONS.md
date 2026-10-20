# Owner-side Tapo status observations

This step externalizes the pinned native integration's `getMost()` status batch.
The three C222 cameras retain their existing native Tapo Control entities, unique
IDs, platform mapping, dashboards and command clients. It does not implement
Core-independent commands or remove the vendor SDK from Core.

## Boundary

`observation_contract.py` pins the legacy-camera batch in pytapo 3.4.26: 87 read
requests, 82 result methods. The owner returns the same bounded result shape to
the existing native `getCamData()` mapper. The adapter also preserves getMost's
local `basicInfo` and preset-cache updates. Vendor integration source is untouched.

The existing warm media process hosts a separate read-only client per locally
owned camera. There is one status read thread at most per node, independent of
the media admission lock: a long recording download must not freeze status.
Standby processes do not log in. Normal autonomous polling runs every 30 seconds,
including when Core is absent. No new service, listener or election is added.

Every camera request is checked against the explicit read request/parameter
allowlist before SDK transport. Unknown methods, media-handle allocation and
writes are rejected, including pytapo's implicit `setCruise(False)` fallback on a
read error. Only the audited legacy transport is allowed; HTTP connect/read
windows are one second. A six-second operation budget and at least three seconds
of remaining owner permit are checked around transport I/O, retries and results.
An already-running synchronous HTTP call drains; its result is discarded after
revocation. Existing lease/quorum timing is unchanged.

Snapshots carry workload, owner generation, a random snapshot ID, channel IDs and
observation time. JSON size is capped at 32 KiB, with shape/depth/item limits and
private-field rejection. No SDK object, token, media handle or credential is
exported. Snapshots travel through existing root Unix IPC and signed NATS;
metadata requests are capped at four and never await camera I/O on the lease
subscription callback.

An early bootstrap adapter replaces only selected cameras' SDK `getMost()`.
Core asks the unique ready owner for a snapshot no older than two seconds; a
cache miss wakes a coalesced owner read and returns `pending` immediately. Core
waits in its SDK executor, with a deadline and failure backoff. Explicit native
refresh after a command requires a different snapshot. A stale, unavailable or
mismatched observation raises an error, without falling back to a camera read in
Core or pretending the SD card is absent. This avoids an extra 30-second cache
cycle on top of native polling.

Camera errors have their own bounded retry backoff; repeated Core requests cannot
bypass it. Owner-generation changes invalidate clients and cached results.

## Remaining Core dependencies

Native SDK construction/authentication, startup discovery/capability probes,
ancillary integration reads and unselected command/action methods remain in Core.
Native entity state publication and automation execution still require Core.
The owner polling service continues through a Core outage. HA automations and
new native command admission still require Core.

Bed and Guess retain owner-side SD recording authority and independent browser
media delivery. Makeup remains live-only. Status polling never allocates a media
handle. Guess has a separate [owner command contract](CAMERA_COMMANDS.md) with
identity/deduplication, ambiguous-timeout handling and generation fencing.
Bed/Makeup commands retain their native Core path.

## Rollout and recovery

The additive role installs inactive observation support. Explicit selection lives
in `/etc/desk-camera/recording-policy/observations.json` (warm reader) and
`/etc/desk-camera/observation-core-policy.json` (Core source policy). The latter is
copied into `.storage/desk_camera_observation_authority` by the stopped-Core hook,
then included in the ordinary certified checkpoint.

`camera-observations.yml --tags workers` installs one controller/worker at a time,
waits for voters and warm readiness, enables read-only shadow observations, and
validates all cameras from each node. During this short validation stage both
Core and the shadow reader can poll. `--tags core` first revalidates observations,
loads the bridge on standby agents, then cleanly certifies current configuration
and hands off Core. Current local configuration saves are preserved in that
checkpoint. Protected services and DNS policy are checked across each stage.

A controller restart revokes direct viewer capabilities: run maintenance with
idle media. Rollback first removes Core observation selection through a clean
certified restart, while the compatible owner reader remains available. Only
then disable warm observation policy and revert worker sources. Do not remove an
adapter still selected by a certified Core policy.

## Validation

New offline checks cover the pinned read batch, private fields, bounded DTOs,
SDK write fallback, lease/deadline loss, generation invalidation, standby silence,
read coalescing/backoff, Core refresh and no direct-read fallback. The dispatch
test holds worker IPC open while verifying a lease reply still completes.

Live validation on 2026-10-18 passed the worker rollout and clean Core cutover.
Core is deskb3x, revision 61 certified/synced on all three nodes, without pending
disk changes. All 327 native entity bindings (228 enabled) have the same hash as
before deployment. Vendor integration sources and the pinned worker SDK match
their original hashes; deployed owned sources matched the phase-3A hashes in the
validation record. Twenty-four
new offline checks and the existing camera/Core/HA regression checks passed.

During planned Core handoff, the status-only watcher sampled 133.477 seconds
without a ready Core. Makeup's autonomous read count advanced 5 → 10; a second
watcher covering 115.345 seconds observed Bed 6 → 10 and Guess 7 → 11. A separate
Guess playback preparation/playable test obtained three distinct status snapshots
while media was busy, then cancelled and drained the media worker.

A controlled Guess handoff deskb3x → deskb2x made the new owner ready after
17.440 seconds and returned a fresh generation-bound snapshot after 18.522 seconds.
Old-generation reads returned unavailable, while Core and the other two owners
stayed unchanged. An earlier node1 → node3 attempt completed the transfer but its
test stopped on a transient unavailable RPC; the repeat test waited through that
interval. This validates recovery with a temporary status gap. Core recorded
three failed reads across these transitions; owner readers ended with zero
consecutive failures. All temporary drains were removed and media was idle.

Final approximately 32-second samples measured whole-node busy at 22.32–23.80%,
warm containers at 47.53–50.27 MiB, and the active Core container at 279.79 MiB.
The earlier short window had different placement and background activity; no
CPU saving or causal regression can be established from these samples. Container
cgroups were measured separately from outer systemd wrapper units. Resource and
lease/quorum limits were unchanged.

See [the sanitized validation record](CAMERA_OBSERVATIONS_VALIDATION.json) for
source hashes, read counts, timings and resource samples. Physical command actions,
power loss, network partitions and a long-duration resource soak were NOT RUN.

The subsequent [command pilot](CAMERA_COMMANDS.md) is deployed for Guess only:
its native PTZ-arrow/preset/privacy methods route to the camera owner. Bed/Makeup
commands retain the phase-3A Core path described above. Core still handles native
startup/authentication, ancillary reads, entities and automations.
