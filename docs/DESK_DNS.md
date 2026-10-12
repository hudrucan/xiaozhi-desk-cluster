# Three-node Desk DNS foundation

This is an opt-in AdGuard Home + dnsdist stack. `dns.yml` is deliberately absent
from `site.yml`. It does not change router DHCP/DNS, `/etc/resolv.conf`, Xiaozhi
Cloud Config, NATS, MQTT, provider runtimes or the Settings VIP service.
Panel support is a separate source change and uses the existing opt-in `panel.yml`.
No ISP fallback is implemented. Keep the router's current DNS until the separate
deployment, query, failover and Xiaozhi latency checks have passed.

## Traffic and fairness

LAN clients use **192.168.1.250:53** (UDP and TCP). All three nodes run dnsdist;
a separate unicast VRRP instance puts that address on exactly one healthy node.
That frontend sends queries round-robin to equally weighted AdGuard backends:

| Node | Backend | Local frontend probe | AdGuard UI |
| --- | --- | --- | --- |
| deskb1x | 10.10.10.11:5443 (TLS DoH) | 192.168.1.101:5353 | 127.0.0.1:3001 |
| deskb2x | 10.10.10.12:5443 (TLS DoH) | 192.168.1.102:5353 | 127.0.0.1:3001 |
| deskb3x | 10.10.10.13:5443 (TLS DoH) | 192.168.1.103:5353 | 127.0.0.1:3001 |

VIP ownership is independent of backend selection: the owner also remains an
equal backend. There is no node-1-first DNS policy. Initial VRRP ranks 100/110/120
are randomly assigned once and persisted identically on all nodes. Healthy
backup nodes retain those ranks on reruns. This provides a stable tie-break,
not automatic round-robin VIP movement. Backend health removes unavailable nodes.

The node currently holding the **deployed Xiaozhi Settings VIP** receives a
50-point DNS VRRP penalty. Any healthy non-Xiaozhi owner then outranks it;
preemption with a delay allows separation after either VIP changes hands. When
only the Xiaozhi owner remains eligible it may hold both VIPs. The script reads
`/etc/xiaozhi-ingress.json`; DNS does not own or pin the Xiaozhi VIP address and
does not restart its Keepalived process. Detection/election takes several
seconds; avoidance is a preference, not an instantaneous exclusion.

The DNS instance has its own configuration, service, PID directory and VRID 52.
Owner-only GARP and peer ARP refreshes mirror the existing management-Wi-Fi
workaround with separate DNS files/timer. They announce only the DNS VIP.
Ansible owns the DNS VIP selected for this experiment. Changing it after initial
deployment requires a separate migration; a rerun rejects changes to existing
VIP, ports and membership.

## Isolation and rollout

AdGuard Home is pinned to **v0.107.79**, schema 34, with the official ARM64 archive
SHA256 `3f7893c18e8aaadc456d0452839190561c306ca95175a2254958be80a769c1ae`.
dnsdist uses an exact Debian package version frozen across all three nodes in
preflight. An already installed version is reused; otherwise the cached apt
candidate is selected. Mixed versions or missing package metadata stop rollout.
Read-only apt simulation rejects removal or upgrades of existing packages.
The playbook does not refresh apt metadata or upgrade shared Keepalived;
needrestart is kept in report-only mode for this package installation.
Package installation suppresses the vendor dnsdist service startup; only the
custom `desk-dnsdist` unit is activated. An already active vendor dnsdist service
is rejected in preflight; the unused vendor unit is disabled for subsequent boots.

`desk-adguard`, `desk-dnsdist`, `desk-dns-vip`, `desk-dns-activity`, `desk-dns-ui`
and `desk-dns-peer-arp.timer` are
separate from every Xiaozhi service. DNS has a dedicated slice with total CPU
quota 100% of one CPU (one of the four CPUs), low CPU/IO weights, 512 MiB memory pressure threshold and
768 MiB hard limit. AdGuard has a 320 MiB Go memory target, 384 MiB pressure
threshold and 512 MiB hard limit; individual services also have limits. This bounds resource
use but cannot prove zero voice-latency impact without a simultaneous load test.
The earlier half-CPU ceiling caused DNS/VRRP health timeouts during native
filter rebuilds. Large OISD lists also exceeded the old 160 MiB Go target and
memory pressure thresholds, causing continuous GC/reclaim, HTTP timeouts and
all DNS VIP candidates entering FAULT. The larger bounded memory budgets give
these rebuilds headroom while preserving the one-CPU ceiling and low weights.
`ansible-playbook -i inventory.ini dns-resources.yml` updates only resource units,
retains mutable UI policy, restarts AdGuard serially and waits for backend health
before advancing. Larger filter lists still need measured tuning.

