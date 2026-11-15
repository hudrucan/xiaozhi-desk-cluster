"""Read-only journal preflight and eMMC collector helpers."""
import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT/file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


safety = load('journal_safety', 'playbooks/node/files/journal-safety.py')
emmc = load('emmc_health', 'scripts/emmc-health.py')


class JournalSafety(unittest.TestCase):
    def test_logrotate_headers_cover_exact_and_glob_patterns(self):
        output = '''rotating pattern: /var/log/syslog
/var/log/haproxy.log  daily (7 rotations)
empty log files are not rotated, old logs are removed
considering log /var/log/syslog
rotating pattern: /var/log/*.log  weekly (4 rotations)
empty log files are not rotated, old logs are removed
'''
        self.assertEqual(safety.rotation_coverage(output, ['/var/log/syslog','/var/log/haproxy.log','/var/log/kern.log']), [])
        self.assertEqual(safety.rotation_coverage('considering log /var/log/syslog', ['/var/log/syslog']), ['/var/log/syslog'])
        self.assertEqual(safety.rotation_coverage('rotating pattern: /var/log/haproxy.log  daily (7 rotations)\n', ['/var/log/haproxy.log.old']), ['/var/log/haproxy.log.old'])

    def test_counts_actual_json_records_without_exposing_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp)/'journalctl'
            fake.write_text('#!'+sys.executable+'\nprint(\'{"MESSAGE":"one"}\')\nprint(\'{"MESSAGE":"two"}\')\n')
            fake.chmod(0o755)
            with patch.dict(os.environ, PATH=temp+os.pathsep+os.environ['PATH']):
                self.assertEqual(safety.entries(), 2)
                fake.write_text('#!'+sys.executable+'\nraise SystemExit(3)\n')
                with self.assertRaisesRegex(ValueError, 'Cannot count'):
                    safety.entries()

    def test_size_follows_journal_symlink_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root/'journal'
            target.mkdir()
            (target/'fixture.journal').write_bytes(b'x'*4096)
            link = root/'linked'
            link.symlink_to(target)
            before = (target/'fixture.journal').read_bytes()
            self.assertEqual(safety.allocated(str(link)), safety.allocated(str(target)))
            self.assertEqual(before, (target/'fixture.journal').read_bytes())
            self.assertEqual(safety.allocated(str(root/'absent')), 0)
            target.rename(root/'moved')
            with self.assertRaisesRegex(ValueError, 'Broken'):
                safety.allocated(str(link))

    def test_emmc_sector_deltas_and_pid_reuse_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            device = root/'sys/block/mmcblk2'
            (device/'device').mkdir(parents=True)
            (device/'device/life_time').write_text('0x01 0x02')
            (device/'device/pre_eol_info').write_text('0x01')
            (device/'stat').write_text('1 2 3 4 5 6 100 8 9 10 11')
            proc = root/'proc/123'
            proc.mkdir(parents=True)
            (proc/'stat').write_text('123 (fixture name) '+' '.join(['0']*19+['99']))
            (proc/'comm').write_text('fixture')
            (proc/'io').write_text('write_bytes: 1000\ncancelled_write_bytes: 0\n')
            before = emmc.sample(root)
            (device/'stat').write_text('1 2 3 4 5 6 110 8 9 10 11')
            (proc/'io').write_text('write_bytes: 2000\ncancelled_write_bytes: 4\n')
            after = emmc.sample(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                emmc.display(before, after, 300)
            self.assertIn('delta_bytes=5120', output.getvalue())
            self.assertIn('1000', output.getvalue())
            # A reused PID must not contribute an unrelated process delta.
            after['processes'] = {('123','100'):('reused',9000,0)}
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                emmc.display(before, after, 300)
            self.assertNotIn('reused', output.getvalue())

    def test_emmc_missing_attributes_are_reported_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            result = emmc.sample(Path(temp))
            self.assertIsNone(result['life_time'])
            self.assertIsNone(result['sectors'])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                emmc.display(result, result, 300)
            self.assertIn('unavailable', output.getvalue())


if __name__ == '__main__':
    unittest.main()
