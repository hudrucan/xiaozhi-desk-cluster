# Cluster dashboard

The management VIP serves a read-only overview at `/`. Xiaozhi, DNS and Monitoring
retain `/xiaozhi/`, `/dns/` and `/monitor/`. `/api/cluster/status` is the same bounded
JSON contract a future front-mounted ESP32 display can consume.

For the future touch display, use the bounded, versioned
[Front-panel API](CLUSTER_PANEL_API.md) at `/api/cluster/panel`.

## Data and ownership

Three identical `desk-dashboard` services bind only to their existing private IPs
on port 8010. Only configured private peers may read their HTTP endpoints. The
management proxy exposes exactly `/api/cluster/status` and `/api/cluster/panel`; `/local` stays private.
No endpoint accepts writes or a caller-supplied upstream address.

Each collector samples every five seconds. Browser requests return cached data,
and browser polling pauses when the page is hidden. CPU comes from host `/proc/stat`
deltas (guest time is not counted twice, I/O wait is excluded from busy time), RAM
uses MemAvailable, and temperature uses existing thermal sysfs. CPU has no value
until the second sample. VIP ownership is checked against actual local interfaces
and the existing applied VIP metadata, never Cloud desired ownership.

MQTT/Core counts come from the existing local status endpoints. Worker activity
and DNS query rates reuse existing `/run` metadata, checked against node identity,
boot and freshness. Unknown/stale sources are shown as unknown, with no retained
ownership or busy indicators. Native AdGuard HTTP health is labelled responding;
it is not a claim that every external lookup succeeds or that fallback is inactive.

Cloud configuration, runtime apply readiness, DNS policy and Monitoring sync are
read every 30 seconds. Runtime revision uses the existing live runtime status API,
not a newly written bundle on disk. No models are loaded or hashed by this service.
The UI's Config summary means current desired Xiaozhi config is synced and both
worker/core runtime revisions match. Per-service configuration is in node details.

The current worker activity groups LLM and Vision under one compute counter;
the dashboard honestly labels it **LLM / VLM**. No segment number, RTF or token
latency is invented. Those fields need separate telemetry before being shown.

## Live topology

The root page shows two endpoints above the existing node cards. Solid lines
identify the currently observed Xiaozhi and DNS VIP owners; dashed green lines
show other eligible DNS backends according to fresh DNS-owner telemetry. Duplicate
owners are labelled conflicting and their ownership line is withheld. Missing,
stale or offline nodes lose ownership, eligibility and activity highlights.
Fallback is labelled explicitly and no AdGuard eligibility is retained.

Active ASR/LLM-VLM/TTS counters highlight the worker card. These counters do not
correlate requests, so the page does not invent a conversation route between
workers or infer per-query routing from pool eligibility. Brief jobs between
five-second samples may not appear. DNS probe details are a native disclosure
that retains its open state across polling. Node details, confirmation dialogs,
service links and recent events retain their existing behavior.

The map uses the existing status response and polling interval; no collector,
probe, daemon, runtime permission or server load is added. Wire positions are
measured from the actual cards on render/resize, and use a left gutter on mobile.
Only active worker icons animate, respecting reduced motion and pausing when
hidden. This is browser rendering, not a change to voice or DNS scheduling.

## Events

Observed node reachability, reboot, local VIP acquisition/release, service health
and configuration transitions are retained in bounded RAM rings. Peers exchange
these cached rings; aggregate snapshots deduplicate IDs and nearby duplicate node
reachability observations. At most 64 events from the last six hours are returned.
This is an observed event feed, not a complete durable audit log. Whole-cluster
restarts clear it; changes faster than the sample interval may not be observed.
Per-turn voice events stay in Xiaozhi voice logs. The root page shows the three
latest observation groups, with View all and Collapse/Expand controls. Groups
are native disclosures; expanded steps show timestamp, affected nodes, observed
before/after values and source. The timeline scrolls inside a bounded 340 px
region (320 px on mobile). Polling preserves open groups, focus and scroll;
View all/Show latest resets the internal scroll to the top.

Grouping is local to the browser: VIP changes within 90 seconds, and changes
for the same node or same service/revision within five minutes, form a group.
Matching peer observations within 15 seconds share a step with the observed
node list. A group is not proof of a shared root cause, and a recovery icon
reflects the last observation, not current health. Older event records without
structured context remain readable; no previous state is invented for them.

New event records add bounded optional `subject`, `from` and `to` fields. Same-
revision pending must persist for 30 seconds before it is logged; an actual new
revision or explicit failure is immediate. Native service unavailability must
persist for 15 seconds; brief dips and their recoveries stay quiet. Dashboard
health remains immediate; only event emission is delayed. Boot identity is kept
in a three-node RAM map so a reboot can be recognized across an unavailable
sample, without publishing boot IDs. No new probes, log scraping, disk history
or daemon are introduced. The compact panel v1 projection stays compatible.

Timeline acceptance on 2026-10-13: 17 Go checks and 8 dashboard/UI Python
checks passed. Isolated UI checks covered peer grouping, legacy event records,
disclosures, section Collapse/Expand, bounded internal scroll, focus/scroll
retention across polls, and a 390 px viewport without overflow. Serial collector
and page rollouts passed protected runtime/policy guards on all three nodes.
Live event context and the deployed timeline were verified through the VIP;
root, panel/status APIs and the three application routes returned HTTP 200.
Only the read-only collector was restarted; no voice/DNS/VIP service, firmware
or provider workload was changed. No physical outage was induced for this change.

