# Xiaozhi Desk Cluster

Ansible manages three RK3528 deskboxes. Wi-Fi provides management/Internet;
`end0` is the private cluster fabric. Keep node identity, network, SSH and service
configuration under Ansible.

| Node | Management (`inventory.ini`) | Private (`host_vars`) |
| --- | --- | --- |
| deskb1x | 192.168.1.101 | 10.10.10.11 |
| deskb2x | 192.168.1.102 | 10.10.10.12 |
| deskb3x | 192.168.1.103 | 10.10.10.13 |

Source, role defaults, shared vars and explicit operator overrides define the
supported configuration. They do not establish current production ownership,
readiness or revision. Inspect service status before operating.

## Deployment

The controller needs Ansible and `community.general`; targets use Debian ARM64,
Python 3.11 and systemd. Keep credentials in encrypted `vars/vault.yml` or the
service's ignored `secret/` inputs. Reuse existing keys and recovery state.

```sh
ansible-playbook -i inventory.ini site.yml --ask-vault-pass
```

`site.yml` applies identity/network/access, Wi-Fi driver, SSH, Core NATS, workers,
control planes and management ingress, with serial service updates and readiness
gates. Default workers are ping-only; conversation runtime requires explicit
voice settings. Preserve deployment overrides on reruns. Other services below
have separate entrypoints; the site playbook does not install them all.

Run commands from the repository root so Ansible loads `ansible.cfg` and its
`roles_path`. Feature entrypoints live under `playbooks/<feature>/`; only
`site.yml` and `bootstrap.yml` remain at root. Keep the cluster inventory at
`inventory.ini`: `group_vars/all.yml` derives `desk_repo_root` from the original
host's inventory directory, including controller-delegated tasks. Local assets,
Vault and secrets retain their repository paths. Pi deployment uses its separate
`playbooks/pi-panel/inventory.ini` and adjacent host vars.

For the layout regression checks, use the Python environment that provides
Ansible, PyYAML and Jinja2:

```sh
python3 -m unittest discover -s tests -v
```

## Services and operations

The management VIP is Cloud-configured; examples use `192.168.1.186`. Its applied
snapshot guards against accidental live VIP changes. LAN administration uses HTTP
with anonymous DNS UI and Beszel auto-login; Home Assistant keeps native auth.

| Service | Entrypoints | Current documentation |
| --- | --- | --- |
| NATS | `site.yml` | [Transport, credentials and recovery](docs/NATS.md) |
| Xiaozhi / voice / MQTT / node panel | `playbooks/xiaozhi/core/core.yml`, `playbooks/xiaozhi/mqtt/mqtt-gateway.yml`, `playbooks/xiaozhi/mqtt/mqtt-ingress.yml`, `playbooks/xiaozhi/workers/worker-*.yml`, `playbooks/xiaozhi/core/core-tts.yml`, `playbooks/panel/panel.yml` | [Runtime and deployment](docs/XIAOZHI_RUNTIME.md), [OAuth recovery](docs/CLOUD_OAUTH.md) |
| DNS `.250:53`, `fe80::250` | `playbooks/dns/dns.yml`, `playbooks/dns/dns-sync.yml`, scoped DNS playbooks | [DNS](docs/DESK_DNS.md), [IPv6/router settings](docs/DESK_DNS_IPV6.md), [healthchecks](docs/DESK_HEALTHCHECKS.md) |
| Monitoring `/monitor/` | `playbooks/monitoring/monitoring.yml`, `playbooks/monitoring/monitoring-sync.yml`, `playbooks/monitoring/monitoring-ui.yml` | [Beszel configuration and operations](docs/DESK_MONITORING.md) |
| VPN `.254:51820`, UI `/vpn/` | `playbooks/vpn/vpn-ha.yml`, `playbooks/vpn/vpn-ui.yml`, `playbooks/vpn/vpn-ddns.yml`, `playbooks/vpn/vpn-recovery.yml` | [WireGuard HA](docs/DESK_VPN.md), [VPN UI](docs/DESK_VPN_UI.md) |
| Network `/network/` | `playbooks/network/network.yml`, `playbooks/network/network-helpers.yml`, scoped Network playbooks | [Discovery, Rust checkpoints and recovery](docs/DESK_NETWORK.md) |
| Home Assistant `:8123` | `playbooks/homeassistant/homeassistant.yml`, `playbooks/homeassistant/homeassistant-checkpoint.yml`, `playbooks/homeassistant/homeassistant-online-sync.yml` | [Core lifecycle and recovery](docs/HOME_ASSISTANT.md) |
| Cameras | `playbooks/camera/camera-media.yml`, coordinated camera cutovers | [Capabilities and deployment order](docs/CAMERA_MEDIA.md) |
| Dashboard `/` and LAN routes | `playbooks/dashboard/desk-dashboard.yml`, `playbooks/dashboard/dashboard-ui.yml`, `playbooks/ui/ui.yml` | [Dashboard](docs/DESK_DASHBOARD.md), [LAN UI](docs/DESK_UI.md), [maintenance](docs/CLUSTER_EXPERIENCE.md) |
| Pi LCD | `playbooks/pi-panel/deploy.yml` with `playbooks/pi-panel/inventory.ini` | [Pi operations](docs/PI_PANEL.md), [panel API](docs/CLUSTER_PANEL_API.md) |
| Wi-Fi driver / movable VIPs | `playbooks/cluster/wifi-driver.yml` | [Pins, activation and recovery](docs/DESK_WIFI_DRIVER.md) |

Camera shared selections are Bed/Guess owner recording, all-camera owner
observations and Guess-only PTZ/preset/privacy commands. Activation still requires
the ordered worker/Core authority cutovers in the camera contracts.

## Static checks

```sh
ansible-playbook -i inventory.ini site.yml --syntax-check --ask-vault-pass
git diff --check
```

Service guides describe their focused checks. Syntax checks do not contact nodes;
`--check` may read installed state and is not deployment acceptance. Keep golden
fixtures, configuration JSON, runtime data and ignored recovery material intact.
