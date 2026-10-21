"""Bound checkpoint CPU independently of the lease/fencing controller."""
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import time

TIMEOUT = 60
STAGE_TIMEOUT = 25
SLICE = 'desk-network-checkpoint.slice'
# Memory budget for one snapshot worker.
# The worker runs in desk-network-checkpoint.slice — a host cgroup separate
# from the container's own cgroup; --memory=512m on the container does not
# apply here.
#
# Measured on node 2 (resource.getrusage RUSAGE_SELF, 2026-10-17):
#   Source DB 27.3 MiB  →  peak data-path RSS 148 MiB
#     read_bytes:   39 MiB   (DB copy in memory)
#     base64 encode: 112 MiB  (cumulative; 4/3× expansion stays live)
#     json.dumps:   148 MiB  (raw JSON string peaks here; gzip frees nothing)
#   Python interpreter + ha.py import overhead: ~20–30 MiB on top
#   Estimated total peak: ~170–180 MiB for a 27 MiB DB
#
# 256 MiB caused repeated OOM kills (confirmed: 292 kills in log).
# 384 MiB provides a ~200 MiB margin over the measured data-path peak.
# Tighten this value once the first successful checkpoint completes under the
# new limit and VmHWM is read from the worker process.
_WORKER_MEMORY_MAX = '384M'


class WorkerNotStopped(RuntimeError):
    """Retain private scratch until worker termination can be confirmed."""


def stop_worker(unit):
    if not re.fullmatch(r'desk-network-checkpoint-[0-9a-f]{24}\.service', unit):
        raise WorkerNotStopped('Invalid checkpoint unit marker')
    def state():
        result = subprocess.run(['/usr/bin/systemctl', 'show', unit,
                                 '-p', 'LoadState', '-p', 'ActiveState', '-p', 'ControlGroup'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=5, check=False)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if values.get('LoadState') == 'not-found':
            return values
        if result.returncode or values.get('LoadState') != 'loaded':
            raise WorkerNotStopped('Checkpoint unit state unavailable')
        return values
    try:
        before = state()
        if before['LoadState'] == 'not-found':
            return
        subprocess.run(['/usr/bin/systemctl', 'stop', unit], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5, check=False)
        after = state()
        if after['LoadState'] != 'not-found' and after.get('ActiveState') not in ('inactive', 'failed'):
            raise WorkerNotStopped('Checkpoint unit still active')
        for group in {before.get('ControlGroup', ''), after.get('ControlGroup', '')} - {''}:
            events = pathlib.Path('/sys/fs/cgroup') / group.lstrip('/') / 'cgroup.events'
            try:
                values = dict(line.split() for line in events.read_text().splitlines())
            except FileNotFoundError:
                continue  # The entire worker cgroup has been removed.
            if values.get('populated') != '0':
                raise WorkerNotStopped('Checkpoint cgroup still populated')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise WorkerNotStopped('Checkpoint worker termination unconfirmed') from None


def execute(command, blob=None):
    # macOS is a source/test host, never a supported production cluster node.
    if sys.platform != 'linux':
        return subprocess.run(command, input=blob, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True, timeout=TIMEOUT)
    unit = 'desk-network-checkpoint-' + secrets.token_hex(12) + '.service'
    if '--run' in command:
        private = pathlib.Path(command[command.index('--run') + 1])
        # Publish before launching; parent-crash recovery never loses the unit ID.
        pending = private / 'worker.unit.tmp'
        pending.write_text(unit)
        pending.replace(private / 'worker.unit')
    runner = ['/usr/bin/systemd-run', '--quiet', '--pipe', '--wait', '--collect',
              '--unit=' + unit, '--slice=' + SLICE,
              '--property=BindsTo=desk-network.service',
              '--property=After=desk-network.service',
              '--property=RuntimeMaxSec=50', '--property=TimeoutStopSec=2',
              '--property=CPUQuota=50%', '--property=MemoryMax=' + _WORKER_MEMORY_MAX,
              '--property=KillMode=control-group', '--property=Nice=19',
              '--property=NoNewPrivileges=yes', '--property=TasksMax=8',
              '--property=Environment=PYTHONDONTWRITEBYTECODE=1', '--', *command]
    try:
        return subprocess.run(runner, input=blob, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True, timeout=TIMEOUT)
    finally:
        # Covers success, worker failure, runner timeout and caller interruption.
        # A failed confirmation tells the parent to retain scratch for recovery.
        try:
            stop_worker(unit)
        except BaseException as error:
            if isinstance(error, WorkerNotStopped):
                raise
            raise WorkerNotStopped('Checkpoint worker termination unconfirmed') from None


def sweep_stale_sqlite(run_dir, min_age_seconds=0.0):
    """Recover marked workers before serving requests, plus legacy SQLite orphans.

    A marker survives parent death; stop/confirm its unit before removing files.
    Unmarked directories predate this contract or died before launching a worker.
    Systemd must finish stopping the old controller cgroup (including pending
    launchers) before restarting it; its service uses KillMode=control-group.
    Never run this sweep concurrently with new checkpoint requests.
    """
    run = pathlib.Path(run_dir)
    now = time.time()
    for d in run.glob('ckpt-*'):
        if d.is_dir():
            marker = d / 'worker.unit'
            if marker.exists():
                stop_worker(marker.read_text())
            shutil.rmtree(d)
    for pattern in ('sqlite-*.db', 'sqlite-*.db-journal',
                    'sqlite-*.db-wal', 'sqlite-*.db-shm'):
        for p in run.glob(pattern):
            try:
                if now - p.stat().st_mtime >= min_age_seconds:
                    p.unlink()
            except OSError:
                pass  # already gone or unlink raced; ignore
