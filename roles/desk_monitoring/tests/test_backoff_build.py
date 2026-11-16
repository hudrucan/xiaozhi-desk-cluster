"""The pinned builder must invalidate its artifact cache when the new patch changes."""
import hashlib
from pathlib import Path
import runpy
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]


class BackoffBuild(unittest.TestCase):
    def test_patch_is_applied_tested_and_part_of_the_cache_identity(self):
        builder = runpy.run_path(str(ROOT / 'scripts/build-beszel-ui.py'))['build']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'source.tar.gz'
            source = root / 'source' / 'beszel-0.21.0'
            source.mkdir(parents=True)
            with tarfile.open(archive, 'w:gz') as tar:
                tar.add(source, arcname=source.name)
            backoff = root / 'beszel-backoff.patch'
            backoff.write_bytes((ROOT / 'roles/desk_monitoring/files/beszel-backoff.patch').read_bytes())
            builder.__globals__.update(SOURCE_SHA256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                                       BACKOFF_PATCH=backoff)
            def download(url, target):
                Path(target).write_bytes(archive.read_bytes())
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                if argv[:2] == ['go', 'build']:
                    Path(argv[argv.index('-o') + 1]).write_bytes(b'fixture binary')
            with patch.dict('os.environ', DESK_BUILD_ROOT=str(root / 'cache')), patch('urllib.request.urlretrieve', download), patch('subprocess.run', run):
                first = builder('linux', 'arm64')
                applied = [Path(argv[-1]).name for argv in calls if argv[0] == 'patch']
                self.assertEqual(applied, ['beszel-ui.patch', 'beszel-ntfy.patch', 'beszel-backoff.patch'])
                test = next(i for i, argv in enumerate(calls) if argv[:3] == ['go', 'test', './internal/hub/systems'])
                build = next(i for i, argv in enumerate(calls) if argv[:2] == ['go', 'build'])
                self.assertLess(test, build)
                count = len(calls)
                self.assertEqual(builder('linux', 'arm64'), first)
                self.assertEqual(len(calls), count)
                backoff.write_bytes(backoff.read_bytes() + b'\n')
                second = builder('linux', 'arm64')
                self.assertNotEqual(first, second)
                self.assertGreater(len(calls), count)
