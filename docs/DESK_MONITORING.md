# Optional Beszel monitoring trial

Open `http://<current Xiaozhi management VIP>/monitor/` (currently
`http://192.168.1.186/monitor/`). No new public IP or VIP is assigned.
Beszel 0.21.0 runs as checksum-pinned ARM64 binaries under systemd, without
Docker or vendor installation scripts. Both executable and service names use
kebab-case.

All three nodes run one independent hub and one agent. Each hub monitors all
three private agent endpoints. Each agent accepts only the three hubs' public
SSH trust keys; this listener provides Beszel metrics, not a shell. The agent
listens on its private IP at 45876 and the hub on its private IP at 8090.
HAProxy routes only `/monitor/` to the hub pool, uses weighted round-robin for new
browser sessions and a cookie to retain the selected healthy hub. The UI VIP owner
only provides the frontend: it does not own monitoring data, and the selected hub
may be another node. A failed hub is removed
from the pool; monitoring sessions switch to another independent hub on failure.

With `desk_monitoring_sync_enabled`, all three hubs share user preferences,
accounts, system definitions, alerts, quiet hours, network-monitor definitions,
persistent connection tokens and PocketBase application settings (including SMTP
and backup policy). Native authentication signing keys, per-agent fingerprints,
metric samples, live status, logs and backup archives stay local. Existing record
IDs are mapped per hub so system histories remain attached to their original IDs.
Do not copy a live PocketBase database or put it on shared storage.

The shared configuration boundary is separate from native Beszel. `/monitor/`
keeps the same sticky hub selection; configuration mutations enter that hub's
replication boundary. Any hub may propose an update. Two of the three replicas
must durably accept the revision and apply it through native PocketBase APIs
before Save is acknowledged. A disconnected replica catches up when it returns.
A single remaining hub can show its last committed configuration and metrics,
but shared writes need two replicas. Replication does not create a new VIP or
make the Settings VIP owner a monitoring leader.

The protocol keeps one encrypted, bounded checkpoint with the latest committed
configuration, ballot and accepted proposal. Promise/accept/checkpoint writes
happen on configuration changes only; idle polling every five seconds does not
write this file. No NATS/JetStream stream is created and no metric history enters
this checkpoint. Private peer traffic uses pinned TLS, HMAC envelopes and replay
checks. API bodies, passwords, tokens and notification URLs are not logged.

## Deployment

This is explicitly opt-in: `site.yml` does not install monitoring.
The inventory currently enables `xiaozhi_ingress_monitoring_enabled` so a normal
ingress rerun preserves the route. That flag only adds the HTTP route and hub
checks; it does not install or start Beszel, and unhealthy monitoring backends do
not affect the Settings or MQTT pools.

From the cluster repository:

```bash
python3 scripts/create-monitoring-secret.py
ansible-playbook -i inventory.ini monitoring.yml --syntax-check
ansible-playbook -i inventory.ini monitoring.yml --check
ansible-playbook -i inventory.ini monitoring.yml
```

The generated credentials are in ignored `secret/desk-monitoring.json`, mode 600.
Do not send passwords in chat or commit this file. Bootstrap uses private local
HTTP, never resets existing passwords and rejects agent membership drift.
Native auto-login lets verification select each hub's existing admin without
using the bootstrap password. With auto-login disabled, verification falls back
to the ignored credentials; those passwords must then match on all three hubs.

Preflight freezes existing service states/PIDs/configuration, checks the current
Cloud VIP against the installed ingress snapshot, and rejects occupied trial
ports. Hubs install serially; all public keys are collected before agents start.
All three systems must be up with recent samples in every hub before opening
the public route. The full HTTP+MQTT HAProxy candidate is validated before atomic
installation and graceful reload, with restoration on reload failure. Core,
workers, gateways, DNS and VRRP are not restarted by this playbook. DNS node 2's
intentionally withdrawn VRRP state is preserved.

## Shared settings rollout

For existing three-node hubs:

```bash
ansible-playbook -i inventory.ini monitoring-sync.yml --syntax-check
ansible-playbook -i inventory.ini monitoring-sync.yml
```

