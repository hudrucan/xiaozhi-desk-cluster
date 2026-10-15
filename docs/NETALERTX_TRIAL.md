# NetAlertX default CPU canary — 2026-10-14

NetAlertX 26.10.0, official ARM64 image pinned to
`ghcr.io/netalertx/netalertx@sha256:f95bca7a794141296dd1d16be5bb7f39f6fdbcd20353cdb51eee696e12d317f6`.
Measured on deskb1x (four cores), with a 90-second host baseline, 120-second
startup and 780-second default runtime. Samples were taken at one-second
intervals from the container cgroup, covering all its subprocesses. CPU values
below are divided by four and expressed as a percentage of the whole node.

| Measurement | Default runtime |
| --- | ---: |
| Mean application CPU | 4.59% |
| Median application CPU | 0.34% |
| p95 application CPU | 24.79% |
| Maximum application CPU | 26.09% |
| Samples above 20% whole-node CPU | 105 seconds |
| Peak cgroup memory, including cache | 142.32 MiB |
| Cgroup block write delta | 16.95 MiB |

Startup peak was 55.51% of the whole node. The idle median is
low, but scheduled work repeatedly approaches one full core. This short trial
does not establish long-term disk growth or TTS impact; TTS was not benchmarked.
Host baseline varied and was not subtracted to estimate application CPU.

Discovery/plugin defaults were unchanged. Automatic subnet selection included
the private cluster subnet, LAN wlan0 twice (with VIP addresses), and the
WireGuard subnet. These should be reviewed before permanent deployment; do not
interpret default discovery as management of modem DHCP leases/reservations.

The trial used packaged Podman, no daemon, no automatic container updates,
read-only container root, host networking, separate PID namespace, 2 GiB memory
ceiling, 512 PID ceiling, 256 MiB temporary filesystem and official required
capabilities. No CPU quota was applied during measurement. Frontend bound to
10.10.10.11:20211. The pinned API source had one bind-address change to keep it
on 127.0.0.1:20212; no public dashboard route was added.

A transient systemd watchdog stopped the canary after the measurement and
removed its RAM marker. Ports are closed. Retained data and measurements live
in `/var/lib/desk-netalertx-trial/` on node 1. The application is not enabled at
boot and NetAlertX HA/config replication has not been deployed.
The dashboard showed **Network trial** only while the canary was active.