`dns-resilience.yml` additionally moves only `desk-dnsdist` and `desk-dns-vip`
to the sibling `desk-dnsfront.slice` (50% CPU, 128 MiB pressure threshold,
192 MiB hard limit). AdGuard rebuilds cannot consume that slice's CPU or RAM.
The total DNS serving ceilings are 1.5 CPU cores and 960 MiB; policy sync has its
own existing 20% CPU / 128 MiB slice. These are ceilings, not reservations.
VIP readiness queries `frontend.desk-dns.test` over UDP and TCP, answered by
dnsdist itself. Deployment verification still requires native filtering and all
three AdGuard identities; frontend liveness does not claim that filtering is up.

The unified DNS UI uses a frontend-only health probe and a 180-second DNS
backend timeout; a local filter compilation no longer marks the entire UI
proxy unavailable. The last rollout phase validates the combined HTTP/MQTT
HAProxy configuration and gracefully reloads only its DNS UI backend route.

The deployed emergency pool is `1.1.1.1`. Native AdGuard servers stay in the
default round-robin pool; the emergency pool is selected only when dnsdist has
marked every native backend down. With 2-second health checks and two failures,
detection takes about 2–4 seconds, plus request/transport timing. Three successful
native checks are required before returning to filtered DNS. Emergency queries
bypass filtering and do not appear in AdGuard statistics. Both IPv4 and IPv6 LAN
clients use this same rule. A live frontend keeps its DNS VIP throughout backend
failure. If all three boxes lose power, no cluster frontend remains to provide
fallback; this does not configure a modem or an external always-on resolver.

Filter writes through the unified UI and non-converged policy pulls acquire a
single maintenance lease from the current DNS VIP owner. At least one other
native TLS DoH backend must respond before a lease is granted. Successful work
releases the lease after native DNS health; uncertain timeouts retain it until
its 180-second expiry. A new VIP owner waits 180 seconds before granting work,
so in-flight operations from a previous owner can finish. During this cooldown,
filter edits return HTTP 409 and can be retried; other UI reads remain available.
Native background refresh is disabled (its interval displays zero in AdGuard).
A shared owner-only timer checks every five minutes and refreshes all enabled
block/allow lists serially once per day, retaining current filters and retrying
later if maintenance is busy. This serialization applies to the managed UI and
sync paths; direct changes to AdGuard outside them bypass the guard.

Apply to the already-installed three-node stack:

```bash
ansible-playbook -i inventory.ini dns-resilience.yml
```

The playbook preserves mutable policy, updates one node at a time, validates
native filtering after each frontend change, and checks frozen Xiaozhi processes
and configuration. The isolated test `roles/desk_dns/tests/integration_dns_fallback.py`
starts a temporary loopback-only dnsdist process: native filtering, all native
servers down, one native server recovered, and both pools down. It does not stop
or modify production services.


Preflight is read-only across all three nodes. It checks ARM64/systemd, reserved
port collisions, management interface, separate VIP/VRID, existing nonlocal bind,
package availability and agreeing persisted elections. It freezes Xiaozhi service
PIDs/configuration hashes and resolver state. A guard checks those snapshots
before each serial phase and again at completion. A drift or failed phase stops
the run. No automatic rollback of already completed DNS phases is claimed.

The phases are: three private AdGuard backends -> three verified dnsdist
frontends -> independent DNS VIP -> exactly one owner separated from the Xiaozhi VIP and LAN UDP/TCP queries
while all three nodes are healthy.
DNS Keepalived is started/restarted only after all three backends and frontends
pass. At runtime its health check needs a working frontend with at least one
AdGuard backend; temporary loss of one backend does not withdraw a usable VIP.
Health probes use synthetic local DNS names and do not require internet access.
Final verification also checks each node's backend identity and filtering.
It does not prove external upstream availability or a successful power-loss
failover. DNS queries in flight during a failure can require client retries.

`--check` runs only read-only preflight/guards. It skips directory/user creation,
downloads, package installation, candidate validation requiring new binaries and
all service changes. It is not an actual service/configuration acceptance test.

## UI, policy and monitoring

