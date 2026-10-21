# Owner-side Tapo status observations

The owner reader externalizes the pinned native integration's `getMost()` status batch.
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

## Deployment and recovery

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
