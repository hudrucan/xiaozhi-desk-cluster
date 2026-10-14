# WireGuard HA

Opt-in `vpn-ha.yml`; never included by `site.yml`. Management UI and resolver
routes are unchanged. User reserved 192.168.1.254 outside modem DHCP.
VPN endpoint is vpn.huynm.dev:51820; modem UDP mapping changes are user-owned.
All three nodes have the same server identity and peer list. Prepared kernel
tunnels exist on each node; only the floating address receives public VPN
traffic. The modem cutover to .254 was confirmed on iPhone 4G/5G; the transitional .103 endpoint is disabled.

Separate keepalived process/PIDs, VRID 53, randomized persisted base priorities
100/110/120 (no hard-coded primary). Xiaozhi/DNS owners each receive a 40-point
penalty. Healthy unoccupied nodes are preferred, while one remaining healthy
node may host all roles. 30-second preemption delay and rise/fall prevent rapid
churn; placement is independent of instantaneous CPU. Owner changes can require
client re-handshake and disrupt existing TCP connections/NAT state. This is
recovery HA, not synchronized transport keys or seamless failover.

Native bounded probe checks local management/tunnel link, gateway, WG public
identity/port/mark/expected AllowedIPs, forwarding and tunnel firewall gates.
The owner also verifies that marked WireGuard UDP replies use the VIP as source. An
active current-boot maintenance marker withdraws VPN eligibility and expires
with the existing lease; removal allows recovery. No new API poll/history writer
or JetStream. No voice/DNS process/config is changed for VPN deployment.

Full IPv4 LAN split tunnel remains 192.168.1.0/24 plus 10.77.0.1/32. No private
10.10.10.0/24/default/IPv6 route. Existing peer keeps 10.77.0.2/32. Source NAT uses
each node's management IP, so unchanged trusted-LAN web ACLs and LAN reply paths
work. Public IPv4 UDP accepts only the VPN VIP. Node-specific
runtime metadata is not shared secret state; server keys and peer list are common.
Owner notification maintains routing table 51853 and rule priority 11853 for
WireGuard mark 51853 only. Replies originate from .254 rather than a physical
node IP, so endpoint roaming cannot pin clients to the previous owner. A scoped
OUTPUT gate suppresses marked replies on standby nodes; the transitional node-3 legacy exception is disabled after WAN cutover. Main routing tables are untouched.

Peer updates are supplied once in ignored credentials, applied with Ansible to
all three nodes, and reloaded with wg syncconf to preserve existing sessions.
No browser CRUD/config sync service is added in this phase.

Run `ansible-playbook -i inventory.ini vpn-ha.yml`. Native helper builds locally
for ARM64; role readiness must pass on all nodes before any election starts.
Do not expose dashboard HTTP, DNS port 53 or SSH as modem port mappings.

Cloudflare A vpn.huynm.dev should be DNS only to the public IPv4. Creation is
separate from DDNS: no DNS API credential is stored on nodes. Automated DDNS
requires a dedicated token limited to this zone, never the existing developer
Cloudflare tunnel certificate. Public-IP changes require updating A, and clients
may need to toggle their tunnel to resolve the new endpoint.

## Validation on 2026-10-14

Native readiness/firewall regression tests and Ansible deployment passed on all
three nodes. The initial transport failover test exposed physical-IP endpoint
roaming; owner source routing and standby reply gating fixed the failure. The
retest used one temporary .3 peer and an isolated client without changing its
endpoint or toggling the tunnel. Encrypted dashboard HTTP and LAN SSH passed
on owners 1, 2 and 3. Observed recovery was 9.2 seconds from 1 to 2 and 18.7
seconds from 2 to 3, including SSH/polling overhead. These are measurements for
this test, not a guaranteed recovery bound. Election services were restored and
the temporary peer, namespace and RAM key file removed in finally.

LAN-side VIP failover passed, and the user subsequently confirmed iPhone
4G/5G access to both the dashboard and modem through .254 using vpn.huynm.dev. The Cloudflare DNS-only A record
was created as 171.248.168.14; automated DDNS remains pending.

## Dashboard and front panel

The native election probe publishes a small atomic RAM snapshot under
`/run/desk-vpn-vip/status.json`. The dashboard validates node identity, boot
identity and freshness; stopped or stale probes become unconfirmed. Local
readiness is not a WAN connectivity measurement. No keys, peer identities or
profile contents are exposed. The dashboard explicitly cannot read WireGuard
configuration or election credentials.

The root topology includes WireGuard, actual VPN ownership and local readiness;
node details show owner/standby and readiness. VPN owner changes join the cluster
timeline. The compact panel API includes additive VPN owner/readiness fields.
The FD6551 front panel rotates `tL NN` with existing roles on the actual .254
owner, with a single shared local address query per poll.

Maintenance/Reboot releases the VPN election along with the configured Xiaozhi
and DNS elections, verifies one healthy survivor owns each VIP, and restores
only election services that were running. Installed VPN metadata keeps the
VPN handoff requirement even if telemetry becomes unknown. No real reboot or
maintenance operation was triggered for this UI rollout.

## Dashboard telemetry rollout regression

The initial dashboard change omitted VPN from the telemetry component whitelist.
Local collectors continued to report healthy application components, but cluster
aggregation rejected their snapshots and displayed all nodes as unavailable.
The whitelist now accepts the bounded VPN states, and local `/health` validates
the collector's own snapshot before it can pass rollout readiness. Peer outages
still leave a healthy local dashboard available. Regression tests cover both
the rejected snapshot and the surviving-peer case.

After the hotfix, all three nodes were online, each VIP had one owner, and root,
Xiaozhi, DNS, monitoring, cluster status and compact panel API returned HTTP 200.
The user confirmed the physical node-1 display reads `tL01`. Combined-worker
activity icons are retained in group vars for subsequent panel-only rollouts.