Open `http://192.168.1.250:3001/` for the authenticated UI on the current DNS
VIP owner. `desk-dns-ui` is a separate unprivileged HAProxy process restricted
to the management LAN, with a bounded DNS-slice budget. It never changes or
reloads the Xiaozhi HAProxy service. The backend remains loopback-only. UI failure
does not withdraw otherwise healthy DNS. A VIP failover may require signing in
again because login sessions and statistics remain node-local.

The optional `dns-sync.yml` shares policy from the current DNS VIP owner.
Query logs, statistics and caches remain per node. For a specific node, use an
SSH tunnel, for example:

```bash
ssh -N -L 3001:127.0.0.1:3001 ops@192.168.1.101
# Open http://127.0.0.1:3001 on this computer.
```

Use `.102` or `.103` for another node, or different local ports for concurrent
tunnels. This foundation does not add a shared DNS dashboard to Xiaozhi Settings
or expose the dnsdist administrative API/console. Inspect dedicated service
logs with `journalctl -u desk-adguard -u desk-dnsdist -u desk-dns-vip`.

The base policy comes from Ansible until policy synchronization is enabled.
With sync enabled, edit shared policy through the DNS VIP UI. Every non-owner
pulls from the current VIP owner; edits through a backup node's SSH tunnel are
overwritten by the next successful pull. Ordinary `dns.yml` reruns retain UI
edits when the base-policy fingerprint is unchanged. Changing managed policy
variables or passing `-e desk_dns_reconcile_policy=true` intentionally replaces
UI policy. The automation login is retained even in those base candidates.
Do not remove the synthetic health/filter/identity records.

Production forwarding uses private TLS DoH at 5443 and trusted original-client
headers, preserving real LAN client IPs. The original private port 5354 remains
available for strict backend probes. `/dns/` aggregates the three backends'
statistics, clients and query logs; entries retain backend provenance in the API. Query logs use
a 1000-entry memory buffer with file logging disabled; statistics retain one day.
AdGuard caches are independent. DNS frontend caching is disabled so queries
actually reach the three equal backends.

## Preparing a later deployment

Reserve `192.168.1.250` outside DHCP leases first. Keep the current router DNS.
Generate an AdGuard-compatible bcrypt admin password hash locally (for example
with `htpasswd -nB admin`, entering the password interactively), then put only
the portion after `admin:` into ignored `secret/desk-dns.yml`:

```yaml
desk_dns_admin_password_hash: '<bcrypt hash, not a plaintext password>'
```

Use permissions 600 or encrypt that file with Ansible Vault. Do not commit it.
When explicitly ready for live deployment, from the repository root:

```bash
ansible-playbook -i inventory.ini dns.yml --check -e @secret/desk-dns.yml
ansible-playbook -i inventory.ini dns.yml -e @secret/desk-dns.yml
ansible-playbook -i inventory.ini verify-dns.yml
dig @192.168.1.250 example.com
dig +tcp @192.168.1.250 example.com
```

`verify-dns.yml` is read-only and needs no password hash. Before changing router
DNS, separately test stopping **only** the current owner's `desk-dns-vip`
service, verify another owner and DNS answers, restore it, and measure voice
latency during DNS traffic. Do not stop the Xiaozhi `keepalived` service for a
DNS-only test. No automatic disruptive failover test runs in these playbooks.
If later advertised by DHCP, DNS1 can be `.250`; DNS2 can be omitted. Repeating
`.250` as DNS2 offers no independent endpoint. No router change is performed
by these files.

## Panel ownership and DNS activity

The optional panel reader displays **`dn 02`** only when the DNS VIP from
`/etc/desk-dns/vip.json` is actually assigned to that node. Existing `nt`, `Co`
and `iP` keep their meanings; multiple role labels rotate every 1.5 seconds.
Node digits stay fixed. A node without roles still shows `-- 01`.

The colon is independent of role labels and pause/play/clock. Fresh client DNS
activity on a backend triggers a 150 ms pulse, capped at two pulses per second
with bursts coalesced. All three backends can show activity, including nodes
without the DNS VIP. Internal health/identity/filter probes must not count.

The reader accepts only bounded `desk-dns-activity-v1` metadata at
`/run/desk-dns/activity.json`: local node ID, process instance, boot ID,
monotonic publication time, cumulative query count and last query time. It
rejects wrong identities, stale snapshots and malformed counters. Missing
telemetry leaves the colon off without changing DNS or other panel indicators.
No query contents are needed by the panel.