Inventory enables shared settings and sets `desk_monitoring_sync_source` to
`deskb1x`. This selects the initial snapshot once, preserving node 1's changed
administrator credentials; it is not a permanent leader. Ignored controller
files `secret/desk-monitoring-sync.json`, `desk-monitoring-seed.fernet` and the
pinned peer certificate/key retain this bootstrap material. Reruns reuse it
rather than rotate passwords or recreate an initial snapshot.

Each existing hub retains a small encrypted pre-sync configuration backup.
Initial migration stops only that hub when needed to align the existing Beszel
and PocketBase administrator email/password hashes. It preserves account IDs,
history and other tables, rotates affected local token keys, and has an encrypted
auth-only rollback. Initial raw passwords are never recovered or printed.
Subsequent password edits use PocketBase validation and hooks on every replica,
with the requested password encrypted inside the configuration checkpoint.

The Ansible-managed auto-login administrator remains `admin@desk.cluster`.
The boundary rejects deletion, demotion or email changes that would remove that
identity; name/preferences/password changes and additional accounts can sync.
Raw collection-schema edits, OAuth/MFA collection policy changes and avatar/file
uploads are outside this boundary. Native one-shot tests, realtime registration,
login and local backup operations remain local. Temporary one-hour connection
tokens remain local; permanent tokens and explicit disable operations sync.
After failover, refresh the page before editing an old record ID or reauthenticate
a PocketBase admin session; native IDs and signing keys remain local.

The config service and private UI proxy use `desk-monitorconfig.slice`, separate
from sampling: 10% of one CPU, low CPU/IO weights, 64 MiB memory high threshold,
96 MiB maximum. Individual limits are 64/24 MiB. Their ports 8091 (private UI),
8092 (config writer) and 8094 (TLS peer RPC) bind only the existing private
addresses, with exact cluster-peer ACLs. Public access still uses only
`192.168.1.186/monitor/`. Check state without revealing config/credentials:

```bash
curl -fsS http://192.168.1.186/monitor/api/desk-config/status
```

Crypto wheels are checksum-pinned and unpacked into a dedicated vendor directory
for the installed Debian 12/Python 3.11 ARM64 runtime. No system Python or Xiaozhi
virtual environment is modified. The regular `monitoring.yml` also preserves and
installs shared settings before exposing its proxy when the flag is enabled.

## Scope and overhead

Each node has a separate `desk-monitoring.slice`, CPU quota 25% of one CPU,
CPU/IO weights 5, memory high threshold 192 MiB, maximum 256 MiB and 96 tasks.
Hub/agent memory maxima are 192/64 MiB. These are ceilings, not measured usage.
UI activity increases sampling load. Background hub updates default to around
60 seconds in this pinned release; an open detail page requests live data.
No synthetic voice/provider calls or paid API calls are used to test monitoring.

The agent reads host CPU/RAM/load, disks, network and available temperature
sensors, plus service metrics matching `xiaozhi*`, `desk-*`, `beszel*`,
`nats-server*`, `haproxy*` and `keepalived*`. Journal access is read-only through
the systemd-journal group and logs use the selected Beszel user permissions. Trusted users who can
view a system can also view its allowed service logs. Docker/GPU collection and
container registry/image update checks are disabled. No DNS request forwarding,
AdGuard policy, upstream resolver, firmware, panel or provider settings change.

Beszel's network bandwidth is not DNS QPS, and a future DNS probe latency is not
the measured latency of real queries. This trial does not add DNS/application
custom metrics, ASR/LLM/TTS turn timelines or alert delivery channels.

## Trial verification (2026-10-11)

Deployment completed on all three nodes with no failed tasks. Every hub had all
three systems up with recent samples. Each node's HAProxy reported all three
monitoring backends and all three Settings backends UP. The VIP served the HTML,
JavaScript and CSS, and each hub was selectable through the sticky cookie.
The browser showed live metrics and service counts for all three nodes.
Protected service PIDs/states and non-monitoring configuration matched preflight;
Settings health/page remained HTTP 200. Existing VIP ownership was unchanged.

A 70.8-second read-only sample, without synthetic workload, measured combined
hub + agent CPU at 0.43%, 0.32% and 0.32% of one CPU on nodes 1/2/3. Combined
process RSS was approximately 45.5, 44.0 and 44.7 MiB respectively. These are
short trial measurements, not peak limits or a voice-latency benchmark.

