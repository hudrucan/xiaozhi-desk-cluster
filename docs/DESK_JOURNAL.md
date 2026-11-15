# Persistent node journal

`playbooks/node/journal.yml` runs separately, serially with fatal errors stopping
the rollout. It retains `SystemMaxUse=200M`, restarts journald only when the
retention drop-in changes, and checks journald, HAProxy, Keepalived and Caddy.

On the inspected Armbian nodes, `/var/log` is on eMMC and the ramlog service is
disabled, but `/etc/default/armbian-ramlog` still contains `ENABLED=true`.
The 15-minute `armbian-truncate-logs` cron job can consequently run
`journalctl --relinquish-var`, clearing `/run/systemd/journal/flushed` and moving
new journal writes to the small RAM journal until the next flush. RAM rotation
then discards evidence; disk journal timestamps can stop at midnight. The same
script can delete `*.log` files and vacuum the journal to 5M when `/var/log`
exceeds its 75% threshold. Increasing SystemMaxUse alone does not prevent this.
These are supplied observations from the nodes, not checks performed by this play.

The play first installs a root-owned safety helper and `/etc/logrotate.d/desk-node-logs`.
Copy validation stages the complete existing logrotate configuration, replaces only
this owned stanza, and runs `logrotate --debug` before installing the candidate.
A failed validation keeps the previous stanza. Package-owned Armbian rules are
unchanged: their `/var/log.hdd` paths remain harmless when absent with `missingok`.
The new stanza covers only the eleven explicit live paths, excluding HAProxy's
separately managed rule. Rsyslog files use `create 0640 root adm`, sharedscripts
and the packaged rsyslog reopen hook; other files preserve existing creation
attributes. There is no copytruncate. All use daily/maxsize100M/rotate7,
compress/delaycompress/missingok/notifempty. **maxsize is checked only when
logrotate runs**; a daily timer/cron is not a hard 100M cap.

Then allocated disk/RAM journal bytes, entry count and effective rotation coverage
are printed. Above 160 MiB combined allocation, invalid logrotate configuration or
any uncovered live top-level log stops the rollout before flush/ramlog changes.
`journalctl --flush` runs only when the flush flag is absent. The flag and a
nondecreasing count are required before setting the existing optional
`ENABLED=false` defaults line. No Storage override or vacuum is performed.
The 200M drop-in still restarts journald only when changed, followed by the
existing four service checks. Check mode predicts copies but does not flush,
disable ramlog, clean archives or establish persistent writes.

Only after flush and disable, cleanup lists and removes regular, singly linked
rotated archives in `/var/log.hdd` for explicitly known log names (`.N`, `.N.gz`,
or dateext suffixes). It scans process FDs and retains open files, symlinks,
unknown names, base/current logs and the entire `journal` subtree. Permission
errors stop cleanup. No directory is recursively removed; savings depend on
which closed archives exist. A concurrently running old ramlog job can race the
change: observe at least one 15-minute cron interval before advancing. Counts
reduce risk but cannot prove identical entry sets or recover old lost evidence.

## Separate journal relocation

`playbooks/node/journal-relocate.yml` is independent of the retention play and
supports a single-node canary. Run part A first. It counts entries, flushes and
syncs, requires the known same-filesystem symlink and machine-id directory,
renames its target to a staging sibling, then uses Linux `renameat2(RENAME_EXCHANGE)`
to atomically exchange the directory with the symlink. Files retain their inodes,
contents, ACLs and xattrs; root/machine directories are root:systemd-journal 2755.
Journald reopens after restart; flush flag, nondecreasing entry count and
archived-file verification must pass before retiring the staging symlink. After
reopen/flush, `journalctl --rotate` seals existing files; only `*@*.journal` files
are checked with `journalctl --verify --file=...`. Active journals can report
transient integrity errors while journald appends, so the new active system file
is checked for existence and real machine-directory location instead. Directory
ownership/mode, flush flag, nondecreasing count and four active services remain
required; a failed archived-file check still triggers rollback. A real
journal directory is left unchanged on rerun. Unsupported exchange fails safely.

On failure, rescue exchanges back and restores the original symlink target before
reopening journald. A crash between steps leaves `.desk-journal-relocate` for
operator inspection/rollback, never an automatic delete. The old parent is removed
only by `rmdir` when empty and without observed process references; remaining logs
or references keep it. Armbian package files are never edited. Archive deletion in
part A is irreversible; journal relocation itself preserves the same journal files.

After deployment approval, run A then B separately on `.103`, `.102`, `.101`,
checking services, flush flag, directory ownership, counts and disk timestamps
between each node. Never proceed after a gate/rescue failure. For relocation
rollback while the staging symlink exists, flush/sync, run the helper's `rollback`,
restart journald and verify/count again. After successful finish, reverse the move
only in a separately reviewed maintenance transaction (flush/sync, move the real
directory back and recreate the original symlink); do not delete journal data.
To deliberately restore Armbian RAM behavior, restore the previous defaults and
use `journalctl --relinquish-var`; this reintroduces evidence loss. Restore any
previous drop-in separately and restart only for that configuration change.

## Read-only eMMC observation

After separate permission for node access, run the collector over stdin so its
source, samples and results are not written to the node filesystem:

```sh
for node in 192.168.1.103 192.168.1.102 192.168.1.101; do
  ssh -T -o BatchMode=yes ops@"$node" 'sudo -n python3 -B - --interval 300' < scripts/emmc-health.py
done
```

This performs no recovery, restart or diagnostic file writes. It reads
`/sys/block/mmcblk2/device/life_time`, `pre_eol_info` and field 7 of block `stat`
(sectors written, multiplied by 512). Two in-memory samples five minutes apart
also report the ten largest `/proc/PID/io` write-byte deltas and cancelled-write
deltas, keyed by PID and process start time. Missing health attributes are shown
as unavailable. Raw wear values are reported without guessing remaining lifetime.
Only surviving readable processes are counted: exited/new processes, kernel
writeback, metadata and filesystem amplification prevent equating process totals
with physical eMMC writes. SSH, sudo and ordinary system activity may themselves
be logged by the OS; the collector does not suppress those normal audit records.
Do not redirect output to a node file when measuring writes. No live readings
were taken as part of this repository change.

## Periodic service logging

DNS peer ARP, ingress peer ARP, DNS policy pull/filter refresh, VPN DDNS and
transient network checkpoint workers use `LogLevelMax=notice`. Timer cadence and
resource limits are unchanged. DNS/ARP stdout and stderr default to notice so
existing failure diagnostics survive. DDNS keeps routine standby/unchanged output
at info, emits confirmed updates and ownership changes at notice, and errors at
err using systemd priority prefixes. Worker stdout/stderr remain captured by the
existing pipe; controller checkpoint failures still produce warnings.

On systemd 252, failed unit results are warning; unsuccessful process exits are
notice (normal exit) or warning (signal). These survive the unit cutoff. Ordinary
starting/finished/deactivated messages at info are filtered. Separate credential
mount units are outside this filter; resource summaries already at notice may
remain. This is not a global journald filter. See the
[systemd.exec documentation](https://www.freedesktop.org/software/systemd/man/252/systemd.exec.html#LogLevelMax=)
and [systemd 252 unit logging source](https://github.com/systemd/systemd/blob/v252/src/core/unit.c).
