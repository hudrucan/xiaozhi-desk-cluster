# Network CPU hotfix

Home Assistant PoC remains stopped by user request. This change targets the existing Network service only.

- The warm supervisor now observes its direct backend child with nonblocking `waitid`, instead of traversing `/proc` every 100 ms. Lease deadline checks retain their cadence. Deactivation still kills and verifies every writer process group before publishing inactive.
- Checkpoint create/validate workers run in transient systemd services inside `desk-network-checkpoint.slice`. The shared slice is capped at 50% of one core, 384 MiB (256 MiB until 2026-10-17, see `DESK_NETWORK.md`) and 24 tasks; each worker also has CPU/memory/task/runtime bounds. They bind to the controller lifetime and are explicitly stopped on caller failure. No unbounded Linux fallback is allowed.
- The controller's lease/watchdog service is not throttled. Checkpoint validation, quorum signatures, atomic activation, periodic checkpoint cadence and SQLite format remain unchanged.
- Stage RPC timeout is 25 seconds to accommodate validation under the reduced budget. Worker execution is bounded at 50 seconds, with caller timeout 60 seconds. Oversized or corrupted snapshots still fail closed. Native HTTP mutation replication can take longer; pending replication must remain visible on failure.

Measured canary with about 23 MiB SQLite: create 10.00 s, validate 4.69 s, worker mean 49.45% of one core. Active warm supervisor fell from about 10.6% to 1.0% of one core over short samples. These are component measurements, not a claim that all cluster CPU spikes are fixed.

`network-cpu-canary.yml` targets one synced standby first with rollback. `network-warm.yml` rolls standby nodes one at a time before restarting the original owner and checks the existing protected services. `control-plane-cpu-fix.yml` is a separate two-file hotfix runner for the tested server defaults cache; it requires the exact original file hashes, preserves a root-only backup, uses serial restart and checks local Cloud/NATS readiness. It does not deploy unrelated server source or conversation workers.

Focused Network validation: 13 warm lifecycle/fencing tests, 3 budget runner tests and 27 existing HA/proxy/checkpoint tests passed on the source host. Tests use a subprocess harness for checkpoint protocol cases on non-systemd CI; budget runner behavior is tested separately and verified by the real systemd canary. Do not disable tests on Linux merely to make them pass.

Rollback: each node has `/var/lib/desk-network/cpu-fix-backup` (first canary) or `/var/lib/desk-network/warm-rollout-backup` (rolling update). Restore previous `ha.py` and `warm_runtime.py` on a standby, restart only its Network controller, wait for synced inactive/voter-ready, then roll the owner. Keep the extra budget helper/slice until no caller references it. Control-plane backup is `/var/lib/desk-control-plane-cpu-backup/google_drive_config.py`; restore it together with the retained `config_loader.py` and restart only the local control plane.

Validation after rollout: one native-ready owner on deskb1x, standby runtimes inactive on deskb2x/deskb3x, voters ready, all replicas committed the new owner's revision `[42, deskb1x, 1]`. The periodic checkpoint succeeded under the new budget. The control-plane canary passed 101 offline Cloud-config tests and live Cloud/NATS readiness. A simultaneous 64-second observation saw old node 2 peak at 95.34% of one core and patched node 3 at 60.44%; means were 5.84% and 5.65%, but the sample includes different numbers/phases of periodic syncs, so these means are not a controlled percentage improvement. This optimization reduces repeated parsing; it does not eliminate all reconciliation cost.

All three control-plane nodes completed the serial one-file rollout with Cloud configuration synced revision 11 and NATS connected; protected-service guards passed. The source change in `google_drive_config.py` caches parsed defaults only within a thread-local, locked operation, returns isolated copies and clears the cache even on exceptions. Fresh operations reload defaults, and every normal reconciliation still reads the live Cloud manifest.

The remaining parser hotspot uses PyYAML SafeLoader's Python implementation. Existing runtime already bundles LibYAML; the follow-up uses CSafeLoader with SafeLoader fallback, both safe constructors. On RK3528, the same 15 public reference YAML files parsed to identical values: 0.35523 CPU seconds with SafeLoader versus 0.03462 with CSafeLoader (about 90% less parsing CPU). Offline Cloud tests including rejection of Python-object YAML tags: 102 passed. No package installation was required.

Final parser canary, 64-second live sample on deskb3x: mean control-plane CPU 2.033% of one core, peak over two seconds 21.833%; Cloud reconciliation ready and NATS connected. The original observation on that node peaked near 96% of one core. Timing/sync phase differ, so this remains short-sample evidence, not a sustained-load benchmark.


## Native read models and UI requests

The pinned native backend used to query and JSON-encode unchanged tables every
five seconds. A live read-only benchmark found ~13 MB of notifications costing
~1 CPU-second per pass, plus history. The warm runtime now wraps only static
native API endpoint construction. SQLite `data_version` detects external commits
and connection `total_changes` detects backend writes. A changed connection,
query, explicit force/ad-hoc refresh, metadata error or 30-second maximum age
refreshes the model. Native deferred writers remain active. Custom SQL and
time-dependent device/resource models are not cached. No live SQLite file or
configuration is cached or replicated by this helper.

POST card rendering and SELECT-only `/dbquery/read` are read requests; they no
longer trigger quorum checkpoint work. Unknown SQL, malformed input and writes
retain the mutation path. Actual settings edits still require the existing
quorum/checkpoint flow. The Save button reports Saving, Saved, Sync pending or
Retry save instead of silently waiting. Routine closed health-probe sockets do
not emit traceback logs; unexpected server errors remain visible.


Follow-up rollout (2026-10-16): all three Network nodes passed the rolling
upgrade and protected-service guards. Owner became deskb2x, two warm standbys
remained inactive, and the user's new one-minute scan schedule was preserved.
Three native read POST probes returned 200 in 30/123/51 ms with the same
checkpoint revision before and after. SQL probe used the same-origin headers
required by the existing native nginx policy; a header-less external probe
correctly returned 403.

CPU evidence is phase-dependent: the initial 20-second pre-fix container sample
averaged 16.16% of one core, but post-upgrade samples overlapping bootstrap,
scan/import or settings edits still averaged 16–19% with the native 0.5-core
ceiling. A subsequent quiet 20-second per-thread sample measured main backend
3.35%; another quiet per-process sample measured backend 1.77%, warm supervisor
1.04% and HA controller 6.95% of one core. These are short idle-window
measurements, not a controlled whole-node percentage improvement or a claim
that scan/checkpoint bursts disappear. The user confirmed the new Save feedback
works; the resulting settings were quorum-synced. Home Assistant stayed stopped.
