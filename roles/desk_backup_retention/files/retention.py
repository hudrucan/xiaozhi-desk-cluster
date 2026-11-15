"""Prune a controller-supplied, repository-owned catalog; no general path globbing."""
import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time


def safe_path(path, root):
    """Reject symlink ancestors as well as escaped/protected namespaces."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Invalid backup path')
    if not any(str(path).startswith(p) for p in
               ('/etc/', '/usr/local/libexec/', '/opt/desk-caddy/',
                '/opt/desk-monitoring/', '/tmp/', '/var/tmp/', '/var/backups/')):
        raise ValueError('Path outside owned backup namespaces')
    mapped = root / str(path).lstrip('/')
    if mapped.resolve() != mapped:
        return None
    return mapped


def snapshot(path, directory=False):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if value.st_uid != os.geteuid() or stat.S_ISLNK(value.st_mode):
        return None
    if directory:
        return value if stat.S_ISDIR(value.st_mode) and not os.path.ismount(path) else None
    return value if stat.S_ISREG(value.st_mode) and value.st_nlink == 1 else None


def identity(value):
    return value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns


def removable_tree(path, device):
    """No symlinks, hardlinks, foreign owners or nested filesystems/mounts."""
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            child = Path(directory) / name
            value = child.lstat()
            if (value.st_uid != os.geteuid() or value.st_dev != device or
                    stat.S_ISLNK(value.st_mode) or os.path.ismount(child)):
                return False
            if stat.S_ISREG(value.st_mode):
                if value.st_nlink != 1:
                    return False
            elif not stat.S_ISDIR(value.st_mode):
                return False
    return True


def candidates(directory, base, kind):
    if not directory.is_dir():
        return []
    result = []
    for child in directory.iterdir():
        if kind == 'directories':
            if not re.fullmatch(re.escape(base) + r'[A-Za-z0-9_.:-]+', child.name):
                continue
            value = snapshot(child, True)
            created = value.st_mtime if value else None
        else:
            match = re.fullmatch(re.escape(base) + r'\.([0-9]+)\.(\d{4}-\d{2}-\d{2}@\d{2}:\d{2}:\d{2})~', child.name)
            if not match:
                continue
            value = snapshot(child)
            try:
                # Ansible backup_local uses node-local time and preserves source mtime.
                created = datetime.datetime.strptime(match[2], '%Y-%m-%d@%H:%M:%S').timestamp()
            except ValueError:
                continue
        if value:
            result.append((created, child.name, child, value))
    return result


def groups(spec, root):
    kind = spec['type']
    if kind == 'directories':
        for parent in spec['parents']:
            # Validate a child path so a namespace root cannot itself be removed.
            example = safe_path(parent + '/' + spec['prefix'] + 'example', root)
            if example:
                yield example.parent, spec['prefix'], kind
    elif kind == 'ansible':
        path = safe_path(spec['file'], root)
        if path:
            yield path.parent, path.name, kind
    elif kind == 'versioned':
        example = safe_path(spec['directory'] + '/1.0/' + spec['file'], root)
        if not example:
            return
        parent = example.parent.parent
        if parent.is_dir():
            for child in parent.iterdir():
                if (re.fullmatch(r'v?\d+(?:\.\d+)+(?:[-+][A-Za-z0-9._-]+)?', child.name)
                        and snapshot(child, True)):
                    path = safe_path(str(Path(spec['directory'])/child.name/spec['file']), root)
                    if path:
                        yield path.parent, path.name, kind
    else:
        raise ValueError('Unknown backup kind')


def prune(request, root=Path('/'), now=None):
    root = Path(root).resolve()
    days = float(request['days'])
    if not math.isfinite(days) or days <= 0:
        raise ValueError('Retention days must be finite and positive')
    catalog = request['catalog']
    scopes = request['scopes']
    if not isinstance(scopes, list) or any(s not in catalog for s in scopes):
        raise ValueError('Unknown backup scope')
    threshold = (time.time() if now is None else now) - days * 86400
    check = request.get('check', False)
    if not isinstance(check, bool):
        raise ValueError('Check mode must be boolean')
    planned, deleted, skipped = [], [], []
    # Complete catalog/path/date validation before deleting anything.
    selected = {}
    for scope in dict.fromkeys(scopes):
        selected[scope] = list(groups(catalog[scope], root))
    seen = set()
    for members in selected.values():
        values = []
        for directory, base, kind in members:
            values.extend((created, name, path, value, kind)
                          for created, name, path, value in candidates(directory, base, kind))
        if not values:
            continue
        newest = max(v[0] for v in values)
        # Retain all timestamp ties: Ansible names have only one-second precision.
        for created, _, path, before, kind in sorted(values):
            if created >= threshold or created == newest or path in seen:
                continue
            seen.add(path)
            if kind == 'directories' and not removable_tree(path, before.st_dev):
                skipped.append(str(path));continue
            planned.append((path, before, kind))
    for path, before, kind in planned:
        current = snapshot(path, kind == 'directories')
        if not current or identity(current) != identity(before) or path.resolve() != path:
            skipped.append(str(path));continue
        if check:
            continue
        try:
            if kind == 'directories':
                if not removable_tree(path, before.st_dev):
                    skipped.append(str(path));continue
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted.append(str(path))
        except FileNotFoundError:
            pass  # A concurrent successful prune is an idempotent no-op.
    return {'deleted': deleted, 'would_delete': [str(p) for p, _, _ in planned], 'skipped': skipped}


if __name__ == '__main__':
    print(json.dumps(prune(json.load(sys.stdin)), separators=(',', ':')))
