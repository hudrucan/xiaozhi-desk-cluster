# Home Assistant Core HA

Native UI: `http://192.168.1.186:8123/`. Use the existing Home Assistant account;
its authentication remains enabled. The management VIP forwards HTTP/WebSocket
traffic to the one native-ready Core, independently of which node holds that VIP.
This phase installs Core only. Camera/media workers, HRI and other devices are deferred.
The former Generic Camera PoC entry is retained but disabled on initial import.

## Ownership and failure behavior

All three nodes run `desk-homeassistant.service`; only its majority-lease owner
starts `desk-homeassistant-core.service`. Images and encrypted certified bundles
are available on standby nodes; the standby Core container is stopped.

The adapter reuses Network's `Lease` implementation unchanged and Monitoring's
encrypted configuration journal unchanged. It does not create another cluster or
JetStream stream. Scoped signed Core NATS RPC carries election/journal metadata;
encrypted bundles use authenticated private TLS between fixed node addresses.

A separate `runtime.py` wrapper checks the owner's BOOTTIME permit every 250 ms.
The local deadline is 10 seconds, shorter than the 15-second voter lease. An
expired permit kills the previous Core namespace before a peer can take over.
Restarting a voter imposes the existing 17-second quarantine. New owners restore
a verified certified generation atomically and cold-start Core. Readiness requires
Home Assistant to finish startup, rather than merely return its login page.
Placement uses fresh CPU, free memory and existing service roles; a healthy owner
is not moved just to rebalance. Core needs 1800 MiB available to become a candidate.
The container has a 1536 MiB memory ceiling and one-CPU ceiling.

## Config durability and RPO

**RPO is the last certified snapshot, not the most recent UI Save.**
There is no periodic stop/restart or automatic snapshot after a Save. If Core
fails abruptly, changes since that checkpoint can be lost, including auth/config,
registries, automations, integration state and local history since that point.
After meaningful configuration work, open **Cluster** in the HA sidebar and press
**Sync config**. This creates an online native HA backup and replicates it while
Core continues running. Nothing automatically syncs after a Save.
The existing clean-stop checkpoint remains available for maintenance.
Planned controller updates refresh standby nodes first, then cleanly checkpoint
and hand off the current owner before its restart. A native HA Restart gets a
startup grace period in the existing namespace and does not restore an old bundle.

The maintenance checkpoint stops Core cleanly, snapshots YAML, `.storage`, secrets and custom
integrations, validates checksums and SQLite, encrypts the archive, stages it on
at least two nodes, and certifies the revision through the existing journal.
Core then restarts. Save operations in HA itself do not restart the container.
A planned handoff uses the same checkpoint before releasing ownership.

Manual online sync uses the pinned native Backup manager and its Recorder
pre/post hooks. Recorder pauses database writes during the snapshot; Core is not
stopped. A 15-second preparation window allows common delayed HA saves to reach
disk. This is a backup of disk-persisted configuration, not a transaction across
every integration's in-memory state. Later writes remain pending. The native
archive is imported into an isolated temporary directory, checked for identity,
size, unsafe paths, JSON and SQLite integrity, then converted to the same encrypted
versioned bundle used by takeover. No live generation is overwritten on standby.
If replication fails, Core keeps running and the previous certified revision remains
the recovery point. A quorum is two nodes; the panel reports each replica separately
and only shows 3/3 once all three have that revision.

The Cluster sidebar panel follows the native HA theme and components. Status and
Sync config use admin-only authenticated WebSocket commands. A root-only Unix socket
exposes only status and ticketed native-backup publication; it has no Core stop or
handoff command, no network listener and no long-lived API token. The container
mounts only that socket directory read-only. Reservations are bound to the current
Core generation and lease term; duplicates and expired jobs are rejected.

SQLite stays a local file. Its clean-stop backup is included in the bundle and
restored locally on takeover; there is no shared database, NFS or distributed SQL.
Bundles reject symlinks, unsafe paths, wrong image identity and oversized data.
Current limits are 64 MiB encrypted, 128 MiB expanded and 8192 files; exceeding a
limit fails checkpoint and leaves the previous certified revision valid. Large
recorder histories or HACS dependencies may require revisiting these limits.
Logs, backups, cached dependencies, media and TTS caches are excluded.
Artifact cleanup retains the seed, certified/accepted bundles, current generation
and three newest artifacts of each kind. Files under one hour old are protected
for in-flight operations; cleanup runs at most once per ten minutes.

