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
ansible-playbook -i inventory.ini healthchecks.yml --check
ansible-playbook -i inventory.ini healthchecks.yml
```

The dedicated playbook installs the helper everywhere, checks live readiness and
compares the old/new VIP preference before editing any VRRP command. It changes
only three script lines in installed configs, validates Keepalived candidates,
and sends SIGHUP serially to active processes. Inactive instances stay inactive.
Per-node guards retain VIP ownership, protected service PIDs and non-VRRP
configuration. A failed candidate/guard restores that attempt's exact original
configs and reloads active instances. Legacy DNS config and ingress module backups
are retained on the nodes. The normal `ingress.yml`, `dns.yml` and `dns-ipv6.yml`
install the helper before rendering native commands when the flag is enabled;
disabling the flag renders the original Python commands in those normal playbooks.

## Validation (2026-10-12)

Five native tests cover HAProxy socket operation and DOWN/malformed stats,
real loopback UDP/TCP DNS framing, wrong/truncated/oversized responses and current
VIP preference. Two controller tests validate YAML/Python and prove the templates
change only the probe commands; changes to priorities fail the guard. DNS template
fixtures, Go vet, playbook syntax and check-mode passed.

All three nodes completed rollout without failed tasks. VIP ownership, protected
PIDs and configurations matched preflight. A second run reported `changed=0` on
all nodes. DNS VIP IPv4/IPv6 UDP/TCP checks passed from all three nodes;
Settings, AdGuard and Beszel pages returned HTTP 200. The final exit-status-only
binary was then aligned without another VIP reload. No physical power-loss test
or Cake TTS RTF benchmark was performed.

Before/after short cgroup samples include each service's short-lived children:

| Node | Both Keepalived services before (% one core) | After (% one core) |
| --- | ---: | ---: |
| deskb1x | 29.94 | 1.15 |
| deskb2x | 30.04 | 1.28 |
| deskb3x | 29.68 | 1.21 |

That removes about 96% of their CPU cost, or approximately 7 percentage points of
whole-host CPU on the four-core boxes. Separate native ingress measurements took
9–10 ms CPU per call, compared with 251–268 ms for the existing Python command.
Whole-host 25-second observations moved from 16.1/20.6/17.0% busy on nodes 1/2/3
to 11.0/14.1/15.3%. Other control-plane work varied during these observations;
these are not controlled TTS latency measurements. The panel was unchanged and
still consumed around 6.7% of one core in the cgroup sample.

The controller test command used with Go 1.22 on the current macOS host was:

```bash
GO111MODULE=off go test -ldflags=-linkmode=external roles/desk_healthcheck/files/healthcheck.go roles/desk_healthcheck/files/healthcheck_test.go
python -m unittest discover -s roles/desk_healthcheck/tests -p 'test_*.py'
```
