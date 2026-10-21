#!/usr/bin/env python3
"""Interleaved, isolated checkpoint benchmark. Reads no production data."""
import argparse
import hashlib
import json
import math
import os
import pathlib
import platform
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zlib

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
FILES = REPO / 'roles/desk_network/files'
sys.path.insert(0, str(FILES))
REFERENCE = REPO / 'roles/desk_network/tests/python_worker_reference.py'


def read(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return None


def counters(path):
    raw = read(path)
    if raw is None:
        return None
    try:
        return {line.split()[0]: int(line.split()[1]) for line in raw.splitlines()}
    except (ValueError, IndexError):
        return {'raw': raw}


def digest(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def environment():
    return {
        'platform': platform.platform(), 'machine': platform.machine(), 'node': platform.node(),
        'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
        'zlib': zlib.ZLIB_RUNTIME_VERSION, 'loadavg': list(os.getloadavg()),
        'thermal_millicelsius': {p.parent.name: read(p) for p in pathlib.Path('/sys/class/thermal').glob('thermal_zone*/temp')},
        'cpu_frequency_khz': {p.parent.parent.name: read(p) for p in pathlib.Path('/sys/devices/system/cpu').glob('cpu[0-9]*/cpufreq/scaling_cur_freq')},
    }


def controller():
    if sys.platform != 'linux':
        return None
    result = subprocess.run(['systemctl', 'show', 'desk-network.service', '-p', 'MainPID', '-p', 'ControlGroup'], capture_output=True, text=True, timeout=5)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    cg = pathlib.Path('/sys/fs/cgroup') / values.get('ControlGroup', '').lstrip('/')
    pid = values.get('MainPID', '0')
    status = read('/proc/' + pid + '/status')
    rss = next((int(line.split()[1]) * 1024 for line in (status or '').splitlines() if line.startswith('VmRSS:')), None)
    return {'pid': int(pid), 'rss_bytes': rss, 'memory_current': read(cg / 'memory.current'),
            'memory_peak': read(cg / 'memory.peak'), 'memory_events': counters(cg / 'memory.events')}


def inside(spec_path):
    spec = json.loads(pathlib.Path(spec_path).read_text())
    results = []
    for step in spec['steps']:
        if step.get('sequence_blob'):
            pathlib.Path(step['stdin']).write_bytes(b'{"token":"synthetic-benchmark-token"}\n' + pathlib.Path(step['sequence_blob']).read_bytes())
        with open(step['stdin'], 'rb') if step['stdin'] else open(os.devnull, 'rb') as incoming, open(step['stdout'], 'wb') as outgoing:
            started = time.monotonic()
            child = subprocess.Popen(step['argv'], stdin=incoming, stdout=outgoing, stderr=subprocess.DEVNULL, start_new_session=True)
            hwm = 0
            io_last = None
            timed_out = False
            while True:
                status = read(f'/proc/{child.pid}/status')
                for line in (status or '').splitlines():
                    if line.startswith('VmHWM:'):
                        hwm = max(hwm, int(line.split()[1]) * 1024)
                io_last = counters(f'/proc/{child.pid}/io') or io_last
                pid, state, usage = os.wait4(child.pid, os.WNOHANG)
                if pid:
                    child.returncode = os.waitstatus_to_exitcode(state)
                    break
                if time.monotonic() - started > 50:
                    timed_out = True
                    os.killpg(child.pid, signal.SIGKILL)
                time.sleep(.005)
            peak = usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024)
            results.append({'wall_seconds': time.monotonic() - started,
                            'user_seconds': usage.ru_utime, 'system_seconds': usage.ru_stime,
                            'cpu_seconds': usage.ru_utime + usage.ru_stime,
                            'peak_rss_bytes': peak, 'vmhwm_bytes': hwm or None,
                            'io': io_last, 'input_blocks': usage.ru_inblock, 'output_blocks': usage.ru_oublock,
                            'returncode': child.returncode, 'timeout': timed_out})
            if child.returncode != step.get('expected_exit', 0):
                break
    destination = pathlib.Path(spec['result'])
    pending = destination.with_suffix('.tmp')
    pending.write_text(json.dumps(results))
    pending.replace(destination)


def measured(spec, work, isolated):
    spec_path, result = work / 'spec.json', work / 'measure.json'
    spec['result'] = str(result)
    spec_path.write_text(json.dumps(spec))
    before = controller()
    cond_before = environment()
    started = time.monotonic()
    unit = 'desk-checkpoint-bench-' + uuid.uuid4().hex + '.service'
    cg_metrics = None
    try:
        if isolated:
            subprocess.run(['systemd-run', '--quiet', '--unit=' + unit,
                            '--property=RemainAfterExit=yes', '--property=CPUQuota=50%',
                            '--property=MemoryMax=384M', '--property=TasksMax=8',
                            '--property=RuntimeMaxSec=185', '--property=TimeoutStopSec=2',
                            '--property=KillMode=control-group', '--property=Nice=19',
                            '--property=NoNewPrivileges=yes',
                            '--', sys.executable, str(HERE / 'bench.py'), '--inside', str(spec_path)], check=True, timeout=10)
            cg_path = subprocess.check_output(['systemctl', 'show', unit, '-p', 'ControlGroup', '--value'], text=True).strip()
            cg = pathlib.Path('/sys/fs/cgroup') / cg_path.lstrip('/')
            while not result.exists():
                state = subprocess.check_output(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'], text=True).strip()
                if state in ('failed', 'inactive') or time.monotonic() - started > 190:
                    break
                time.sleep(.05)
            # RemainAfterExit retains counters until they have been collected.
            memory_peak = read(cg / 'memory.peak')
            cg_metrics = {'memory_peak_bytes': int(memory_peak) if memory_peak is not None else None,
                          'memory_events': counters(cg / 'memory.events'),
                          'cpu_stat': counters(cg / 'cpu.stat'), 'io_stat': read(cg / 'io.stat'),
                          'cpu_max': read(cg / 'cpu.max'), 'memory_max': read(cg / 'memory.max')}
        else:
            subprocess.run([sys.executable, str(HERE / 'bench.py'), '--inside', str(spec_path)], check=True, timeout=190)
        steps = json.loads(result.read_text()) if result.exists() else []
        expected = len(spec['steps'])
        errors = sum(s['returncode'] != v.get('expected_exit', 0) for s, v in zip(steps, spec['steps']))
        if len(steps) != expected and not errors:
            errors = 1
        return {'steps': steps, 'wall_seconds': sum(s['wall_seconds'] for s in steps) if steps else None,
                'user_seconds': sum(s['user_seconds'] for s in steps) if steps else None,
                'system_seconds': sum(s['system_seconds'] for s in steps) if steps else None,
                'cpu_seconds': sum(s['cpu_seconds'] for s in steps) if steps else None,
                'peak_rss_bytes': max((s['peak_rss_bytes'] for s in steps), default=None),
                'cgroup': cg_metrics, 'errors': errors,
                'controller_before': before, 'controller_after': controller(),
                'conditions_before': cond_before, 'conditions_after': environment()}
    finally:
        if isolated:
            subprocess.run(['systemctl', 'stop', unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            subprocess.run(['systemctl', 'reset-failed', unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def dataset(root, mib, entropy):
    data = root / 'data'
    (data / 'db').mkdir(parents=True)
    (data / 'config').mkdir()
    (data / 'config/app.conf').write_bytes(b"# isolated benchmark fixture\nARPSCAN_RUN_SCHD='*/5 * * * *'\n")
    (data / 'config/workflows.json').write_bytes(b'[]')
    # Deterministic mixture of repetitive and pseudo-random data; never production data.
    import random
    generator = random.Random(3528)
    with sqlite3.connect(data / 'db/app.db') as c:
        c.execute('CREATE TABLE bulk(data BLOB)')
        for i in range(mib * 16):
            c.execute('INSERT INTO bulk VALUES (?)', (generator.randbytes(65536) if i % 100 < entropy else bytes(65536),))
    return data


def quantile(values, q):
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)]


def summary(rows):
    groups = {}
    for row in rows:
        if not row['warmup']:
            groups.setdefault((row['mib'], row['operation'], row['engine']), []).append(row)
    out = []
    for (mib, operation, engine), runs in groups.items():
        metrics = {}
        for key in ('wall_seconds', 'cpu_seconds', 'user_seconds', 'system_seconds', 'peak_rss_bytes'):
            values = [r[key] for r in runs if r[key] is not None]
            metrics[key] = {'p50': quantile(values, .5), 'p95': quantile(values, .95), 'max': max(values)} if values else None
        peaks = [r['cgroup']['memory_peak_bytes'] for r in runs if r['cgroup'] and r['cgroup']['memory_peak_bytes'] is not None]
        metrics['cgroup_memory_peak_bytes'] = {'p50': quantile(peaks, .5), 'p95': quantile(peaks, .95), 'max': max(peaks)} if peaks else None
        events = [r['cgroup']['memory_events'] for r in runs if r['cgroup'] and r['cgroup']['memory_events'] is not None]
        oom_kills = sum(e.get('oom_kill', 0) for e in events) if len(events) == len(runs) else None
        out.append({'mib': mib, 'operation': operation, 'engine': engine, 'runs': len(runs),
                    'errors': sum(r['errors'] for r in runs),
                    'oom_kills': oom_kills, 'metrics': metrics})
    comparisons = []
    for py in out:
        if py['engine'] != 'python':
            continue
        rust = next((r for r in out if r['engine'] == 'rust' and r['mib'] == py['mib'] and r['operation'] == py['operation']), None)
        if rust is None:
            continue
        difference = {}
        for metric, pv in py['metrics'].items():
            rv = rust['metrics'][metric]
            difference[metric] = {q: (rv[q] / pv[q] - 1) * 100 if pv[q] else None for q in pv} if pv and rv else None
        comparisons.append({'mib': py['mib'], 'operation': py['operation'], 'rust_vs_python_percent': difference})
    return {'groups': out, 'comparisons': comparisons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', help=argparse.SUPPRESS)
    parser.add_argument('--binary', type=pathlib.Path)
    parser.add_argument('--output', type=pathlib.Path)
    parser.add_argument('--scratch', type=pathlib.Path, default=pathlib.Path(tempfile.gettempdir()))
    parser.add_argument('--sizes', default='1,10,25,40')
    parser.add_argument('--operations', default='create,validate,restore,full,cycle,invalid')
    parser.add_argument('--runs', type=int, default=20)
    parser.add_argument('--warmups', type=int, default=2)
    parser.add_argument('--entropy-percent', type=int, default=10)
    parser.add_argument('--resume', action='store_true', help='Continue only missing cells of a compatible saved run')
    parser.add_argument('--systemd', action='store_true', help='Require root, Linux systemd and cgroup v2; no unbounded fallback')
    args = parser.parse_args()
    if args.inside:
        inside(args.inside)
        return
    if not args.binary or not args.output:
        parser.error('--binary and --output are required')
    if args.systemd and (os.geteuid() != 0 or not pathlib.Path('/sys/fs/cgroup/cgroup.controllers').is_file()):
        parser.error('--systemd requires root and cgroup v2')
    if args.runs < 1 or args.warmups < 0 or not 0 <= args.entropy_percent <= 100:
        parser.error('invalid run counts or entropy')
    import ha
    binary = args.binary.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'host': environment(), 'isolation': 'systemd CPUQuota=50% MemoryMax=384M' if args.systemd else 'offline unlimited; not an RK3528 acceptance benchmark',
              'measurement_note': 'Worker peak RSS is wait4 max including descendants (largest process, not concurrent sum). cgroup includes identical Python measurement wrapper and all worker/helper/scratch pages. VmHWM and /proc IO sampled at 5ms; missing data is null.',
              'binary_sha256': digest(binary), 'source_sha256': {str(p.relative_to(REPO)): digest(p) for p in [HERE / 'bench.py', HERE / 'bridge.py', FILES / 'ha.py', FILES / 'checkpoint_budget.py', REFERENCE]},
              'entropy_percent': args.entropy_percent, 'runs': args.runs, 'warmups': args.warmups, 'rows': []}
    collector_sha = digest(HERE / 'bench.py')
    if args.resume:
        previous = json.loads(args.output.read_text())
        for key in ('binary_sha256', 'isolation', 'entropy_percent', 'runs', 'warmups'):
            if previous[key] != report[key]:
                parser.error('resume environment/settings mismatch: ' + key)
        for key in ('platform', 'machine', 'python', 'sqlite', 'zlib', 'node'):
            if key in previous['host'] and previous['host'][key] != report['host'][key]:
                parser.error('resume host/runtime mismatch: ' + key)
        for path, value in report['source_sha256'].items():
            if path != 'tools/desk-checkpoint/bench.py' and previous['source_sha256'].get(path) != value:
                parser.error('resume worker/helper source mismatch')
        if any(r['errors'] for r in previous['rows']):
            parser.error('resume refuses a run with worker failures')
        original_collector = previous['source_sha256']['tools/desk-checkpoint/bench.py']
        for row in previous['rows']:
            row.setdefault('harness_sha256', original_collector)
        report = previous
        report.setdefault('resume_events', []).append({'at_unix': time.time(),
            'harness_sha256': collector_sha, 'node': platform.node(), 'completed_rows': len(report['rows'])})
    completed = {(r['mib'], r['operation'], r['engine'], r['index']) for r in report['rows']}
    with tempfile.TemporaryDirectory(prefix='desk-checkpoint-bench-', dir=args.scratch) as temp:
        base = pathlib.Path(temp)
        for mib in map(int, args.sizes.split(',')):
            root = base / str(mib)
            root.mkdir()
            data = dataset(root, mib, args.entropy_percent)
            fixture = root / 'checkpoint.gz'
            fixture.write_bytes(ha.checkpoint(data, root / 'fixture-run'))
            restore_input = root / 'restore.stdin'
            restore_input.write_bytes(ha.encoded({'token': 'synthetic-benchmark-token'}) + b'\n' + fixture.read_bytes())
            invalid = root / 'invalid.gz'
            invalid.write_bytes(b'invalid synthetic checkpoint')
            python = [sys.executable, str(REFERENCE)]
            rust = [str(binary), '--bridge', str(HERE / 'bridge.py'), '--ha', str(FILES / 'ha.py'), '--python', sys.executable]
            # Operation first is also accepted by Rust; Python uses its existing flag.
            commands = {'python': python, 'rust': rust}
            expected = digest(fixture)
            if any(r['mib'] == mib and r['sha256'] != expected for r in report['rows']):
                parser.error('resume dataset digest mismatch')
            for operation in args.operations.split(','):
                if operation not in ('create', 'validate', 'restore', 'full', 'cycle', 'invalid'):
                    parser.error('unknown operation')
                for index in range(args.warmups + args.runs):
                    engines = ('python', 'rust') if index % 2 == 0 else ('rust', 'python')
                    for engine in engines:
                        if (mib, operation, engine, index) in completed:
                            continue
                        with tempfile.TemporaryDirectory(prefix='run-', dir=root) as run_temp:
                            work = pathlib.Path(run_temp)
                            target = root / ('cycle-' + engine) if operation == 'cycle' else work / 'target'
                            steps = []
                            output = work / 'created.gz'
                            for action in ('create', 'validate', 'restore') if operation in ('full', 'cycle') else (operation,):
                                actual = 'validate' if action == 'invalid' else action
                                argv = [*commands[engine][:2], '--snapshot-worker', actual, *commands[engine][2:]] if engine == 'python' else [str(binary), '--snapshot-worker', actual, *rust[1:]]
                                scratch = work / ('scratch-' + action)
                                argv += ['--run', str(scratch)]
                                if actual != 'validate':
                                    argv += ['--data', str(data if actual == 'create' else target)]
                                # Tests/benchmarks run on private copies, with real ownership syscalls.
                                if engine == 'rust' and os.geteuid() != 0:
                                    argv += ['--owner', f'{os.getuid()}:{os.getgid()}']
                                incoming = None if actual == 'create' else invalid if action == 'invalid' else restore_input if actual == 'restore' else output if operation in ('full', 'cycle') else fixture
                                if actual == 'restore' and operation in ('full', 'cycle'):
                                    # Header + created blob is assembled by the measurement wrapper immediately before restore.
                                    incoming = work / 'sequence.stdin'
                                steps.append({'argv': argv, 'stdin': str(incoming) if incoming else None,
                                              'stdout': str(output if actual == 'create' else work / (action + '.out')),
                                              'expected_exit': 1 if action == 'invalid' else 0,
                                              'sequence_blob': str(output) if actual == 'restore' and operation in ('full', 'cycle') else None})
                            # Python chown default is intentionally retained. Run Linux benchmarks as root.
                            result = measured({'steps': steps}, work, args.systemd)
                            produced = digest(output) if output.exists() else None
                            if produced is not None and produced != expected:
                                raise RuntimeError('digest mismatch; performance evaluation aborted')
                            if operation in ('restore', 'full', 'cycle') and result['errors'] == 0:
                                expected_db, expected_conf, expected_workflows = ha.unpack(fixture.read_bytes())
                                restored = target / 'db/app.db'
                                result['restored_database_sha256'] = digest(restored)
                                if result['restored_database_sha256'] != hashlib.sha256(expected_db).hexdigest():
                                    raise RuntimeError('restore database mismatch')
                                if (target / 'config/app.conf').read_bytes() != ha.apply_policy(expected_conf, 'synthetic-benchmark-token'):
                                    raise RuntimeError('restore policy mismatch')
                                if (target / 'config/workflows.json').read_bytes() != expected_workflows:
                                    raise RuntimeError('restore workflow mismatch')
                                del expected_db, expected_conf, expected_workflows
                            result.update({'harness_sha256': collector_sha, 'mib': mib, 'database_bytes': (data / 'db/app.db').stat().st_size,
                                           'operation': operation, 'engine': engine, 'index': index,
                                           'warmup': index < args.warmups, 'output_bytes': output.stat().st_size if output.exists() else fixture.stat().st_size,
                                           'sha256': produced or expected, 'scratch_files_after_worker': sum(1 for p in work.glob('scratch-*/*'))})
                            report['rows'].append(result)
                            report['summary'] = summary(report['rows']) if all(any(r['engine'] == e and not r['warmup'] for r in report['rows']) for e in ('python', 'rust')) else None
                            args.output.write_text(json.dumps(report, indent=2) + '\n')
                            print(f'{mib}MiB {operation} {engine} {index}: wall={result["wall_seconds"]} errors={result["errors"]}', flush=True)
                            if result['errors']:
                                raise RuntimeError('correctness/reliability failure; performance evaluation aborted')
    report['summary'] = summary(report['rows'])
    report['scratch_clean'] = not pathlib.Path(temp).exists()
    args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