`desk-dns-activity` is a separate passive collector with `CAP_NET_RAW`. A
locked socket filter delivers only copies of IPv4 UDP/TCP packets addressed to
this node's private AdGuard port 5354; no promiscuous mode is enabled. It ignores
internal probes and writes only activity metadata, never names or packet logs.
It receives packets only and has no DNS forwarding or response role. DNS services
do not depend on the collector and keep operating if it fails.

The collector has a 5% CPU quota of one CPU, 32 MiB memory limit, bounded packet
buffer and TCP tracking inside the existing DNS slice. At high load it may miss
activity indications; these limits do not limit client DNS queries. Fragmented,
oversize or out-of-order packets may also be omitted from this best-effort
indicator. It is not a DNS accounting or successful-response metric. The
collector starts after backend verification and restarts only on its script/unit
changes; check mode skips it. The panel reads local files without DNS requests.

After the separate DNS rollout, enable the display with the opt-in panel role.
On the existing combined voice cluster, preserve its worker activity reader:

```bash
ansible-playbook -i inventory.ini panel.yml -e '{"xiaozhi_panel_worker_activity_enabled": true}'
```

This adds `dn`/colon without removing ASR/LLM/TTS icons. Verify the physical
panel separately. DNS role rollout does not restart the existing Xiaozhi panel
or other Xiaozhi services; `panel.yml` restarts only the single panel writer.

## Offline validation

From the repository root (no deskbox connections), use the Python interpreter
belonging to your Ansible installation for the tests and renderer; they use
its existing PyYAML/Jinja2 dependencies:

```bash
python3 -m unittest discover -s roles/desk_dns/tests -p 'test_*.py'
python3 -m unittest discover -s roles/xiaozhi_panel/tests -p 'test_*.py'
python3 roles/desk_dns/tests/render_fixtures.py
ANSIBLE_ROLES_PATH=roles ansible-playbook -i roles/desk_dns/tests/local_inventory.ini roles/desk_dns/tests/freeze_fixture.yml
ansible-playbook -i inventory.ini dns.yml --syntax-check
ansible-playbook -i inventory.ini verify-dns.yml --syntax-check
git diff --check
```

The renderer needs Ansible's existing Jinja2/PyYAML dependencies. The three
fixture hosts are local aliases; the freeze fixture executes only controller
metadata/election operations, never Linux service or network commands. These
checks do not execute ARM64 AdGuard, Linux dnsdist or Keepalived candidate
validators. Those validators remain mandatory before activation on real nodes.

## References

