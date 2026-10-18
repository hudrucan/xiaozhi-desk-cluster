# Owner-side Tapo commands — phase 3B pilot

Status, 2026-10-18: **deployed and activated for Guess only**
(`tapo_c222_163`). All three recovery nodes have compatible workers and the early
Core guard. Bed and Makeup commands retain their native Core path. The installed
pinned SDK also passed fixture checks with all test HTTP/socket I/O mocked.

## Supported boundary

Only these existing native SDK methods are routed to a camera owner when selected:

| Native method / UI | Typed owner operation | Arguments | Outcome evidence |
| --- | --- | --- | --- |
| `moveMotor`, four dashboard arrow buttons | `ptz_move` | One nonzero integer axis, −360…360 degrees | Camera ACK; no physical-position confirmation |
| `setPreset`, Move to Preset select | `preset_goto` | Numeric preset ID present in the owner's current preset map | Camera ACK; no physical-position confirmation |
| `setPrivacyMode`, privacy switch and camera on/off | `privacy_set` | Explicit boolean target | Camera ACK and optional independent state readback |

This preserves the native integration/platform, entities, unique IDs and dashboard
references. Other native commands, including calibration, cruise, alarm, reboot,
SD formatting and firmware operations, remain on their current Core path. This
is not a universal SDK proxy or a complete control migration. Core still handles
UI admission, entity publication and automations, SDK startup/authentication and
ancillary reads. Existing owner recording and media delivery remain unchanged.

The early Core adapter wraps the three selected high-level methods and rejects
direct Core wire writes for `motorMove`, `motorMoveToPreset` and
`setLensMaskConfig`. Selected operations never fall back to a Core SDK write.
Nonselected cameras and other methods retain their existing path.

## Command identity and uncertainty

The issuer chooses an opaque request ID, the unique ready camera owner, its exact
generation, an explicit operation/arguments and an eighteen-second deadline. It
submits **once** through existing root Unix IPC and signed NATS. If the response
is lost, it polls only that ticket on the original node/generation. A temporary
`unknown_request` does not prove that a delayed submit was never accepted. There
is no automatic rediscovery, successor resubmission, inverse action or write retry.
Deduplication covers this invocation/ticket and its internal RPC retries. A fresh
HA service call creates a fresh ticket; external callers must not automatically
repeat an uncertain action as a new service call. No end-to-end browser or
automation intent ID is added in this pilot.
The journal is local to the original owner node, outside HA checkpoints. If that
node is unreachable, its receipt remains unavailable. A Core restart does not
reconstruct an interrupted native service call or automatically replay it.

The existing warm process has one command lane per node, independent of the media
admission lock. It persists the queued ticket before acknowledgement and fsyncs a
running marker immediately before the first actual device HTTP write. The marker
and final receipt use atomic replacement and directory fsync. The root-owned
journal lives at `/var/lib/desk-camera/commands/ledger.json`, mounted writable at
`/commands`; no new process, listener or resource limit is added.
Journal writes drain in executor threads, and receipt reads use the last committed
immutable record so a slow fsync does not block the warm event loop.

The ledger deduplicates workload/request ID across trusted cluster senders and
rejects changed arguments, deadline or generation for an existing ticket. A
restart converts queued work to rejected and running work to unknown; it never
replays either. Historical receipts can be queried after owner loss without
reauthorizing execution. A new ticket still requires current generation, selected
authority and at least three seconds of remaining permit, checked again after
fsync. A six-second SDK operation budget and `(1,1)` HTTP connect/read timeouts
bound the dispatch path. Already-running synchronous I/O drains.

The adapter pins hashes of the installed pytapo 3.4.26 SDK and legacy transport.
It blocks all three relevant retry layers: SDK recursive requests/implicit cruise
writes, transport error/reauth retries, and low-level connection-reset retries.
A command uses a short-lived SDK client and closes its transport/event loop after
completion. Credentials, SDK objects, session handles and raw responses never
cross the command API.

Receipts distinguish:

- `acknowledged`: a successful device response, without claiming physical motion.
- `reconciled`: ACK was uncertain, but an independent privacy read observed the
  requested absolute state. This confirms the observed state, not which actor set it.
- `unknown`: a write may have happened, with insufficient evidence to resolve it.
  PTZ/preset uncertainty remains explicit; the adapter raises a native service error.
- `rejected`: no dispatch was admitted, for example expired generation, overload,
  disabled authority, invalid preset or unavailable durable storage.

Privacy readback never retries a write. A worker restart returns the persisted
unknown receipt; it does not automatically reconcile or replay it. Post-command
observation invalidation also discards any status read that started before command
completion, and Core requests a new snapshot even if native polling did not force
one. Camera read-error backoff remains intact.

The ledger retains up to 128 tickets per node until fifteen minutes after their
deadlines; overload rejects new work instead of evicting retained identities. A
persisted wall-clock high-water mark prevents a large clock rollback from making
a pruned command valid again. Corrupt/unwritable journals fail closed. Do not
remove the ledger, restore an old copy, or create a new generation/ticket to retry
an uncertain command automatically. This provides bounded at-most-one dispatch
attempts, not camera-enforced exactly-once physical execution or partition fencing.

