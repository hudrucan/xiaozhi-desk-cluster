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
HAProxy routes only `/monitor/` to the hub pool, uses round-robin for new browser
sessions and a cookie to retain the selected healthy hub. The Settings VIP owner
only provides the frontend: it does not own monitoring data, and the selected hub
may be another node. A failed hub is removed
from the pool; monitoring sessions switch to another independent hub on failure.

Histories, users, alert settings and browser preferences are **independent**.
The initial login credentials are the same across all hubs, but subsequent UI
changes are local. This trial does not replicate databases or promise a shared
historical timeline. Do not put a live PocketBase database on shared storage or
copy it between running hubs. Metadata polling through all three hubs costs more
than a single hub; measure this trial before choosing the long-term topology.

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

## Removal

Keep the ignored credentials/data if rollback may be required. Set the inventory
flag `xiaozhi_ingress_monitoring_enabled: false`, remove the marked monitoring
blocks from the installed HAProxy candidate, validate it with the existing MQTT
fragment and gracefully reload HAProxy. Stop/disable **only** `beszel-hub` and
`beszel-agent` on all three nodes. No VIP movement is required.

## Current LAN auto-login

The inventory now enables native `AUTO_LOGIN` for each hub's existing admin at the
user's request. No manual login is required, including after hub failover.
The original ignored bootstrap credentials remain available for provisioning;
history and preferences are still independent. See [Unified UI](DESK_UI.md).

All three existing admin accounts and new bootstrap accounts use
`admin@desk.cluster`. `monitoring-accounts.yml` migrates existing emails serially
with a private backup and rollback; it retains each account's ID, password,
token key, settings and history. Monitoring reruns also converge this email
before enabling auto-login. This does not replicate hub settings or histories.

```bash
ansible-playbook -i inventory.ini monitoring-accounts.yml
```
