"""Same-filesystem journal exchange; no copying, deletion of entries or vacuum."""
import ctypes
import grp
import os
import re
import stat
import subprocess
from pathlib import Path
import sys


def exchange(left, right):
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.renameat2
    if call(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        raise OSError(ctypes.get_errno(), 'Journal atomic exchange failed')


def verify_archives(root=Path('/')):
    live = root/'var/log/journal'
    machine = (root/'etc/machine-id').read_text().strip()
    if not re.fullmatch('[0-9a-f]{32}', machine):
        raise ValueError('Invalid journal machine-id')
    current = live/machine
    group = grp.getgrnam('systemd-journal').gr_gid
    for directory in (live, current):
        info = directory.stat()
        if (directory.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, group, 0o2755)):
            raise ValueError('Expected real root:systemd-journal 2755 directory: '+str(directory))
    active = current/'system.journal'
    if active.is_symlink() or not active.is_file():
        raise ValueError('Missing active system journal in relocated machine directory')
    if not (root/'run/systemd/journal/flushed').exists():
        raise ValueError('Persistent journal flush flag missing')
    # Rotation seals these files. Never integrity-check a journal still being appended.
    archives = sorted(live.glob('*/*@*.journal'))
    if not archives:
        raise ValueError('No archived journals after rotation')
    for path in archives:
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise ValueError('Expected regular archived journal: '+str(path))
        subprocess.run(['journalctl', '--verify', '--file='+str(path)], check=True, timeout=60)
    return len(archives)


def relocate(root=Path('/'), action='move'):
    live = root/'var/log/journal'
    old = root/'var/log.hdd/journal'
    stage = root/'var/log/.desk-journal-relocate'
    if action == 'rollback':
        if stage.is_symlink() and live.is_dir() and not live.is_symlink():
            exchange(stage, live)
        if stage.is_dir() and not stage.is_symlink() and live.is_symlink() and not old.exists():
            stage.rename(old)
            return True
        return False
    if action == 'finish':
        # Keep the rollback symlink until all fallible reference inspection finishes.
        # rmdir succeeds only for an empty directory; never traverse or delete logs.
        parent = old.parent
        references = False
        for process in (root/'proc').glob('[0-9]*'):
            try:
                handles = [process/'cwd', process/'root', *list((process/'fd').iterdir())]
                for handle in handles:
                    try:
                        references |= handle.resolve().is_relative_to(parent)
                    except FileNotFoundError:
                        pass
            except FileNotFoundError:
                pass
        if stage.is_symlink() and live.is_dir() and not live.is_symlink():
            stage.unlink()
        if parent.exists() and not parent.is_symlink() and not references:
            try:
                parent.rmdir()
            except OSError:
                pass
        return False
    if stage.exists() or stage.is_symlink():
        raise ValueError('Unfinished relocation: inspect and rollback before retry')
    if live.is_dir() and not live.is_symlink():
        return False
    if not (root/'run/systemd/journal/flushed').exists():
        raise ValueError('Persistent flush required before relocation')
    defaults = root/'etc/default/armbian-ramlog'
    if defaults.exists() and not re.search(r'^ENABLED=false$', defaults.read_text(), re.M):
        raise ValueError('Disable ramlog before relocating its journal')
    if not live.is_symlink() or live.resolve() != old.resolve() or not old.is_dir() or old.is_symlink():
        raise ValueError('Expected journal symlink to /var/log.hdd/journal')
    if old.stat().st_dev != live.parent.stat().st_dev:
        raise ValueError('Journal relocation requires the same filesystem')
    machine = (root/'etc/machine-id').read_text().strip()
    if len(machine) != 32 or any(c not in '0123456789abcdef' for c in machine) or not (old/machine).is_dir():
        raise ValueError('Missing machine-id journal directory')
    # Preserve every journal file, inode, ACL and xattr. Normalize only parent mode.
    os.chown(old, 0, grp.getgrnam('systemd-journal').gr_gid)
    os.chmod(old, 0o2755)
    os.chown(old/machine, 0, grp.getgrnam('systemd-journal').gr_gid)
    os.chmod(old/machine, 0o2755)
    old.rename(stage)
    try:
        exchange(stage, live)
    except BaseException:
        stage.rename(old)
        raise
    return True


if __name__ == '__main__':
    if sys.argv[1:] == ['verify-archives']:
        print('Verified archived journals:', verify_archives())
    else:
        print('changed' if relocate(action=sys.argv[1]) else 'unchanged')
