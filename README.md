# Xiaozhi Desk Cluster

Ansible owns the three RK3528 desk boxes. Wi-Fi provides SSH management and
Internet access; the `end0` Ethernet fabric is the private cluster LAN. Do not
configure node identity, networking, SSH or NATS manually outside Ansible.

| Node      | Management address (`inventory.ini`) | Private address (`host_vars`) |
| --------- | ------------------------------------ | ----------------------------- |
| `deskb1x` | `192.168.1.101`                      | `10.10.10.11`                 |
| `deskb2x` | `192.168.1.102`                      | `10.10.10.12`                 |
| `deskb3x` | `192.168.1.103`                      | `10.10.10.13`                 |

Host-specific private addressing remains authoritative in
`host_vars/<node>.yml` as `cluster_ip`; NATS templates and checks consume those
values. `group_vars/deskboxes.yml` retains the existing interface/connection
settings and adds password references to Vault variables.

## Core NATS

All three nodes run **nats-server v2.15.0**, installed from the official
[ARM64 Debian release](https://github.com/nats-io/nats-server/releases/tag/v2.15.0).
The role accepts only this pinned release and SHA256:

```text
nats-server-v2.15.0-arm64.deb
7e37abbab18910ebc6d7a4733546d061d1098c7ee35881a92ce9d451f5804d92
```

The role checks Debian OS family, ARM64 facts and `dpkg --print-architecture`
before installation. It downloads to `/var/cache/nats-server` only when the
installed package differs, verifies the cached checksum before `apt` installation,
and permits an explicit downgrade to the pinned version. Matching cached content
is reused. The package is held against repository-driven upgrades. Changing the
version later requires deliberately updating the role's pin, checksum and URL
assertions together; it never follows `latest` or a Debian/Armbian NATS version.

The official package supplies `/usr/bin/nats-server` and no systemd unit. Ansible
provides the `/usr/sbin/nats-server` symlink required by the managed service and
owns `/etc/systemd/system/nats-server.service`.

| Listener                    | Port       | Bind address on each node |
| --------------------------- | ---------- | ------------------------- |
| Application clients         | `4222/TCP` | `cluster_ip`              |
| Authenticated server routes | `6222/TCP` | `cluster_ip`              |
| HTTP monitoring             | `8222/TCP` | `cluster_ip`              |

Cluster name is `desk-cluster`; each server name is `inventory_hostname`.
`max_payload` is `8MB`, and debug/trace are disabled. Each node seeds routes to
the other two nodes using `hostvars[peer].cluster_ip`, excluding itself. Explicit
route advertisement also uses the private IP. No listener binds to Wi-Fi or
`0.0.0.0`. Preflight requires distinct `10.10.10.0/24` addresses and checks that
the local `cluster_ip/24` exists on the private interface.

This is **Core NATS only**. JetStream is disabled by omission: there is no
JetStream block, storage directory or persistence setup. Core NATS does not
persist messages for disconnected subscribers. Future consumers are
`xiaozhi-core` and `xiaozhi-worker` for internal RPC and streaming-operation
control. The MQTT gateway is not a NATS client.

Public defaults live in `roles/nats/defaults/main.yml`: version, release URL and
checksum, listener ports, cluster name, payload size, usernames and verification
retry settings. Override shared listener/user settings in group vars, not per-host
address copies. `nats_client_user` defaults to `xiaozhi`; `nats_route_user` defaults
to `nats-route`. The V1 client identity has no subject restrictions. Subject-level
permissions will follow the RPC contract, including request/reply `_INBOX.*` needs.

## Vault and transport policy

The operator must add these string variables to the existing **encrypted**
`vars/vault.yml` using their normal Ansible Vault workflow:

- `vault_nats_client_password`: nonempty application/client password.
- `vault_nats_route_password`: nonempty server-route password.

Group vars reference them through `nats_client_password` and
`nats_route_password`. Do not substitute plaintext public variables or commit
unencrypted passwords. Secret assertions, configuration rendering and NATS
configuration validation use `no_log`; secret template diffs are disabled.

Route usernames and passwords must use only ASCII letters, digits, `.`, `_`, `~`
or `-` because credentials appear in `nats://user:password@address:port` URLs.
Preflight rejects other characters instead of guessing URL-encoding behavior.
Application and route usernames must differ. The application password is quoted
and escaped in the generated configuration and is not used in route URLs.

V1 intentionally omits TLS because the Ethernet fabric is physically private.
**Credentials and NATS traffic are not encrypted on this link.** Monitoring HTTP
also has no authentication; its private bind is the access boundary. Do not expose
these ports through Wi-Fi, routing or port forwarding to an untrusted network.
Add TLS and certificate lifecycle management if that trust boundary changes.

## Service and deployment order

NATS runs as the unprivileged system user/group `nats`, without a login or home
directory. `/etc/nats-server.conf` is owned `nats:nats`, mode `0600`, so the service
can read it while other users cannot. `root:nats` with `0600` would prevent the
service user from reading it. The unit makes the filesystem read-only to the
service with `ProtectSystem=strict`; no writable JetStream storage is added.
Other hardening includes no privilege escalation, private temporary/device views,
protected home/kernel settings, empty capabilities and IPv4/Unix address families.
Logs go to journald. Package service actions are suppressed during installation.

Execution is `/usr/sbin/nats-server -c /etc/nats-server.conf`, with validation in
`ExecStartPre`. The service is enabled at boot and restarts on failure after three
seconds. Systemd sends `SIGUSR2` for
[NATS lame-duck shutdown](https://docs.nats.io/learn/deployment/rolling-upgrades):
five-second grace and a thirty-second drain, with a forty-five-second stop timeout.

`site.yml` runs:

1. Existing identity, network and access roles.
2. Existing SSH hardening with `serial: 1`, unchanged.
3. The NATS role in its own `serial: 1` play, stopping the rollout on failure.
4. A separate read-only NATS verification play after all serial batches finish.
5. A separate `worker`-tagged play for the explicit `xiaozhi_workers` group.
6. A `control-plane`-tagged `serial: 1` play for `xiaozhi_control_planes`.
7. Final read-only control-plane verification from one deskbox over the private LAN.
8. An `ingress`-tagged all-node preflight, serial ingress rollout and final verification.

The config template uses `nats-server -t -c <candidate>` before replacing the
live config. Ordered handlers validate `/etc/nats-server.conf` again before
daemon-reload and restart. Validation failure stops that host and the rollout;
it does not restart a healthy service with a bad generated config. Unchanged
package/config/unit causes no restart. Handlers are flushed before local health
is checked so a node becomes locally ready before advancing the serial batch.
That local check does not require peers during first installation.

After review and Vault preparation, the operator can apply the full inventory:

```sh
ansible-playbook -i inventory.ini site.yml --ask-vault-pass
```

The controller needs Ansible and `community.general` for the existing network
role. Target hosts need Debian ARM64, systemd, Python and APT support. Use the
existing `bootstrap.yml` access workflow when required. First installation must
cover all three nodes before full-cluster route verification can succeed.

## Verification

The NATS verification play includes `roles/nats/tasks/verify.yml`, a separate read-only entry
point that reuses role defaults without rerunning installation tasks. From each
managed node it checks:

- `systemctl is-active nats-server`.
- HTTP 200 from its private `/healthz`, bypassing management HTTP proxies.
- `/routez` eventually reports exactly the other two distinct server names and
  private IPs, and two distinct remote server IDs.

The v2.15.0 [`Routez`/`RouteInfo` schema](https://github.com/nats-io/nats-server/blob/v2.15.0/server/monitor.go)
exposes `server_name`, `num_routes`, and `routes` entries with `remote_name`,
`remote_id` and `ip`. `num_routes` counts TCP route connections, including pools;
it is not an exact peer count. Verification deduplicates names/IDs/IPs instead of
requiring `num_routes == 2`. It retries transient convergence after the final
node starts. It changes no configuration or services.

Repository review can parse YAML and render templates with placeholder passwords
without opening Vault or connecting to nodes. A full syntax check is normally:

```sh
ansible-playbook -i inventory.ini site.yml --syntax-check --ask-vault-pass
```

When Vault access is unavailable or forbidden, use a temporary copy of the
playbook that omits `vars_files` solely for structural syntax checking; that
cannot prove Vault values resolve. Do not weaken the real playbook or decrypt
Vault merely for static review. Runtime config validation, systemd compatibility
and route/health checks still require a separately approved deployment.

## Standalone workers

The explicit `[xiaozhi_workers]` inventory group contains `deskb1x`, `deskb2x`
and `deskb3x`; all three workers are deployed/eligible. Worker membership is
explicit and is not inferred from `deskboxes`.

`roles/xiaozhi_worker` deploys source from
[`hudrucan/xiaozhi-desk-robot-server`](https://github.com/hudrucan/xiaozhi-desk-robot-server)
at the full, immutable commit
`e586c725600dd79a3e24ec7f763bb32d7f9e3c9f`. The public
`xiaozhi_worker_revision` must be a full 40-character SHA, never a branch or
`latest`. A future source update changes that pin deliberately. Checkout is
root-owned; local working copies and rsync are not deployment sources.

| Path                                         | Purpose                                            |
| -------------------------------------------- | -------------------------------------------------- |
| `/opt/xiaozhi-worker/repo`                   | Pinned source checkout                             |
| `/opt/xiaozhi-worker/venv`                   | Dedicated Python 3.11 environment                  |
| `/etc/xiaozhi-worker.env`                    | Existing NATS client credentials, root:root `0600` |
| `/etc/systemd/system/xiaozhi-worker.service` | Managed worker service                             |

Only `main/xiaozhi-server/requirements-worker.txt` is installed into the venv:
currently `nats-py==2.16.0`. Full `requirements.txt`, providers, FFmpeg, Opus
and model packages are not installed. APT prerequisites are `git`, `python3`,
`python3-venv`, `ca-certificates` and `python3-packaging`; the last supports
Ansible's pip module on the system interpreter. Pinned requirements use
`state: present`, without upgrades or forced reinstalls.

The environment's three NATS URLs are derived from sorted `deskboxes` and
their existing `hostvars[host].cluster_ip`; no address copies are added to vars.
The worker reuses `nats_client_user` (default `xiaozhi`) and
`nats_client_password`, which must equal `vault_nats_client_password`. The role
reads the NATS role's public username/port defaults without executing NATS
deployment and honors existing overrides. No separate worker password is added.
`XIAOZHI_WORKER_ID` is `inventory_hostname`. The environment template quotes
values and escapes backslashes/double quotes for systemd; secret assertions,
rendering and the request probe use `no_log`, with secret diffs disabled.

The unprivileged `xiaozhi-worker` system user/group has no login or home.
Systemd runs `/opt/xiaozhi-worker/venv/bin/python worker.py` from
`/opt/xiaozhi-worker/repo/main/xiaozhi-server`, with no stdin/TTY and journald
output. The service starts after network-online and local NATS, is enabled at
boot, and restarts on failure after three seconds. SIGTERM permits worker drain,
with a sixty-second stop timeout. Hardening includes `NoNewPrivileges`,
`PrivateTmp`, `ProtectHome`, `ProtectSystem=strict`, no capabilities and only
Unix/IPv4 sockets. Python bytecode writes are disabled; no runtime write path is
added. Source, venv dependency, environment or unit changes notify restart;
only unit changes notify daemon-reload. Unchanged services remain started.

The worker play loads the existing encrypted `vars/vault.yml`. Preflight checks
explicit membership, the three private NATS nodes, nonempty matching Vault
credentials, the full source SHA, Debian ARM64 and Python 3.11 before mutations.
For check/apply, limit to the worker group as well as selecting its tag so
unrelated plays cannot gather facts on non-worker hosts:

```sh
ansible-playbook -i inventory.ini site.yml --check --tags worker --limit xiaozhi_workers --ask-vault-pass
ansible-playbook -i inventory.ini site.yml --tags worker --limit xiaozhi_workers --ask-vault-pass
```

Check mode reports the source/venv desired state and evaluates environment/unit
templates without creating accounts/directories, installing packages, checking
out source, invoking pip or running service handlers/probes. These dependent
steps wait for a real apply; `check_mode: false` is not used to force mutations.

After deployment, `tasks/verify.yml` checks `systemctl is-active xiaozhi-worker`
and uses the worker venv's `nats-py` for a read-only targeted request to
`xiaozhi.v1.worker.<inventory_hostname>.ping`. It requires the
JSON object to equal protocol `xiaozhi-worker-v1`, the corresponding worker ID, status
`ok` and `capabilities: []`. Request timeout is three seconds, connect timeout
is eight seconds overall, and each probe is bounded to fifteen seconds with
finite retries. No NATS CLI or balanced queue-distribution check is added.
This deploys only the transport skeleton; no normal `app.py`,
providers, core dispatcher, gateway, JetStream or containers are started.

## Standalone control-plane backends

The explicit `[xiaozhi_control_planes]` group contains all three deskboxes.
`roles/xiaozhi_control_plane` defines the same standalone Settings/control process
on each node, pinned to server revision
`fda3c3c62e79e8ad3d03f7346203694feef2f9ab`. It never runs normal `app.py`, workers
or provider/model initialization. The `control-plane` play follows NATS with
`serial: 1` and `any_errors_fatal: true`; preflight requires all three group members
to be selected, Debian ARM64 and the system Python 3.11 interpreter.

| Path | Ownership / purpose |
| --- | --- |
| `/opt/xiaozhi-control-plane/repo` | Root-owned pinned checkout |
| `/opt/xiaozhi-control-plane/venv` | Root-owned dedicated Python environment |
| `/etc/xiaozhi-control-plane.env` | Root:root `0600`; systemd reads NATS credentials |
| `/etc/xiaozhi-control-plane/drive-credentials.json` | Root:service-group `0640`, private directory `0750` |
| `repo/main/xiaozhi-server/data/bootstrap.yaml` | Root:service-group `0640`; node identity and private Cloud authority |
| `repo/main/xiaozhi-server/data/{cloud-config,node-secrets,cloud-soundbank}` | Service-owned `0700`; the only persistent service write paths |

The source `data/` parent is root:service-group `0750`; the service cannot replace
its managed bootstrap or write elsewhere in the checkout/venv. Runtime paths
are already Git-ignored by the pinned source. Ansible creates fresh state; it
does not copy developer-machine `data/` or generate runtime Soundbank files.
The `xiaozhi-control-plane` system user/group has no login shell or home.

APT installs only `git`, `python3`, `python3-venv`, `python3-packaging`,
`ca-certificates` and `libopus0`. Pip installs only
`main/xiaozhi-server/requirements-control-plane.txt`, without full server
requirements, provider models or FFmpeg. Opus is retained for strict P3 validation
when Settings explicitly edits Soundbank; unrelated V2 Settings Save needs no
local Soundbank bytes. Nodes need Internet/package access and read access to the
pinned Git repository.

Populate the existing **encrypted** `vars/vault.yml` locally. No private values
belong in public defaults, inventory, host vars or this README:

- `vault_xiaozhi_cloud_folder_id`: the shared Cloud Config folder.
- `vault_xiaozhi_cloud_manifest_file_id`: the same Config manifest on all nodes.
- `vault_xiaozhi_cloud_credentials`: a YAML credential mapping supported by
  Google Auth, rendered with `to_nice_json`; it must authorize live reads and
  CAS-last Config publication to that authority.
- Optional `vault_xiaozhi_cloud_memory_manifest_file_id`: rendered as
  `google_drive.memory_manifest_file_id` only when supplied; Memory is not required.
- Existing `vault_nats_client_password`: reused unchanged through
  `nats_client_password`; no separate control-plane password is added.

NATS username/port come from the NATS public defaults and honor the same shared
overrides as workers. The NATS server list comes from sorted `deskboxes` and
`hostvars.cluster_ip`; no private-address copies or worker identity are added.
Secret assertions/candidate rendering use `no_log`; credential, bootstrap and
password-bearing environment writes also disable diffs. Each bootstrap sets
`config_provider: google_drive`, `node_id: inventory_hostname`, and absolute
credentials path `/etc/xiaozhi-control-plane/drive-credentials.json`.

Each backend binds **only** its `cluster_ip:8004` on `end0` with
`XIAOZHI_CONTROL_PLANE_ALLOW_REMOTE=true`. Reconciliation defaults to 45 seconds.
No control-plane listener binds `0.0.0.0` or the `wlan0` management address.
Settings APIs are unauthenticated within this trusted private backend segment.
The ingress foundation below exposes them to the trusted management LAN;
do not route/expose either segment to an untrusted network.
The current initial Cloud desired VIP is `192.168.1.186`; it is desired state,
not proof of an active address. Ansible does not own or pin the VIP value. Verification
requires all three nodes to report the same non-empty value and trusts server-side
Cloud Config validation for address validity. Changing VIP through unified Settings
does not require an Ansible expected-value change.

Systemd wants/starts after network-online and local NATS, runs the dedicated venv's
`python control_plane.py` from `repo/main/xiaozhi-server`, uses journald and no
stdin/TTY, disables bytecode writes, restarts on failure and sends SIGTERM with
a 120-second graceful stop window. Hardening mirrors workers, including no
capabilities and a read-only filesystem except the three managed data directories.
Unix, IPv4 and IPv6 sockets support Drive/NATS clients.

After independent review and local Vault preparation:

```sh
ansible-playbook -i inventory.ini site.yml --check --tags control-plane --limit xiaozhi_control_planes --ask-vault-pass
ansible-playbook -i inventory.ini site.yml --tags control-plane --limit xiaozhi_control_planes --ask-vault-pass
```

Check mode evaluates preflight and all templates, including private candidates
without printing them. It skips account/package/source/venv/data/credential writes,
service actions and live probes, and never forces changes with `check_mode: false`.
Successful check mode cannot prove Cloud credentials, topology or runtime readiness.

Apply flushes ordered reload/restart handlers, starts the unchanged service and
checks `systemctl is-active`, local `/healthz` and `/api/cluster` on the private
address before advancing. Health is a cheap validated desired-cache/assignment
check; it can remain healthy offline. Deployment additionally requires a
successful live Cloud reconciliation, connected NATS, matching node identity and
no conversation runtime. Missing assignments fail with a non-secret message;
Ansible never adds them.

After all three serial batches finish, the final read-only play probes all six
health/status URLs **from `deskb1x` over the private LAN**, bypassing HTTP proxies.
Bounded retries permit startup convergence. It requires health 200 on every node,
matching identities, Google Drive, shared V2 scope, one common desired revision,
one non-empty Cloud-configured VIP, connected NATS and `runtime_revision: null` with
runtime capability disabled. A recorded active revision is allowed and does not
mean this process started conversation runtime.

V1/legacy scope fails final verification with an explicit migration message.
Missing topology assignments, divergence or Cloud access errors also fail read-only.
Handle these in a separate explicitly reviewed Cloud migration/topology operation,
then rerun; this role does not POST sync/migration or change Cloud state. Only
healthy shared V2 revision agreement on all three nodes permits ingress deployment.

## Management ingress foundation

`[xiaozhi_ingress]` explicitly contains all three control-plane nodes. Run
`ingress.yml` to deploy only this foundation, or select the `ingress` tag in
`site.yml`. Ingress is intentionally part of default full-site convergence:
an untagged `site.yml` apply includes it after control-plane verification.
The `ingress` tag selects these plays; it does not make them opt-in. Use
`--skip-tags ingress` to omit ingress from a full-site run.
No Vault credentials are needed by the standalone ingress playbooks;
they read only the safe local control-plane status API. All three backends must
already be deployed, healthy, reconciled to shared V2, and agree on one revision
and a non-empty `ingress.configured_vip`. Legacy migration is a separate operation.
A status sample can overlap periodic Cloud reconciliation. Preflight retries the
read-only status GET while its state is `refreshing` (up to ten retries with a
one-second delay and the existing three-second request timeout). After that it
still requires `ready`, synced shared V2 and a successful live reconciliation;
an error, invalid snapshot or persistent refresh fails closed. It does not
trigger a Cloud sync, accept cached state as ready or start the failover test.

The VIP comes from **Cloud desired state**, never an Ansible default/expected
address. `192.168.1.186` is the current initial Cloud value only. Preflight reads
all three local `/api/cluster` endpoints before any mutation and freezes the
agreed value for that rollout. Immediately before each serial node's first
mutation, a read-only local status check requires its current desired revision
and configured VIP to match its frozen preflight values. Final verification
re-reads all three control planes and requires the same frozen revision/VIP
before reporting success. These checks never update the frozen values; drift or
an unreadable status stops rollout/verification without dynamic VIP migration.
It does not PUT Config, migrate Cloud state, or
materialize Soundbank assets. The interface/prefix is discovered from the unique
local IPv4 address matching `ansible_host` in inventory (currently `wlan0`),
rather than assuming a NIC name or using the private Ethernet default route.
Every management address and the VIP must share a usable management subnet;
the VIP cannot equal any node address. Server-side Cloud validation remains the
authority for the configured address contract.

The role installs Debian `haproxy` and `keepalived` packages with automatic service
starts suppressed. It owns `/etc/haproxy/haproxy.cfg`,
`/etc/keepalived/keepalived.conf`, a root-owned Python standard-library readiness
probe and a sysctl file enabling IPv4 nonlocal binding. This lets HAProxy bind
only `VIP:80` on standby nodes as well as the owner. Port 80 is configurable via
`xiaozhi_ingress_port`; backend port defaults to the existing control-plane port
8004. Backends come from sorted inventory `cluster_ip` values: currently
`10.10.10.11:8004`, `10.10.10.12:8004`, `10.10.10.13:8004`. HTTP round-robin health
checks require `/healthz` status 200. No public stats/admin listener is added;
HAProxy stats are available only through a root-accessible Unix socket.

Keepalived uses unicast VRRP between the three inventory management addresses,
VRID 51 by default (`xiaozhi_ingress_vrid`; reserve a unique VRID on this LAN).
All instances start in `BACKUP`; sorted-node priorities 150/140/130 are election
preferences, with `nopreempt` to avoid automatic failback to a recovered node.
The current VIP owner sends two gratuitous ARP messages every 30 seconds
(`xiaozhi_ingress_garp_refresh_seconds` / `xiaozhi_ingress_garp_refresh_repeat`)
to refresh neighbor/AP learning. Only the MASTER instance sends these periodic
announcements; a newly elected owner advertises its own MAC. The same settings
are required across all nodes (positive integer interval up to one day and
one to five repetitions). This uses Keepalived's native GARP mechanism, without
a cron job or a static mapping to one node. It does not prove every Wi-Fi
firmware will forward ARP correctly; validate idle-cache and failover behavior.
There is no permanent master. A bounded readiness script enters FAULT when
HAProxy has no open frontend or no healthy backend; at least one healthy backend
keeps ingress eligible. A failed local control-plane alone therefore need not
remove a working proxy serving another node. Deployment verification is stricter:
every proxy must see all three named backends UP. Cached `/healthz` validates the
local desired snapshot/assignment and does not require NATS to remain connected.

Management LAN devices can access Settings through `http://<Cloud VIP>/settings/`.
This foundation provides HTTP without authentication/TLS, using the existing
trusted-LAN Settings contract. It requires LAN/AP support for moving IPv4/GARP,
and bidirectional VRRP IP protocol 112 between the management peers. It does not
change firewall, Wi-Fi, private Ethernet, workers, providers or conversation
runtime. The control-plane status/UI still reports Cloud ingress as desired-only;
Ansible's address/service verification is what establishes actual VIP ownership.

After review, from a controller with direct management-LAN reachability:

```sh
ansible-playbook -i inventory.ini ingress.yml --check
ansible-playbook -i inventory.ini ingress.yml
ansible-playbook -i inventory.ini verify-ingress.yml
```

Select all three nodes; limited/partial deployment is rejected. Check mode makes
only read-only local status/health GETs, address queries and snapshot reads, then
renders candidates. It installs/downloads nothing, writes nothing, and changes no
services/sysctls. A static syntax check (`--syntax-check`) does not contact nodes.
During apply, package config validators run before candidate replacement;
HAProxy is started/reloaded and all three expected backends must be UP before
the validated Keepalived candidate is installed and its service explicitly
started/restarted. Keepalived has no restart handler: failed backend verification
cannot newly activate/restart it or replace its configuration. Changed
Keepalived configuration restarts the service; unchanged configuration only
ensures it is started. Rollout uses `serial: 1` with failure stopping further
nodes. Unchanged configuration does not
reload/restart services. Final read-only checks sample all three address lists
until exactly one VIP assignment is observed, require all three proxy backend
pools UP, and GET VIP `/healthz` and `/settings/` from the management controller. Health uses
a three-second request timeout; the Settings HTML transfer allows ten seconds
(`xiaozhi_ingress_settings_timeout`). Both checks use bounded
`xiaozhi_ingress_verify_retries` / `xiaozhi_ingress_verify_delay` retries and
require HTTP 200; repeated Settings timeouts still fail verification.

Failover is a separate, explicit service interruption exercise, never part of
normal apply or check mode:

```sh
ansible-playbook -i inventory.ini verify-ingress-failover.yml
```

It verifies the initial cluster, discovers the actual owner, stops only that
node's Keepalived, requires exactly one different owner and working VIP HTTP,
and restores the original service in an `always` block even when verification
fails. Successful completion rechecks the whole restored cluster. This proves
service-level failover, not a physical power-loss test.
If the controller is killed or loses SSH during the exercise, restore Keepalived
on the reported original node with `systemctl start keepalived` before repeating
verification.

For deployment acceptance, capture VIP-only ARP on the elected owner's discovered
management interface and confirm periodic announcements at the configured
interval. Check VIP HTTP from the management controller after a quiet period
longer than the LAN's ARP cache age; a brief health pass is not that proof.
Run the explicit failover exercise separately and check the new owner is unique,
HTTP recovers, and recovery does not force failback. Observe the management
router/AP behavior without pinning the VIP to one node's MAC.

Keep the current Cloud VIP outside the management DHCP allocation range to
prevent a later DHCP lease collision. Exclude the floating address from the pool;
a fixed DHCP reservation or static ARP binding to one node is unsuitable for
failover. DHCP pool administration is an operator/router task, outside this role.

`/etc/xiaozhi-ingress.json` records the public deployment VIP before configuration
replacement, including partial attempts. It is a safety guard, not Cloud desired
state. A rerun with a changed Cloud VIP stops before mutations; this iteration
does not perform live VIP migration, old/new two-phase cutover or runtime watching.
Changing Settings remains valid, but applying that change to existing ingress
requires a separately reviewed migration. Never delete this guard to force a
live cutover. Configuration changes other than the VIP can still be rerun.

Configuration references: [HAProxy 2.6 manual](https://docs.haproxy.org/2.6/configuration.html),
[Keepalived VRRP configuration](https://www.keepalived.org/documentation/keepalived-conf/),
and [Linux nonlocal binding](https://kernel.org/doc/html/latest/networking/ip-sysctl.html).