Five monitoring safety tests, six ingress peer-ARP tests and four MQTT ingress
stats tests passed. YAML/Python/template checks, Ansible syntax/check mode,
HAProxy candidate validation with the existing MQTT fragment and
`git diff --check` passed. No physical power-loss test was performed.

## Shared-config verification (2026-10-12)

The initial source was `deskb1x`: its changed Beszel password and PocketBase
administrator password hashes were retained and aligned on the other two hubs.
Read-only checks confirmed the seed hashes on all three hubs, preserved native
administrator/system IDs and existing metric history, and three systems up on
every hub. The temporary test account was removed.

Real API checks through the VIP passed for preferences saved from nodes 1, 2
and 3; a save with node 3's config service stopped was accepted by the other two,
and node 3 caught up when restarted. Temporary account creation, password
rotation from node 2 and native password authentication on all three hubs passed.
The PocketBase console's collection-ID route also replicated an administrator
edit; its original value was restored. These checks did not change the retained
administrator password. No physical power-loss test was performed.

Native batch transactions need at least 30 seconds for CPU-capped password
hashing, rather than PocketBase's initial 3-second deadline. The shared boundary
keeps that minimum when settings are saved; CPU and memory ceilings are unchanged.

A 20-second idle observation measured config-process CPU at 1.52%, 1.60% and
1.55% of one CPU on nodes 1/2/3, with service memory at 21.0, 20.2 and 19.5 MiB.
All three encrypted checkpoints were 6,796 bytes. Their modification times and
process disk-write counters were unchanged: no idle checkpoint writes. This is
a short observation, not a peak measurement. DNS, Xiaozhi and monitoring URLs
remained HTTP 200; the full deployment also passed protected-service guards.

All 36 monitoring tests, template/YAML/Python validation, both playbook syntax
checks and `git diff --check` passed. Runtime config helpers matched the tested
source on all three nodes. Metrics never enter the encrypted config journal;
there is no JetStream dependency.

## Network-monitor record identity recovery

Beszel 0.21.0 derives network-monitor record IDs from the native system ID and
monitor configuration. Create hooks replace a supplied ID; changing the target,
protocol, TCP port or DNS server can replace the native record. Shared identities
remain stable, but each hub must remap to its final native ID after applying a
batch. Missing mappings recover an existing monitor by its system/configuration
before planning a create. Duplicate monitor configurations are rejected before
commit, and irrelevant port/server fields follow Beszel's normalization.

This fixes the Save failure where all hubs remained at applied revision 20 while
revision 22 retried a duplicate create, making every monitoring backend unready.
The recovered revision applied with zero native record changes on all three hubs;
subsequent live edits converged to revision 24. A same-value network-monitor Save
through each VIP-selected hub returned HTTP 200 and coverage 3/3. Regression tests
cover native ID changes during create/update, deletion, missing-ID recovery and
duplicate rejection. Retained administrator passwords were not changed.

## Removal

Keep the ignored credentials/data if rollback may be required. Set the inventory
flag `xiaozhi_ingress_monitoring_enabled: false`, remove the marked monitoring
blocks from the installed HAProxy candidate, validate it with the existing MQTT
fragment and gracefully reload HAProxy. When shared settings are enabled, also stop/disable
`beszel-config-sync` and `beszel-ui-proxy`; retain their encrypted rollback/state
if recovery is needed. Stop/disable **only** `beszel-hub` and
`beszel-agent` on all three nodes. No VIP movement is required.

## Current LAN auto-login

The inventory now enables native `AUTO_LOGIN` for each hub's existing admin at the
user's request. No manual login is required, including after hub failover.
The original ignored bootstrap credentials remain available for provisioning;
histories remain local and preferences now sync when enabled. See [Unified UI](DESK_UI.md).

All three existing admin accounts and new bootstrap accounts use
`admin@desk.cluster`. `monitoring-accounts.yml` migrates existing emails serially
with a private backup and rollback; it retains each account's ID, password,
token key, settings and history. Monitoring reruns also converge this email
before enabling auto-login. The email migration itself does not replicate settings or histories; the separate
shared configuration boundary above handles settings.

