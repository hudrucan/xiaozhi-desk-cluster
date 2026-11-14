# Wi-Fi observation

This phase only records connectivity evidence for the aic8800 Wi-Fi path. It
never reconnects, changes routes, reloads a driver, notifies NTFY or reboots.
`desk_wifi_watch_recovery: false` is reserved; changing it has no effect.

Run the separate playbook `ansible-playbook -i inventory.ini
playbooks/node/wifi-watch.yml`. It processes inventory order, one node at a time,
requires an active timer and waits for one successful observer execution before
advancing. A successful execution means the observation completed, including when
network probes fail. It is not a connectivity gate. It is not imported by
`site.yml`. The explicit deployment trial can run before the timer's boot grace.

The oneshot runs every 30 seconds after a two-minute boot grace, with five-second
timer accuracy. Healthy samples read the current lowest-metric IPv4 default from
`/proc/net/route` for wlan0, wireless statistics and byte counters directly; they
spawn just `ping -I wlan0 -c1 -W1 <gateway>`. Reading the kernel route table instead
of spawning `ip` on this path was explicitly approved. No gateway is hard-coded.
On failure, the observer retries the router alongside four concurrent probes to
the two physical peers over wlan0 and end0. It also reads `ip route show default
dev wlan0`, the gateway neighbor, two bounded nmcli snapshots (without rescan),
and a recent kernel window (at most 80 entries inspected, 20 matching lines kept).
The window is bounded to two minutes and starts after the last failure snapshot;
wall-clock corrections use a fresh 90-second window. Busy kernel logs can omit
older evidence. No credentials are requested or recorded.

Read `journalctl -t desk-wifi-watch`. `LogLevelMax=notice` filters routine systemd lifecycle messages while `SyslogLevel=notice` keeps all observer stdout/stderr events, including heartbeats, outages and errors, at priority 5. Each event is one line of `key=JSON-value`
fields, including wall time, monotonic time, uptime and rx/tx bytes. Router replies
mean `ok`; only wlan0 peers replying means `router_or_icmp`; only end0 peers
replying means `wlan_path_down`; neither means `isolated`. These are observations,
not proof of a driver fault. Three consecutive failed samples confirm an outage
(roughly 60–90 seconds after the last good sample); a successful retry clears a
failure. Connected with unchanged RX across three failed samples adds
`assoc_no_data`. `outage_start` includes BSSID/signal and filtered kernel evidence;
`outage_end` uses monotonic duration from the first failed sample. Classification
or association-flag changes during an outage produce `outage_change`.

Boot changes, backwards monotonic time, invalid/missing transient state, or a gap
above 120 seconds during failed sampling reset observation without inventing an
outage duration. `/run/desk-wifi-watch/sample.json` holds transient failure counts
on tmpfs. Stable healthy samples write nothing and log nothing. A transition,
initial reset, or ten-minute heartbeat atomically writes
`/var/lib/desk-wifi-watch/state.json` (0600 in a root-owned 0700 directory) and emits
one journal line; the persistent file is a checkpoint, not the source of live
counts after boot. Failed cycles update only the tmpfs sample until an event.

An internal nonblocking flock prevents overlap. The root service bounds its
capabilities to CAP_NET_RAW for ping, with NoNewPrivileges, private tmp and a
read-only system tree except its two state directories. Root also permits local
NetworkManager D-Bus queries and kernel journal reads without granting network
administration capabilities. Every child has a timeout; concurrent pings share
1.5 seconds, other queries use 0.6–1 seconds, and an eight-second command budget
leaves room for cleanup below ten seconds in normal execution. systemd provides
an independent 20-second timeout, Nice=19, idle I/O, CPUQuota=30%, MemoryMax=32M
and TasksMax=32. Debian 12 ARM64 capability/NetworkManager/journal access and
resource limits still require a hardware canary; CPU contention or stuck kernel
I/O can defeat a userspace wall-time expectation. No node validation is implied
by the offline fixtures.

Rollback with `ansible-playbook -i inventory.ini playbooks/node/wifi-watch.yml
-e desk_wifi_watch_state=absent`: stop and disable the timer, stop any active
sample, remove only observer units/program/config and its runtime/persistent
state, and reload systemd. Existing journal evidence remains. Phase 2 recovery,
NTFY, reboot and driver reload are deliberately unimplemented.

Offline checks: `python3 -m unittest discover -s roles/desk_wifi_watch/tests -v`,
`python3 -m unittest discover -s tests -v` and the playbook syntax check. The serial
fixtures remap all paths into temporary directories and replace systemd with
local stubs; they never connect to managed nodes.
