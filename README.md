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
Settings APIs are unauthenticated within this trusted private backend segment;
do not route/expose them to an untrusted network. There is no VIP endpoint yet.
HAProxy, unicast Keepalived, address assignment and failover are the next iteration.
The current initial Cloud desired VIP is `192.168.1.186`; it is desired state,
not an active address. Ansible does not own or pin the VIP value. Verification
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
healthy shared V2 revision agreement on all three nodes permits future ingress work.
