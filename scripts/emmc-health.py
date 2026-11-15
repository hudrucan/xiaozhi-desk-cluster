#!/usr/bin/env python3
"""Read-only eMMC and per-process write deltas; output stays on stdout."""
import argparse
from pathlib import Path
import time


def read(path):
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return None


def sample(root=Path('/'), device='mmcblk2'):
    base = root/'sys/block'/device
    stat = (read(base/'stat') or '').split()
    processes = {}
    unavailable = 0
    for directory in (root/'proc').glob('[0-9]*'):
        try:
            # PID plus start time distinguishes a reused PID; never read cmdline.
            start = directory.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19]
            fields = dict(line.split(':', 1) for line in directory.joinpath('io').read_text().splitlines())
            processes[(directory.name, start)] = (read(directory/'comm') or '?',
                                                 int(fields['write_bytes']), int(fields['cancelled_write_bytes']))
        except (OSError, ValueError, IndexError, KeyError):
            unavailable += 1
    return dict(life_time=read(base/'device/life_time'), pre_eol_info=read(base/'device/pre_eol_info'),
                sectors=int(stat[6]) if len(stat)>6 else None, processes=processes, unavailable=unavailable)


def display(before, after, elapsed):
    print('eMMC mmcblk2 | life_time='+str(after['life_time'] or 'unavailable')+
          ' | pre_eol_info='+str(after['pre_eol_info'] or 'unavailable'))
    first, last = before['sectors'], after['sectors']
    delta = (last-first)*512 if first is not None and last is not None and last>=first else None
    total = last*512 if last is not None else None
    print(f'Sector field 7 x 512: total_bytes={total} delta_bytes={delta} elapsed_s={elapsed:.1f}')
    print('PID      COMM                     WRITE_DELTA_BYTES  CANCELLED_DELTA_BYTES')
    rows = []
    for key, (comm, written, cancelled) in after['processes'].items():
        previous = before['processes'].get(key)
        if previous is not None and written>=previous[1] and cancelled>=previous[2]:
            rows.append((written-previous[1], cancelled-previous[2], key[0], comm))
    for written, cancelled, pid, comm in sorted(rows, reverse=True)[:10]:
        safe = ''.join(c if c.isprintable() else '?' for c in comm)[:24]
        print(f'{pid:<8} {safe:<24} {written:>17} {cancelled:>22}')
    print(f'Unreadable/vanished processes: {before["unavailable"]}/{after["unavailable"]}. '
          'Only surviving PID/start-time pairs counted; process totals do not equal device writes.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interval', type=float, default=300, help='seconds between in-memory samples (default: five minutes)')
    args = parser.parse_args()
    if args.interval<0:
        parser.error('interval must be nonnegative')
    before = sample()
    start = time.monotonic()
    time.sleep(args.interval)
    after = sample()
    display(before, after, time.monotonic()-start)


if __name__ == '__main__':
    main()