From this repository (there is currently no checkpoint button in the UI):

```sh
cd /Users/hudrucan/Personals/repos/xiaozhi-desk-cluster
ansible-playbook -i inventory.ini homeassistant-checkpoint.yml
ansible-playbook -i inventory.ini homeassistant-checkpoint.yml -e desk_homeassistant_operation=handoff
```

The command-line maintenance operation can interrupt UI connections while Core restarts.
The sidebar **Sync config** operation runs online and does not use that lifecycle path. A failed operation
must be checked in public status; do not assume the latest Save was replicated.
Maintenance/reboot waits for a ready successor with matching certified revisions.

## Files and services

- `homeassistant.yml`: staged three-node preparation, serial controller activation,
  native readiness gate, and additive validated ingress listener.
- `roles/desk_homeassistant/files/agent.py`: placement, ownership, checkpoints,
  private metadata/blob endpoints and root-only operation socket.
- `lease.py`, `config_journal.py`: copied existing framework, with equality tests.
- `runtime.py`: independently fenced container lifecycle.
- `ui_bridge.py`: local, restricted manual online sync publication and replica status.
- `component_config.py`: install the owned component on stopped config generations.
- `snapshot.py`, `bootstrap.py`: stopped import, bounded encrypted restore.
- `desk_cluster/`: **Core Health**, readiness and the admin-only native Cluster sidebar panel; no device integration. Its domain and readiness endpoint remain `desk_cluster`.
- `/etc/desk-homeassistant/`: private deployment configuration and scoped keys.
- `/var/lib/desk-homeassistant/`: journal, encrypted blobs and restored generations.
- `/run/desk-homeassistant/public.json`: sanitized current status read by dashboard.
- `/run/desk-homeassistant/admin.sock`: root-only checkpoint/handoff interface.
- Private ports: Core `18123`, metadata `20223`, encrypted transfer `20225`.
- Shared ingress native listener: `8123`, limited to LAN/private source ranges.

Create local ignored credentials once with
`python3 scripts/create-homeassistant-secret.py`. Preserve the ignored
`secret/homeassistant/` directory securely for reruns and disaster recovery.
Never regenerate these keys for a running cluster. Native auth and encryption keys
are not placed in the dashboard, Git, NATS payloads or deployment output.
The initial stopped PoC source is retained untouched for recovery.
The pinned Core HTTP store is normalized only while Core is stopped: port 8123,
all container interfaces and the four exact internal proxy addresses are owned by
this deployment. Other HTTP protections remain intact. Core 2026.10 reads these
settings from `.storage/http`, rather than the already-migrated YAML block.

## Operations and rollback

Inspect `sudo systemctl status desk-homeassistant desk-homeassistant-core` and
`sudo cat /run/desk-homeassistant/public.json` on each node. Public status reports
snapshot age/revision and whether local configuration has changed since that
checkpoint; "synced" refers to the certified checkpoint only.

To suspend Core HA, disable/stop `desk-homeassistant` on **all three** nodes, then
stop `desk-homeassistant-core` on all three and confirm no Core container remains
running. Stopping only one controller intentionally causes takeover. Preserve
`/var/lib/desk-homeassistant` and keys; removing these destroys recovery state.
Do not recreate an old standalone Core alongside the HA deployment.

The primary upstream runtime is the official Home Assistant Container, pinned by
ARM64 digest in the role. Installation reference:
https://www.home-assistant.io/installation/linux/

The host DNS frontend and AdGuard backend both admit `10.88.0.0/16` for Core's
isolated container bridge. Backend access is added with AdGuard's native API;
other access entries and filtering policy are preserved. The conditional DNS
role templates retain this prerequisite on future rebuilds. Rollouts update DNS
serially before freezing the normal protected-service baseline.

HTML navigation shows an automatically retrying reconnect page during cold takeover.
Readiness and API requests still return their real unavailable status. A warming
page is never counted as a healthy Core.

