"""Bound checkpoint CPU independently of the lease/fencing controller."""
import pathlib
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


def execute(command, blob=None):
    # macOS is a source/test host, never a supported production cluster node.
    if sys.platform != 'linux':
        return subprocess.run(command, input=blob, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True, timeout=TIMEOUT)
    unit = 'desk-network-checkpoint-' + secrets.token_hex(12) + '.service'
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
    except BaseException:
        # Killing systemd-run alone must not leave the worker consuming CPU.
        try:
            subprocess.run(['/usr/bin/systemctl', 'stop', unit],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass  # RuntimeMaxSec/BindsTo remain the independent final fence.
        raise


def sweep_stale_sqlite(run_dir, min_age_seconds=0.0):
    """One-time startup sweep for sqlite-*.db files left by the old codebase.

    The current snapshot_worker() gives every worker its own isolated scratch
    directory (ckpt-<hex>/) and shutil.rmtree()s it after the subprocess exits,
    so no sqlite-*.db files can ever be orphaned under the new code.

    This function is kept only to clean up files that were orphaned by OOM-killed
    workers running the *previous* version (mtime-based approach) and that may
    still be present in the run-dir after an in-place upgrade.  It should be
    called once at process startup, before any worker runs, to purge anything
    left over, and is otherwise a no-op on a clean node.  Orphaned ckpt-*
    scratch dirs (service SIGKILLed mid-rmtree) are removed the same way.
    """
    run = pathlib.Path(run_dir)
    now = time.time()
    for d in run.glob('ckpt-*'):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    for pattern in ('sqlite-*.db', 'sqlite-*.db-journal',
                    'sqlite-*.db-wal', 'sqlite-*.db-shm'):
        for p in run.glob(pattern):
            try:
                if now - p.stat().st_mtime >= min_age_seconds:
                    p.unlink()
            except OSError:
                pass  # already gone or unlink raced; ignore
