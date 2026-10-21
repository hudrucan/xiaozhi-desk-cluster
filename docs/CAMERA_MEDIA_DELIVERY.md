# Independent camera delivery

The existing native HA Media Browser/player remains the UI. For the selected
owner-only recording workloads (Bed `.160` and Guess `.163`), an authenticated
Core open now obtains an opaque viewer capability from the serving camera
controller. The browser then reads media and sends viewer heartbeat/close directly
through the existing HAProxy listener on port 8123. No additional service, public
listener, broker, SDK process or second cache copy is introduced.

```text
Authenticated open: browser -> Core -> root bridge -> signed camera RPC
Admitted playback:  browser -> VIP:8123 -> exact serving controller:20226
Camera session:     existing owner controller -> existing warm SDK worker
```

The ingress path is `/desk-camera-media/v1/<serving-node>/<capability>/<artifact>`.
It routes to the node holding that clip, independently of Core readiness and of
subsequent camera placement. There is no fallback to a different cache or owner.
Only the fixed artifact names, heartbeat and close routes are accepted. The
private controller accepts ingress from cluster nodes/loopback only. Media bytes
and capabilities never pass through NATS broadcasts or public status documents.

## Viewer lifetime and isolation

- The random 256-bit capability is held in controller RAM and scoped to a viewer,
  workload, clip, worker attempt, HLS generation (or exact cached MP4 stamp), and
  the owner generation while its download is active. It contains no SDK handle,
  camera credential or HA access token. Issuance uses the existing root-only IPC
  and authenticated node RPC; the HTTP endpoint cannot mint capabilities.
- Browser POST heartbeat runs every five seconds. A viewer expires after twenty
  seconds without renewal; media reads do not renew it. Its absolute lifetime is
  two hours, matching the former Core-signed media URL lifetime. A capability
  holder can renew within that interval without contacting Core; HA permission
  changes are therefore not rechecked on every segment/heartbeat.
- Closing one of several viewers releases only that viewer. The last close
  requests stop and acknowledges completion only after the exact worker attempt
  has drained. Late close/touch cannot revive a closed viewer or cancel a newer
  attempt. Expired viewers also trigger bounded orphan cleanup.
- Core shutdown/sweeping no longer cancels an admitted detached job. A newly
  started Core can rediscover the same attempt, but cancelling its temporary
  waiter cannot cancel another browser's detached viewer or extend its deadline.
  After admission, Core cancels only its own progress waiter and dismisses its
  preparation notice. This removes redundant progress polling and avoids turning
  a normal detached close into a Core-side interruption notification.
- Active viewer caches are excluded from cache eviction until their heartbeat
  expires. Completed clips can remain readable through a camera owner change;
  an active SDK job is still fenced on owner/generation loss. This does not
  migrate or resume an in-flight camera download across camera owners.
- Controller restart revokes its capabilities. Maximum registry size is 128,
  closed tombstones are retained thirty seconds, and absolute expiry bounds
  abandoned entries. HTTP serving has eight slots, bounded file/range sizes,
  timeouts, strict playlist URI validation and symlink rejection. Responses are
  private/no-store with no-referrer policy; capability paths are not access logged.

The mounted native player can continue if HA rebuilds its panel during a Core
reconnect. Navigation, closing the popup or leaving the page releases the viewer.
A full page reload/browser restart does not persist or restore its capability.
Opening a new viewer still needs a ready authenticated Core; browsing a fresh
recording list may also need the camera's shared SDK admission to become idle.

## Deployment and rollback

Use `camera-media-delivery.yml` after the certified owner-only recording cutover.
It requires idle SDK workers, matching worker/Core authority and synced recovery 3/3.
It installs controller/helper files one node at a time, waiting for the voter and
warm runtime before continuing. It then validates and reloads the combined
HAProxy configuration, stages Core/bridge/frontend files on all recovery nodes,
restarts standby agents, and certifies the component through a clean Core handoff.
Close cached-media viewers before controller maintenance too: their capabilities
are deliberately revoked by that restart, even when no SDK download is active.
Protected-service PIDs, base ingress, VIP configuration and DNS policy are compared
with their pre-rollout state. The rollout does not change lease/quorum deadlines.

For rollback, first close viewers and require idle warm workers. Restore the
previous Core viewer/frontend/bridge files from the deployment backups on all
nodes and certify them with a clean Core handoff. Keep compatible controller and
ingress support until those clients have stopped; restore their previous files
afterward, validate HAProxy before reload, and roll controllers one at a time.
Do not revoke the separate owner authority/early SDK guard as a delivery rollback.
Preserve this compatibility order during recovery.