## Initial validation — 2026-10-16

Final state: deskb1x native-ready, two stopped Core standbys, checkpoint r6 synced
on all three nodes. Native HTML, HTTP 401 for unauthenticated API, and WebSocket
101/auth_required passed through the stable ingress. Existing six service UIs
remained HTTP 200. Original user identities and password-provider data matched
the retained source. The user confirmed PASS: the existing account logs in and
reaches Overview through the stable VIP.

Planned checkpoint/handoff took 124.47 seconds. A controller SIGSTOP experiment
recovered on a peer in 116.39 seconds; the old namespace stopped within 11.31
seconds of the test request, with no process overlap at 250 ms sampling. These
are measured cold-start results, not a power-cut test or an uptime guarantee.
The final quiet 20-second sample measured Core at 272.8 MiB working memory and
0.28% of one CPU core; each controller used 23.6–24.8 MiB and 2.52–3.98% of one
CPU core. No HA cgroup OOM kills were recorded. Native UI Restart has an offline
regression and the official supervisor restart path was inspected; an interactive
restart was not exercised. See `HOME_ASSISTANT_VALIDATION.json` for measurements.

## Bluetooth limitation

The host BlueZ adapters are powered, but the current Core container deliberately
has no host D-Bus mount and drops all Linux capabilities. The existing Bluetooth
entry therefore reports its adapter as not found. This is not evidence of failed
hardware. Native Container Bluetooth requires access to `/run/dbus` and additional
capabilities; these have not been enabled by this diagnostic.

The retained Bluetooth entry identifies node 1's adapter MAC. All three nodes have
different local adapters, so granting container access alone would not make that
entry portable during Core takeover. Device integration design must address this
explicitly; a stable remote Bluetooth proxy is an option for later evaluation.
Reference: https://www.home-assistant.io/integrations/bluetooth/#additional-details-for-container

## Display-name checkpoint — 2026-10-16

Checkpoint r7 preserves the user's latest saved configuration and renames the
readiness component to **Core Health** (manifest version 1.0.1). All three encrypted
bundles were checksum-verified and contain the renamed manifest. Core returned
native-ready on node 1 after one clean checkpoint restart. After startup, the only
changed file found against the snapshot manifest was `.storage/core.restore_state`;
the current broad `pending_disk_changes` indicator counts this runtime state too.
Consequently, that badge alone does not prove an unsaved configuration change.

## Online sync deployment

`homeassistant-online-sync.yml` stages the focused upgrade on all three nodes,
refreshes standby controllers first, then preserves current changes using the
existing clean checkpoint/handoff for the one required integration installation
restart. Future normal deployments also install the owned component on stopped
generations. Rollback helper sources are retained root-only under
`/var/lib/desk-homeassistant/online-sync-upgrade-backup`; do not restore old helpers
while an online operation is in progress.

Dirty tracking excludes known restore-state/trace data and backup bookkeeping.
The recovery archive still retains those files. A pending indicator cannot detect
configuration that an integration has not yet written to disk.

### Online validation — 2026-10-16

The native sidebar button completed an online sync to r9, certified and present
on 3/3 nodes. Each encrypted bundle was independently restored into an isolated
temporary directory: 33 files, SQLite quick_check OK, native auth/provider data
retained, and the Cluster component present. The owner's user identities matched
the restored bundle. These were isolated restore checks, not a second live
failover or a physical power-cut test.

A 78.31-second observation (75 samples) around the online operation recorded no
readiness/native HTTP failures. Container and HA process identities were unchanged,
with zero cgroup OOM kills. Core averaged 2.53% of one CPU core and peaked at
43.02%; final cgroup memory was 261.19 MiB. This CPU figure excludes the separately
budgeted snapshot validation/encryption worker and does not represent total node
load. The sync button and replica rows now preserve unchanged DOM between polls,
preventing native button flicker.

Routine availability checks use the explicitly public `/api/desk_cluster/ready`
endpoint. Do not probe protected `/api/` without authentication: the expected 401
still creates a native invalid-login notification. The one-off negative auth
check from node 2 at 14:33:06 was identified in the HA log and removed from the
scratch validation script. Native authentication remains enabled.
