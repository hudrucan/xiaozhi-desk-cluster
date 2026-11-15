"""Read-only journal measurements; stdout contains counts/paths, never log messages."""
import fnmatch
import glob
import json
from pathlib import Path
import re
import subprocess
import sys
import os
import stat
import shutil
import tempfile

LIMIT = 200 * 1024 * 1024


def allocated(path):
    if not Path(path).exists():
        if Path(path).is_symlink():
            raise ValueError('Broken journal directory symlink: '+path)
        return 0
    return int(subprocess.check_output(['du', '-s', '-L', '-B1', path], text=True, timeout=30).split()[0])


def entries():
    journal = subprocess.Popen(['journalctl', '--no-pager', '-q', '--output=json'],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    counter = subprocess.Popen(['wc', '-l'], stdin=journal.stdout, stdout=subprocess.PIPE)
    journal.stdout.close()
    try:
        output, _ = counter.communicate(timeout=60)
        if journal.wait(timeout=5) or counter.returncode:
            raise ValueError('Cannot count existing journal entries')
        return int(output)
    finally:
        for process in (counter, journal):
            if process.poll() is None:
                process.kill()
            process.wait()


def rotation_coverage(output, paths):
    patterns = []
    for block in output.split('rotating pattern:')[1:]:
        # logrotate's debug header ends before per-log "considering" output.
        header = block.split('considering log ', 1)[0].split('empty log files', 1)[0]
        patterns.extend(re.findall(r'/var/log/[^\s{}"\']+', header))
    return [p for p in paths if not any(fnmatch.fnmatchcase(p, pattern) for pattern in patterns)]


def inspect():
    disk, ram = allocated('/var/log/journal'), allocated('/run/log/journal')
    count = entries()
    result = dict(entries=count, disk_bytes=disk, ram_bytes=ram, max_bytes=LIMIT,
                  safe_bytes=LIMIT*4//5)
    paths = sorted(set(['/var/log/haproxy.log', '/var/log/syslog', *glob.glob('/var/log/*.log')]))
    try:
        rotation = subprocess.run(['logrotate', '--debug', '/etc/logrotate.conf'],
                                  capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        rotation = subprocess.CompletedProcess([], 127, '', '')
    result.update(rotation_rc=rotation.returncode,
                  rotation_uncovered=rotation_coverage(rotation.stdout+rotation.stderr, paths))
    return result


def validate_rotation(candidate, root=Path('/')):
    # Validate the entire effective configuration, replacing only our stanza.
    config = (root/'etc/logrotate.conf').read_text()
    with tempfile.TemporaryDirectory(prefix='desk-logrotate-') as directory:
        directory = Path(directory)
        includes = directory/'rules'
        includes.mkdir()
        for source in (root/'etc/logrotate.d').iterdir():
            if source.name != 'desk-node-logs' and source.is_file():
                shutil.copy2(source, includes/source.name)
        shutil.copyfile(candidate, includes/'desk-node-logs')
        os.chmod(includes/'desk-node-logs', 0o644)
        config, count = re.subn(r'(?m)^\s*include\s+/etc/logrotate.d\s*$', 'include '+str(includes), config)
        if count != 1:
            raise ValueError('Expected one /etc/logrotate.d include; refusing partial validation')
        staged = directory/'logrotate.conf'
        staged.write_text(config)
        subprocess.run(['logrotate', '--debug', str(staged)], check=True, timeout=30)


def cleanup_archives(root=Path('/')):
    if not (root/'run/systemd/journal/flushed').exists():
        raise ValueError('Persistent flush required before cleanup')
    defaults = root/'etc/default/armbian-ramlog'
    if defaults.exists() and not re.search(r'^ENABLED=false$', defaults.read_text(), re.M):
        raise ValueError('ramlog must be disabled before cleanup')
    directory = root/'var/log.hdd'
    if directory.is_symlink():
        raise ValueError('Refuse symlink archive directory')
    names = r'(?:syslog|kern\.log|auth\.log|user\.log|cron\.log|alternatives\.log|armbian-hardware-monitor\.log|armbian-ramlog\.log|boot\.log|dpkg\.log|fontconfig\.log|haproxy\.log)'
    candidates = [p for p in directory.glob('*') if re.fullmatch(names+r'(?:\.[0-9]+(?:\.gz)?|-[0-9]{8}(?:\.gz)?)', p.name) and not p.is_symlink() and p.is_file()]
    opened = set()
    for process in (root/'proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            for fd in (process/'fd').iterdir():
                try:
                    info = fd.stat()
                    opened.add((info.st_dev, info.st_ino))
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    removed = []
    for path in candidates:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and (info.st_dev, info.st_ino) not in opened:
            path.unlink()
            removed.append(str(path))
    return sorted(removed)


if __name__ == '__main__':
    if sys.argv[1:] == ['inspect']:
        print(json.dumps(inspect()))
    elif sys.argv[1:] == ['count']:
        print(entries())
    elif sys.argv[1:2] == ['validate'] and len(sys.argv) == 3:
        validate_rotation(sys.argv[2])
    elif sys.argv[1:] == ['cleanup']:
        print(json.dumps(cleanup_archives()))
    else:
        raise SystemExit('Expected inspect, count, validate or cleanup')