- [AdGuard Home release](https://github.com/AdguardTeam/AdGuardHome/releases/tag/v0.107.79)
- [AdGuard configuration](https://github.com/AdguardTeam/AdGuardHome/wiki/Configuration)
- [dnsdist selection policies](https://www.dnsdist.org/guides/serverselection.html)
- [dnsdist backend health](https://www.dnsdist.org/guides/downstreams.html)
- [dnsdist source address handling](https://www.dnsdist.org/advanced/passing-source-address.html)

## Runtime file naming

The managed configuration is `/var/lib/desk-adguard/adguard-home.yaml`.
An existing `AdGuardHome.yaml` is moved only while that node's AdGuard is stopped,
preserving local UI policy and credentials. The unit explicitly selects the new
name with `-c`. Conflicting old/new files
stop migration rather than overwriting either. Subsequent applies use only the
kebab-case configuration path.

Managed runtime executables are real files, not symlinks:
`/opt/desk-adguard/v0.107.79/adguard-home` and
`/opt/desk-adguard-sync/0.9.3/adguard-home-sync`. Installation copies the
byte-identical executables out of the checksum-pinned vendor payloads. Original
archive member names stay in the extracted payload for repeatable installation
and rollback; services and candidate validators use the managed names.
Runtime DNS scripts also use kebab-case (`dns-probe.py`, `peer-arp.py`,
`dns-activity.py`, `desk-dns-sync.py`, `dns-sync-account.py`), and the sync
configuration is `/etc/desk-dns/sync/adguard-home-sync.yaml`. Source Python
modules retain snake_case. Naming changes do not replace mutable AdGuard policy
or credentials and do not change the election.

## Shared policy synchronization

`dns-sync.yml` is a separate opt-in deployment, absent from `site.yml`. It uses
the official **bakito/adguardhome-sync v0.9.3** ARM64 archive, pinned to SHA256
`cb1909ad9f4516cf16e3a06e195f4094c29aa97a1a9418a8edfae352d869098c`.
There is no fixed source node: all three run the same timer. A VIP owner skips
pulling; each backup reads the current VIP over an authenticated, pinned TLS
endpoint and applies through its own loopback API. Ownership is checked at
each source request and replica write; a receiver promoted to owner refuses
the old pull. No sync process participates in DNS query forwarding. The API boundary logs in
once to local AdGuard and reuses its in-memory session cookie, refreshing on
authentication rejection. This avoids expensive bcrypt verification on every
metadata request; no human browser session is reused.

The default interval is 30 seconds after the preceding run finishes, plus up
to two seconds jitter. Unchanged policy skips the upstream process entirely.
The initial pull gets three bounded retries before deployment fails; the timer
is enabled only after that succeeds. Failed/offline replicas retain local state
and retry. Sync is eventual and not
transactional: filter downloads may take longer; a failed run can apply some
policy fields before retrying. A save followed by immediate owner power loss
may lose a change that has not reached another node. Let replicas converge
before deliberate failover. This does not claim quorum storage or multi-writer
conflict merging, and assumes the DNS VRRP has exactly one owner.

Shared fields: API-portable upstream/bootstrap/fallback and DNS options,
protection, safe search/browsing/parental settings, filter subscriptions,
allowlists, custom rules, user rewrites, blocked-service schedules and persistent
client settings. Client policy still has the existing dnsdist source-identity
limitation described above. Files referenced by `upstream_dns_file` are rejected
as non-portable; enter upstreams in the UI instead.

Node-specific bindings, ports, DNS access lists, DHCP, TLS listeners, admin
accounts/passwords, query log/statistics records, caches and UI
theme are excluded. Query-log/statistics settings are now shared. The small API boundary omits reserved health/identity
rewrites from both sides of upstream reconciliation and rejects changes to
them. Rewrite processing remains enabled, and the internal filtering probe rule
is retained. Global disabling of protection/filtering still conflicts with the
existing strict filtering health probes; their checks and election weights
are unchanged. The two VRRP scripts run every five seconds with a four-second
timeout, rather than starting both every three seconds. On these ARM nodes
each Python probe consumes roughly 0.4 CPU seconds even before network waits;
the slower cadence leaves room in the existing serving CPU budget and avoids
false priority/health failures. Two failed health samples withdraw readiness;
this can take around ten seconds. Loss of VRRP advertisements on power loss
continues to use the existing one-second advertisement interval.

`desk-dns-sync-agent` exposes read-only authenticated TLS on DNS VIP port 3002,
restricted to the three management node addresses. Replica mutations are accepted
only on 127.0.0.1:3002. TLS verification is enabled; private trust/key material
is not committed. The dedicated `desk-sync` automation account is provisioned
serially while each AdGuard is stopped, preserving all existing users, password
hashes and UI policy. Candidates are validated; provisioning failure restores
the private rollback YAML. AdGuard does not support scoped user permissions:
the automation login is an admin-equivalent secret. Its random password and
bcrypt hash stay in ignored files and service-readable private configuration.
Changing the human admin password does not interrupt synchronization.

The sync units have no capabilities and run under a dedicated account in
`desk-dnssync.slice`, a sibling of the serving/VRRP `desk-dns.slice`. Its
aggregate quota is 20% of one CPU and 128 MiB; metadata work cannot consume
the serving slice's CPU budget and delay VRRP health checks. Sync neither modifies nor restarts Xiaozhi
services.
The source API allows at most eight concurrent metadata requests and bounds
policy bodies at 2 MiB. These bounds affect configuration synchronization only,
not DNS query volume. Larger custom policy bodies fail clearly and retain local
policy. The trusted self-signed certificate lasts 825 days; renew and deploy the
trust certificate/key together before expiry. No automatic rotation is implied.

Initialize the ignored automation material once on the controller (requires
existing `openssl` and `htpasswd`), then deploy:

```bash
python3 scripts/create-dns-sync-secret.py --vip 192.168.1.250
ansible-playbook -i inventory.ini dns-sync.yml --check
ansible-playbook -i inventory.ini dns-sync.yml -e @secret/desk-dns-sync.json
```

Initialization retains an existing complete credential set and never prints a
password. Back up the ignored credential/certificate files securely. Check mode
performs read-only preflight/guards and skips credentials, downloads, account
changes and all service activation. A normal rerun is idempotent.

Inspect a particular node without reading any secrets:

```bash
ssh ops@192.168.1.101 'sudo cat /var/lib/desk-dns-sync/status.json'
ssh ops@192.168.1.101 'sudo journalctl -u desk-dns-sync --since "10 minutes ago"'
```

Status is `origin`, `synced`, or `retry_pending`; a synced replica includes its
source node, timestamp and canonical policy hash. The wrapper verifies policy
after upstream exits, because a zero exit code alone does not guarantee API
success. Origin/replica ownership or policy drift during a run prevents reporting
success. Raw upstream output/error bodies are suppressed so upstream URLs or
client data cannot spill into systemd logs. For detailed diagnosis, inspect the
safe status and the AdGuard UI on the affected node.

Sync is not a dependency of AdGuard, dnsdist or DNS VIP health; DNS keeps serving
its last local policy if synchronization stops. Turning off a node's timer does
not affect DNS forwarding. No router DNS changes occur in this playbook.

Reference: [upstream sync documentation](https://github.com/bakito/adguardhome-sync/tree/v0.9.3).

## Current unified UI and local auth mode

Use the management VIP at `http://192.168.1.186/dns/` for the current DNS owner's
UI. The inventory explicitly disables local UI authentication on all three
AdGuard instances. Protected peer sync still requires its existing token/TLS
boundary. The legacy DNS-VIP UI port accepts only the three cluster proxies and rejects other LAN devices;
DNS traffic still uses `.250:53`. See [Unified UI](DESK_UI.md) for deployment.

## Unified real-client UI (2026-10-11)

`dns-cluster.yml` adds a restricted TLS-only backend boundary and a loopback UI
aggregator on each node. Run `python3 scripts/create-dns-backend-trust.py` first;
its key/certificate remain ignored and are never printed. Inventory opts into
client identity forwarding; full DNS reruns retain it. UI aggregation is outside
the DNS packet path. Settings health, MQTT, providers and Xiaozhi process/config
snapshots are unchanged by this rollout. The modem is changed only by the user.

Global statistics sum node counters and use query-count-weighted latency; top
lists merge the upstream API's bounded lists. Logs merge by timestamp with global
pagination. Partial read coverage is supplied in `X-Desk-DNS-Coverage`; failed
mutations return an error. Native client names are retained and known node names
come from public inventory. Unknown names remain IP-only; no names are invented.

Query-log/statistics saves use PUT and synchronize across all nodes. No-body Clear
and Reset requests omit Content-Type as required by pinned AdGuard. Internal
health/identity/filtering names are excluded from new logging/statistics. Existing
historical entries remain until a user-authorized Clear/Reset or normal expiry.
No client IP is hidden to disguise historical relay/probe traffic.

For DoH forwarding, panel activity reads the native `adguard_clients` backend
request counter through `show stat` on a local UNIX socket with CLI level `user`.
The observer joins `desk-dnsdist`; socket mode 660 and directory mode 0710 grant
only group read access and traversal. It stores no domains and is outside DNS
forwarding.
The plain-DNS packet observer remains available for the disabled identity mode.
IPv6 DNS shares the existing DNS VIP election; see `DESK_DNS_IPV6.md`. It is
deployed on all three nodes; no modem change is made by Ansible.

## Filter mutation response handling (2026-10-12)

AdGuard filter mutations do not all share the same response format. Add/remove,
set URL/config/rules can return plain text or an empty HTTP 200 response; refresh
returns JSON with its update count. The serial maintenance boundary ignores bodies
for the plain/empty contracts, parses JSON only for refresh, and releases the lease
after native backend health passes. This prevents a successful deletion from being
reported as HTTP 400 during response parsing and retaining an unnecessary lease.
Native HTTP errors retain their status through the boundary instead of becoming
generic HTTP 503; response bodies with possibly private URLs are not forwarded.

Restarting the maintenance coordinator retains its existing 180-second startup
cooldown, allowing any previous update lease to expire before another backend can
start a rebuild. DNS forwarding and filtering remain active during this window.
An update during maintenance/cooldown returns HTTP 409, distinct from a backend or
transport failure. The response fix restarts only the sync/API agent, leaving
AdGuard, dnsdist, VIP processes and existing lists unchanged. Fake API regressions
cover add/remove/refresh contracts and native HTTP 400/429/502 propagation.

After the startup cooldown, a real request through `186/dns/control/filtering/refresh`
with the confirmed-empty whitelist returned HTTP 200 and `updated: 0`. Existing
filter lists and their metadata were identical before/after. All three native DNS
backends were healthy, with both replicas synced to the current DNS VIP owner.
This checks the live maintenance/refresh transport without downloading or replacing
active lists; live add/delete of the user's lists was not performed by the agent.