## Deployment and isolation

```sh
ANSIBLE_LOCAL_TEMP=/private/tmp/desk-ui-ansible ANSIBLE_PIPELINING=1 \
  ansible-playbook -i inventory.ini -u ops desk-dashboard.yml
```

For page-only updates after the dashboard is installed, use
`ansible-playbook -i inventory.ini dashboard-ui.yml`. It uses the installed
public membership, retains each prior page, validates the existing proxy routes,
and checks protected runtime/policy before and after each node. It does not
build or restart the collector, node controller, DNS or voice services.

The dependency-free ARM64 binary is compiled on the controller, not the TV boxes.
It uses a single Go processor, low CPU weight and a 3% service CPU ceiling, with
48 MB maximum memory. These limits apply only to the dashboard; DNS and TTS have
no new request limits. The service cannot write the root filesystem. Its only capability is
CAP_DAC_READ_SEARCH, explicitly authorized to read worker-owned 0600 activity
metadata without changing the existing writer or restarting it. Credentials/config
bundle paths, /var/lib, /opt and home directories are inaccessible in its service
mount namespace. It reads fixed safe metadata paths and cached APIs; no credential
files are read.

Rollout starts only the new collectors and gracefully reloads HAProxy with both
HTTP and MQTT configurations validated together. Existing voice, DNS, monitoring,
VIP configuration, processes and policy are guarded against changes. Prior root
pages are retained for per-node rollback on failure. Existing UI/ingress playbooks
retain the dashboard route on later deployments. No modem or firmware changes.

## Validation

```sh
GO111MODULE=off go test ./roles/desk_dashboard/files
python -m unittest discover -s roles/desk_dashboard/tests -p 'test_*.py'
python -m unittest discover -s roles/desk_ui/tests -p 'test_*.py'
```

On older Go/Darwin linker combinations, use `go test -ldflags=-linkmode=external`.
Check the public overview, each node's details, `/api/cluster/status`, and the
three unchanged service links. Compare `desk-dashboard` CPUUsageNSec over a
sample interval and MemoryCurrent on each node before claiming any overhead.

Topology acceptance on 2026-10-12: existing dashboard/UI Python suites passed
(8 checks), rendered JavaScript passed syntax validation, and isolated browser
fixtures verified active TTS, failover, fallback, stale metadata and duplicate
owners. A 390 px viewport had no horizontal overflow; native DNS disclosure
remained open across polls. Node 3 canary and subsequent nodes 1/2 page-only
rollouts passed runtime/policy guards. Public root, Xiaozhi, DNS and Monitoring
returned HTTP 200; live ownership, three eligible DNS backends and node detail
controls were verified. No real voice workload or physical reboot was triggered
for this page change.

## Firmware Web Control link

The optional Xiaozhi Client card probes the configured private IPv4 HTTP endpoint
`/api/status/system` every 15 seconds, in parallel with other cached status reads.
It verifies version, device IP and uptime, and retains no SSID or other robot status
fields. Offline or stale health renders a disabled native button. It links directly
to the device only when healthy; no firmware action or camera capture is triggered.


## Confirmed node reboot

`desk_node_control_enabled` explicitly enables a separate `desk-node-control`
service on the private interface (8011). The collector remains GET-only and cannot
read `/etc/desk-node-control`. The new agent holds only a dedicated peer action
key, and implements one fixed action: `/usr/bin/systemctl reboot --no-block`.
There is no SSH or arbitrary command API. The service is root to ask PID1 to run
its ordinary orderly reboot sequence; it has no Linux capabilities.

The dashboard exposes Reboot node in Node details. Prepare and Confirm are
separate same-origin JSON POSTs. Confirm uses a signed 60-second challenge bound
to node identity and current boot ID. Two non-target nodes must durably hold the
same exclusive 10-minute operation lease. The target rechecks both surviving
nodes and persists the executed request ID before scheduling a reboot. Lost
acknowledgements do not trigger retries. Peer traffic uses HMAC, a timestamp and
one-use nonce on the private addresses; public HAProxy routes only the three exact
operation paths, never `/peer/`.

Fresh collector identity/boot, MQTT/core/worker readiness, all shared revisions,
NATS routes and an actual DNS backend health answer are required on all three
nodes before confirmation. Recovery reports accepted/offline/starting/rejoined,
with needs-attention after five minutes if boot/services are not confirmed. It
requires a new boot ID and all service/config checks. Successful recovery releases
the survivor leases and retains a small completed result for one hour on the
peers, so a second tab cannot consume the only recovery notification. Expired leases do not permit another reboot unless all three
nodes are healthy again and the user confirms a new operation. Small operation
state files are written only for explicit actions; no JetStream or metrics history.

The shared peer key is generated once in ignored `secret/desk-node-control.key`.
Never print or commit it. A reboot is never part of rollout verification. Offline
unit fixtures replace the reboot command. A user can test HA by confirming one
node, observing VIP migration, then waiting for Rejoined before another test.
Xiaozhi sessions on the rebooted core are interrupted and must reconnect; they
are not replayed or migrated. Power loss or a node that cannot boot needs manual
intervention. Xiaozhi VIP does not preempt on return; DNS follows its separate
health/priority policy.

