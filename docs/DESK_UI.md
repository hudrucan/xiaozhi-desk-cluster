# Unified LAN administration

The current Cloud management VIP is `192.168.1.186`. Open its root for links to
`/xiaozhi/` (Xiaozhi), `/dns/` (DNS, powered by AdGuard Home) and
`/monitor/` (Monitoring, powered by Beszel).
Ansible reads the installed Settings VIP snapshot; it does not pin its address.
DNS serving remains on its separate VIP, currently `192.168.1.250:53`.

The explicitly requested LAN mode removes AdGuard UI accounts (`users: []`) and
uses Beszel's native `AUTO_LOGIN` for `admin@desk.cluster` on all three independent hubs.
These are full administration interfaces, including policy edits and allowed
service logs. The management HTTP routes accept the existing management LAN;
The legacy `.250:3001` UI listener accepts only cluster-node source addresses and loopback. Other private backend listeners are unchanged. No password prompt is expected.
Beszel history, users and preferences remain independent across hubs.

`/dns/` forwards to the current DNS VIP owner, rather than round-robin across
independent AdGuard UIs. Existing protected peer synchronization replicates that
owner's policy. Local loopback synchronization skips UI login, but peer tokens,
TLS, source restrictions and write boundaries remain required. `/dns/` now
aggregates statistics, client discovery/lookup and timestamp-ordered query logs
from all three backends. Query-log/statistics saves commit to the DNS VIP owner
and apply to all peers; periodic reconciliation repairs offline replicas.
Clear query logs and Reset statistics act on every backend and report incomplete
operations instead of claiming success. The native AdGuard layout is retained.
Original client addresses pass over private TLS DoH with trusted forwarding
headers. Hostnames are retained when known; the original IP remains available
for AdGuard's native display. Local logs/caches are still stored per backend.

Run `ansible-playbook -i inventory.ini ui.yml` explicitly; it is not part of
`site.yml`. The play freezes process/configuration state and live DNS policy,
quiesces policy pulls, changes each node serially, and restores reconciliation.
Failure cleanup restores periodic pulls on reachable nodes. AdGuard validates a
candidate containing only the auth change and retains a private rollback copy
at `/var/lib/desk-adguard/pre-ui-auth.yaml`; existing live policy is preserved.
Only AdGuard, its sync boundary, Beszel hubs and the two HTTP proxies may restart/reload.
Xiaozhi runtimes, MQTT and both VRRP services are preserved, including any
intentionally withdrawn DNS VRRP instance.

The complete HTTP and existing MQTT candidate must validate before atomic
installation and graceful proxy reload. Landing page edits also trigger reload.
Future DNS/monitoring/ingress reruns retain the inventory's chosen auth mode and
UI routes. Re-enabling authentication requires deliberately restoring accounts
and matching loopback sync credentials, and clearing Beszel AUTO_LOGIN before
rolling out; do not overwrite live AdGuard policy with the old rollback file.

Router DHCP, IPv6 DNS advertisements, VPNs and Private Relay are unchanged by
this UI rollout. Clients must actually use the DNS VIP to receive its filtering.


The landing page names services consistently with their public paths: Xiaozhi,
DNS and Monitoring. Product names remain in the DNS/Monitoring descriptions.
Old Settings page bookmarks redirect to /xiaozhi/; existing static asset/API
paths stay compatible. HAProxy maps the public UI path to the installed native
Settings handler, preserving /xiaozhi/ota/ for firmware bootstrap. This change
requires only a validated graceful HAProxy reload, not a Xiaozhi runtime restart.

ui-routes.yml updates an already unified installation serially, validates the
combined HTTP/MQTT candidate and verifies protected processes/configuration and
the native bootstrap response.
