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

## Owner-only Cloudflare DDNS

`vpn-ddns.yml` is a separate opt-in deployment. Create a dedicated Cloudflare
API token from the Edit zone DNS template, restricted to the `huynm.dev` zone.
Cloudflare DNS tokens cover a zone; they are not restricted to one record.
The updater itself pins the exact record ID, hostname, A type and DNS-only
state, and refuses other records. No developer tunnel certificate is deployed.

Run locally from the repository:

```bash
python3 scripts/setup-vpn-ddns-secret.py
ansible-playbook -i inventory.ini vpn-ddns.yml
```

The helper reads the token through a hidden prompt, validates access to the
existing DNS-only A, and writes only ignored `secret/vpn-ddns.json` with mode
0600. When available, it reads just the zone ID from the local cloudflared
certificate; alternatively supply `--zone-id`. Tokens are never printed.

All nodes receive the same dedicated credential through systemd LoadCredential.
The updater uses DynamicUser, an empty capability set and inaccessible secret
paths. Only a fresh, ready .254 owner calls external services. Every approximately
2 minutes (plus up to 10 seconds jitter), it compares two HTTPS IPv4 observations,
requires a global IPv4, and PATCHes only the existing A if content/TTL changed.
A fresh ready snapshot is required at start, and actual local VIP ownership
and boot identity are rechecked immediately before PATCH. The systemd health
copy is not treated as a continually refreshed file during the HTTP operation. Updates enforce
DNS-only and TTL 120 and are read back. Standby timers do not make HTTP requests.
Invalid IP, provider disagreement, redirects, API errors or ownership loss leave
the existing DNS record unchanged. DDNS has no dependency into VPN readiness or
VIP election, and has no application history database; only outcome lines enter the normal system journal. Logs contain outcome/error categories,
never tokens or peer keys. IP changes may still require toggling client tunnels
because WireGuard does not guarantee continuous endpoint DNS re-resolution.

Inspect outcomes with `systemctl status desk-vpn-ddns.timer` and
`journalctl -u desk-vpn-ddns -n 10`. Stopping the timer affects only DDNS.
The dedicated user-supplied token is deployed through systemd credentials.
DDNS timers are enabled on all three nodes. Owner readback reports unchanged,
standby nodes skip HTTP. No IP change is simulated on the real DNS record.

DDNS fake regression tests passed (12 cases, including startup snapshot lifetime). A credential-free canary on all
three nodes verified that the actual DynamicUser sandbox can read only its
systemd metadata copies and sees exactly node 1 as eligible; nodes 2/3 skip.
This credential-free canary made no external HTTP calls or DNS changes;
the subsequent live deployment was verified separately with the dedicated token.

The short DDNS process is capped at 20% of one CPU core (5% of a four-core
host), with Nice=15 and CPUWeight=1; it does not stay resident. A 5% one-core
cap stretched Python startup beyond the 20-second health freshness window on
these nodes, so it was raised for the bounded startup/TLS burst. The updater
reuses one TLS context and validates startup freshness only once.

## iPhone WAN handoff validation

With the iPhone tunnel kept on 4G/5G, node-1 VPN election was stopped temporarily.
Node 2 acquired .254, and its existing iPhone peer formed a new WAN handshake
and exchanged encrypted traffic without changing the client profile. Observed
handshake recovery was approximately 100 seconds, including polling overhead.
The user confirmed application access recovered but reported noticeable delay.
The original election was restored using finally; a separate 300-second remote
restore timer guarded against controller disconnection. No peer configuration,
Xiaozhi or DNS service was modified. This validates automatic WAN recovery,
not fast or seamless session failover.

A second unchanged-profile WAN test recovered in approximately 31 seconds.
Both runs restored the original election and its standby placement. Recovery
time varies; the VIP and handshake observations narrow the delay to the path
after ownership election, but do not prove whether router ARP/NAT delivery or
client session renewal dominates. No fast-failover guarantee is made. The
bounded packet-type capture was stopped and stored no packet payload or keys.

Live DDNS on node 1 reports unchanged after public-IP agreement and Cloudflare
record readback. Nodes 2 and 3 report standby. The observed updater process
used roughly 1.29 CPU seconds for an owner check and 0.76–0.78 CPU seconds for
a standby check, once per approximately two minutes. These are single-run
measurements, not a TTS contention benchmark. Dashboard and node-control
mount namespaces deny both /etc/desk-vpn-ddns and /run/credentials.