```bash
ansible-playbook -i inventory.ini monitoring-accounts.yml
```


## Configuration failure isolation (2026-10-12)

UI readiness now follows native Beszel health, independently of write quorum,
pending apply or native validation failure. A configuration error cannot remove
all otherwise healthy hubs from the management VIP. Peer status reads the journal
under a short lock; native API calls and password hashing occur outside it.

Apply records first, persist successful batch/mapping/password progress, then
apply application settings through their separate native API. Readback must match
the committed projection before acknowledging the revision. Failed settings
after a successful record batch are explicit partial application and retry from
that progress; they cannot falsely roll back over already changed records.
A caller whose own native apply failed never reads an old local record/settings
as the successful Save result, even if two other hubs applied.

Temporary-token enable removes the shared permanent token before enabling the
local ephemeral token. Shared disable/permanent changes invoke native token reset
on every hub, including its in-memory temporary-token map. Reset generations
are journaled, so an offline hub performs its reset once when catching up.
Ephemeral tokens themselves remain native local runtime state.

`/monitor/api/desk-config/status` exposes revision, native apply progress,
ready nodes, writability and pending/error state without credentials. The
`repair-ui-sync.yml` playbook installs these changes serially alongside the DNS
policy/UI repair. Existing accounts, passwords, record identities and metrics
are retained; no native hub restart is required.


Post-rollout same-value network-monitor Save succeeded through each of the
three VIP-selected hubs with HTTP 200 and coverage 3/3, at revision 25.
All hubs reported the same committed/applied revision and retained their three
existing monitors. The original administrator/password and native metrics were
preserved. Idle encrypted checkpoint metadata stayed unchanged on every node.

## Load preference for new sessions

With `desk_monitoring_load_aware_enabled`, the existing dashboard collector
provides a private TCP weight agent on port 8012, restricted to cluster peers.
It uses cached, fresh telemetry and native monitoring health; it does not probe
per request or access the HAProxy administrative socket. The local ingress
collector chooses the preferred hub. All three hubs remain running and healthy
sticky sessions stay on their selected hub.

The score is CPU percentage plus role costs: UI/Xiaozhi owner 20, DNS owner 10,
VPN owner 5, network collector 20, Home Assistant owner 20 and 30 for each active
ASR/LLM/TTS stage. Each owner beyond the first adds 100 points, so a node already
holding VPN and Network normally yields preference to a node holding only
Xiaozhi. Home Assistant starting, active and checkpointing count as ownership;
standby or stale Home Assistant metadata does not. Monitoring itself is not
counted as an owner, avoiding a preference feedback loop.
Offline, stale, unhealthy and maintenance nodes are excluded from preference.
An unavailable preferred hub is replaced immediately; switching between healthy
hubs requires an improvement of at least 15 points sustained for three minutes,
and three minutes since the previous selection.

HAProxy receives only weights: 100% for the preferred hub and 10% for each other
hub. This is a preference, not exclusive ownership. Native backend checks still
control availability. Missing or stale placement data returns neutral weights.
Existing cookies retain healthy sessions even when their hub is not preferred.
The dashboard displays **Monitoring preferred** on the choice reported by the
single current UI VIP owner; conflicting/missing owners suppress the badge.

Use `dashboard-helpers.yml` to build and roll out only a changed collector binary,
with a rollback copy and protected-service checks. It does not reload ingress or
restart the monitoring hubs.

After the 2026-10-17 collector-only rollout on all three nodes, the shared panel
reported `deskb2x` as preferred with VPN + Network on `deskb1x`, Xiaozhi on
`deskb2x`, and DNS + Home Assistant on `deskb3x`. The UI owner's weight agent
returned 10% / 100% / 10%. Of 24 requests without a session cookie through the
management VIP, 20 reached `deskb2x` and two reached each other healthy hub.
A request carrying the existing `deskb1x` cookie still reached `deskb1x`.
Protected voice/DNS/VIP service checks passed on every node.

An optional NetAlertX CPU canary is labelled **Network trial**, only while its
private frontend is reachable and its RAM marker is present. This does not mean
NetAlertX has HA or that it manages the modem's DHCP configuration.
