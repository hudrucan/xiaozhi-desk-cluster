"""Bound checkpoint CPU independently of the lease/fencing controller."""
import secrets
import subprocess
import sys

TIMEOUT = 60
STAGE_TIMEOUT = 25
SLICE = 'desk-network-checkpoint.slice'


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
              '--property=CPUQuota=50%', '--property=MemoryMax=256M',
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