## Canary rollout and rollback

`camera-commands.yml` defaults to **Guess only** (`tapo_c222_163`). It requires
phase-3A owner observations and synced three-node recovery, and issues no physical
camera command itself. With idle media:

```bash
ansible-playbook -i inventory.ini camera-commands.yml
```

The ordered stages can also be run separately with `--tags workers`, then
`--tags core`, then `--tags activate`. Worker installation preserves inactive
write authority. The Core stage installs the early guard on all recovery nodes,
loads standby bridges first, and preserves current config through clean handoff.
Activation checks certified guard readiness before enabling owner writes. A gap
between guard installation and activation temporarily rejects the selected UI
commands. Lease/quorum timing, SDK dependencies and resource budgets are unchanged.

After the Guess canary is physically checked, expand the explicit selection:

```bash
ansible-playbook -i inventory.ini camera-commands.yml \
  -e '{"desk_camera_command_workloads":["tapo_c222_160","tapo_c222_161","tapo_c222_163"]}'
```

The validated Guess selection is recorded in `group_vars/deskboxes.yml` as
`desk_camera_command_workloads` so provisioning a new recovery node seeds the same
Core guard. Generic role defaults remain empty. Forward
rollout rejects authority removal; use the ordered rollback instead:

```bash
ansible-playbook -i inventory.ini camera-commands.yml \
  -e desk_camera_commands_disable=true
```

Rollback first disables owner write admission everywhere and waits for attempts
to drain. It then certifies empty Core command selection through clean handoff,
restoring the original native path. It retains the compatible modules and journal.
Do not reverse that order or restore an old HA snapshot over user configuration.

## Validation and remaining checks

Offline tests cover duplicate/lost ACKs, same-ticket recovery after restart,
conflicting retries, generation/deadline loss, durable-storage failure, capacity,
clock rollback, native method routing/raw bypass rejection, fresh post-command
observations, and lease subscription responsiveness during slow command IPC.
Seven additional checks run actual installed SDK request/transport code with HTTP
and socket connections replaced by fixtures. They cover positive ACK, readback,
unknown preset, lease loss, and all audited SDK retry paths.

Live Guess validation passed the ordered workers/Core/activation stages. The
first worker pass exposed an invalid final isolation-guard CLI invocation; it was
corrected to `guard` with frozen state on stdin, covered by two regression checks,
and rerun successfully before Core cutover. Recovery advanced from r61 with pending
disk changes to clean r62 synced 3/3. Core recovered on the same node, deskb3x;
the sampled readiness gap was 143.4 seconds. Owner observation reads continued
during the gap. All 327 entity bindings (228 enabled) and five YAML/dashboard
file checksums stayed unchanged. Staged sources, command policies and pinned SDK
hashes matched on all nodes; 17 native vendor files matched on active Core.

Two live probes invoked the native wrapped privacy method from separate Python
processes in the Core container. Both set the existing `off` state again, received
camera ACK and matching independent readback, and obtained fresh post-command
status snapshots. Each deliberately dropped the bridge submit reply and completed
with exactly one submit, polling the same ticket; command completion took 2.142
and 2.289 seconds. These are adapter/bridge checks, not HA frontend/service-call
tests or lost camera-HTTP-ACK tests.

Replaying the completed ticket through all three root bridges returned the same
receipt without changing the journal. Conflicting, expired and wrong-generation
requests were rejected. A warm-worker restart preserved the completed record and
replay behavior without changing the camera generation. A controlled idle Guess
handoff moved deskb1x → deskb3x; both nodes rejected the old generation, the old
receipt remained on its original node, and the successor supplied a fresh snapshot
and passed the second privacy probe. Core and the other camera owners did not
change. The initial handoff probe caught transient `local_ready` with a null
generation; follow-up waited for a valid generation and a ready snapshot. No
usable-generation handoff latency is claimed. The temporary drain was removed.

The final 31-second sample measured whole-node busy CPU at 20.34%, 26.28% and
22.70%, with 2208/1973/1914 MiB available RAM. Camera controller memory was about
21 MiB per node, warm container memory about 48 MiB, and active Core container
memory 285 MiB. This short sample does not establish savings or sustained peaks.
Camera command storage/clock checks and observation failure counters were healthy.

The extra PID audit also caught **desk-network OOM on deskb1x at 10:34:42 +07**.
Kernel logs identify its own 256 MiB cgroup limit. It automatically restarted and
network recovered on deskb2x with sync 3/3. Network sources/limits were not changed;
the cause of its memory growth needs separate investigation. Protected voice/DNS/
ingress process PIDs predate the rollout; dashboard/monitoring PIDs were unchanged.

See [validation evidence](CAMERA_COMMANDS_VALIDATION.json) for sources and results.
Physical PTZ/preset motion, a privacy toggle, actual HA frontend service calls,
in-flight Core/owner loss or partition, cross-host Core recovery, rollback and
sustained resource measurements remain **NOT RUN**. Next validate a small arrow
movement, preset and privacy toggle with visual confirmation before expanding
authority beyond Guess. An ACK alone does not validate physical motion.
