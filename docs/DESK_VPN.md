# WireGuard HA

Opt-in `vpn-ha.yml`; never included by `site.yml`. Management UI and resolver
routes are unchanged. Reserve 192.168.1.254 outside modem DHCP.
VPN endpoint is vpn.huynm.dev:51820; modem UDP mapping changes are user-owned.
All three nodes have the same server identity and peer list. Prepared kernel
tunnels exist on each node; only the floating address receives public VPN
traffic. The legacy physical-node endpoint is disabled by the current defaults.

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
OUTPUT gate suppresses marked replies on standby nodes. The legacy physical-node
exception is disabled by default. Main routing tables are untouched.

Peer updates are supplied once in ignored credentials, applied with Ansible to
all three nodes, and reloaded with wg syncconf to preserve existing sessions.
The [VPN UI](DESK_VPN_UI.md) provides replicated peer management;
`vpn-ha.yml` preserves UI-managed membership.

Run `ansible-playbook -i inventory.ini vpn-ha.yml`. Native helper builds locally
for ARM64; role readiness must pass on all nodes before any election starts.
Do not expose dashboard HTTP, DNS port 53 or SSH as modem port mappings.

Cloudflare A vpn.huynm.dev should be DNS only to the public IPv4. Creation is
separate from the owner-only DDNS updater described below. Automated DDNS
requires a dedicated token limited to this zone, never the existing developer
Cloudflare tunnel certificate. Public-IP changes require updating A, and clients
may need to toggle their tunnel to resolve the new endpoint.

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
VPN handoff requirement even if telemetry becomes unknown.

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
The short DDNS process is capped at 20% of one CPU core (5% of a four-core
host), with Nice=15 and CPUWeight=1; it does not stay resident. The updater
reuses one TLS context and validates startup freshness only once.

## Proactive peer-session recovery

`vpn-recovery.yml` is a separate optional deployment. It installs a small native
`desk-vpn-recovery` helper on all nodes, independent of VPN election and readiness.
The owner sends HMAC-authenticated endpoint hints every 5 seconds to private
10.10.10.x addresses on UDP 51854. Only endpoints already learned by WireGuard
for configured peers with a recent handshake are included. Receivers pin private
source membership, server identity, peer membership, signature and timestamps;
replays, stale hints and conflicting publishers are rejected. Cache lives only
in process RAM, expires after 15 seconds and stores no transport/private keys.
The dedicated ignored key is `secret/vpn-recovery.key`; web reader namespaces
cannot access `/etc/desk-vpn-recovery`.

After a false-to-true local VIP transition, the helper waits for source routing
and the owner output gate. If one fresh hint remains, it renews only the matching
peer's ephemeral state, retaining the installed peer identity
and AllowedIPs, then emits one UDP discard-port datagram through the tunnel to
start a standard kernel handshake. The former owner does not need to answer any
RPC or remain powered on. Already renewed, deleted or altered peers are skipped. The current helper
preserves PSKs through private stdin and retains keepalive when recreating a peer.
The standalone `vpn-recovery.yml` prerequisite still rejects PSK/keepalive peers;
for UI-managed peers use the coordinated `vpn-ui.yml` deployment.

Starting/restarting this helper on an existing owner never resets its current
peer session. Missing/expired/conflicting cache, helper failure or a cold cluster
restart leaves normal WireGuard client-driven recovery available. Static keys,
on-disk WireGuard config, modem rules and election priorities stay unchanged.
The service has CAP_NET_ADMIN for kernel WG state, no CAP_NET_RAW, binds only the
private address and is capped at 10% of one core with a 32 MiB memory limit.
This accelerates re-handshake, not transport-key or TCP/NAT-state replication.
