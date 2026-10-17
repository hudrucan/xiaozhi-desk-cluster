#!/usr/bin/env python3
"""Summarise py-spy raw (collapsed-stack) profiles collected by diagnostics-profile.yml.

usage: profile-summary.py <dir> <duration_seconds> <rate_hz> [top_n]
Each raw line is `frame;frame;... samples`, root first. Self time is the leaf frame; inclusive time counts
a function once per stack. Active % is samples / (duration * rate): the share of wall time an active Python
thread was running, so it approximates single-thread CPU use (idle threads are not sampled).
"""
import collections, pathlib, re, sys

def parse(path):
    stacks = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        stack, _, count = line.rpartition(' ')
        stacks.append((stack.split(';'), int(count)))
    return stacks

def name(frame):
    return re.sub(r':\d+\)$', ')', frame)  # drop the line number so one function aggregates

def main():
    root, duration, rate = pathlib.Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    top = int(sys.argv[4]) if len(sys.argv) > 4 else 6
    expected = duration * rate
    for path in sorted(root.glob('*/*.txt')):
        stacks = parse(path)
        total = sum(c for _, c in stacks)
        self_t, incl = collections.Counter(), collections.Counter()
        for frames, count in stacks:
            self_t[name(frames[-1])] += count
            for f in set(map(name, frames)):
                incl[f] += count
        print(f'== {path.parent.name} / {path.stem}: {total} samples, active {100 * total / expected:.1f}% of wall time')
        if not total:
            continue
        print('   self:')
        for f, c in self_t.most_common(top):
            print(f'     {100 * c / total:5.1f}% {f}')
        print('   inclusive:')
        for f, c in incl.most_common(top):
            print(f'     {100 * c / total:5.1f}% {f}')

if __name__ == '__main__':
    main()
