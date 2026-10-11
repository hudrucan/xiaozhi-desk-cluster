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
| deskb1x | 10.10.10.11:5354 | 192.168.1.101:5353 | 127.0.0.1:3001 |
| deskb2x | 10.10.10.12:5354 | 192.168.1.102:5353 | 127.0.0.1:3001 |
| deskb3x | 10.10.10.13:5354 | 192.168.1.103:5353 | 127.0.0.1:3001 |

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

`desk-adguard`, `desk-dnsdist`, `desk-dns-vip`, `desk-dns-activity`
and `desk-dns-peer-arp.timer` are
separate from every Xiaozhi service. DNS has a dedicated slice with total CPU
quota 50% of one CPU, low CPU/IO weights, 256 MiB memory pressure threshold and
320 MiB hard limit; individual services also have limits. This bounds resource
use but cannot prove zero voice-latency impact without a simultaneous load test.
The small resource budget may need measured tuning for larger filter lists.

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

Each AdGuard has its own authenticated UI, query log and statistics. The UI is
loopback-only; use a tunnel to the desired node, for example:

```bash
ssh -N -L 3001:127.0.0.1:3001 ops@192.168.1.101
# Open http://127.0.0.1:3001 on this computer.
```

Use `.102` or `.103` for another node, or different local ports for concurrent
tunnels. This foundation does not add a shared DNS dashboard to Xiaozhi Settings
or expose the dnsdist administrative API/console. Inspect dedicated service
logs with `journalctl -u desk-adguard -u desk-dnsdist -u desk-dns-vip`.

The base upstream/filter/rule policy is configured in Ansible. Editing an AdGuard
UI changes **only that node**, not the other two. Ordinary reruns retain those
local edits when the managed policy fingerprint is unchanged. Changing managed
policy variables, or passing `-e desk_dns_reconcile_policy=true`, replaces the
policy on all nodes after candidate validation. That replacement overwrites
local UI edits. Do not use UI edits as shared policy until a separate reviewed
synchronization design exists. Do not remove the synthetic health/filter/identity
records: verification depends on them.

AdGuard sees dnsdist's private source IP; query logs are split across the three
backends and do not identify the original LAN client. This iteration does not
claim shared logs, per-client policy or retained client identity. Query logs use
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

No service has been deployed or restarted during offline development. After the
separate DNS rollout, use `ansible-playbook -i inventory.ini panel.yml` to enable
the display on all three nodes and verify the physical panel. DNS role rollout
does not restart the existing Xiaozhi panel or other Xiaozhi services.

## Offline validation

From the repository root (no deskbox connections):

```bash
python3 -m unittest discover -s roles/desk_dns/tests -p 'test_*.py'
python3 -m unittest discover -s roles/xiaozhi_panel/tests -p 'test_*.py'
# Run with the Python interpreter belonging to your Ansible installation:
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
