# Native recurring VIP healthchecks

The optional `desk_healthcheck_native_enabled` inventory flag replaces only the
three recurring Python probes used by the two Keepalived processes. It is enabled
for the existing three deskboxes. It does not alter VIPs, election priorities,
preemption, backend distribution, failover timers, DNS fallback or voice providers.

`/usr/local/libexec/desk-healthcheck` is a static Linux ARM64 executable built on
the controller with Go 1.22 or later, using only the standard library and
`CGO_ENABLED=0`. No compiler, package or additional daemon is installed on nodes.
Its controller artifact is cached outside Git by source hash. Nodes receive the
root-owned executable through Ansible's atomic copy. There is no checkpoint,
network listener or periodic disk writer. Keepalived still invokes the probes;
only interpreter startup and repeated Python imports are removed.

| Command | Readiness condition | Existing cadence |
| --- | --- | --- |
| `ingress` | HAProxy UNIX stats socket; Settings frontend OPEN, exactly the three expected backends present, at least one UP | 2 seconds; timeout 3; fall/rise 2 |
| `dns` | Local numeric management address at port 5353 answers the frontend probe with exactly `127.0.0.1` over UDP and TCP | 5 seconds; timeout 4; fall/rise 2 |
| `prefer` | Read the current deployed Xiaozhi VIP and local interface addresses; return failure when this node owns it | 5 seconds; timeout 4; fall/rise 2; weight -50 |

Socket reads, DNS framing and JSON reads are bounded. Malformed/missing state,
unavailable sockets and invalid DNS responses return failure; no optimistic
cached health is used. The executable reports through exit status only;
Keepalived logs health transitions. It has no credentials or user-query payloads.
DNS frontend liveness still succeeds during emergency resolver mode. Full Python
metadata/filter/backend validation remains in deployment verification.

## Rollout

For the current installed cluster, use:

```bash
ansible-playbook -i inventory.ini playbooks/cluster/healthchecks.yml --check
ansible-playbook -i inventory.ini playbooks/cluster/healthchecks.yml
```

The dedicated playbook installs the helper everywhere, checks live readiness and
compares the old/new VIP preference before editing any VRRP command. It changes
only three script lines in installed configs, validates Keepalived candidates,
and sends SIGHUP serially to active processes. Inactive instances stay inactive.
Per-node guards retain VIP ownership, protected service PIDs and non-VRRP
configuration. A failed candidate/guard restores that attempt's exact original
configs and reloads active instances. Legacy DNS config and ingress module backups
are retained on the nodes. The normal `playbooks/ui/ingress.yml`, `playbooks/dns/dns.yml` and `playbooks/dns/dns-ipv6.yml`
install the helper before rendering native commands when the flag is enabled;
disabling the flag renders the original Python commands in those normal playbooks.

## Offline checks

```sh
GO111MODULE=off go test -ldflags=-linkmode=external roles/desk_healthcheck/files/healthcheck.go roles/desk_healthcheck/files/healthcheck_test.go
python3 -m unittest discover -s roles/desk_healthcheck/tests -p 'test_*.py'
```
