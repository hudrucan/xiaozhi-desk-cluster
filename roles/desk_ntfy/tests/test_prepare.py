"""Exercise real Git checkouts; redirect the upstream URL to a local fixture."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
PIN = '6f9c2e2bbad93819c0fd7bfe1c06230df5878721'


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ntfy-prepare-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_GLOBAL=str(base / 'gitconfig'))
        self.origin = base / 'origin'
        self.origin.mkdir()
        self.git(self.origin, 'init', '-q')
        for name in ('boundary', 'subscribe', 'other'):
            (self.origin / name).write_text('original\n')
        self.git(self.origin, 'add', '.')
        self.commit(self.origin)
        self.pin = self.git(self.origin, 'rev-parse', 'HEAD').strip()
        (self.origin / 'boundary').write_text('read-only auth\n')
        self.old = self.git(self.origin, 'diff', '--binary')
        (self.origin / 'subscribe').write_text('filter message ID\n')
        self.new = self.git(self.origin, 'diff', '--binary')
        self.git(self.origin, 'restore', '.')
        self.git(self.origin, 'config', '--global',
                 'url.' + self.origin.as_uri() + '.insteadOf',
                 'https://github.com/Arcturus808/ntfy-rs.git')
        self.repo = base / 'repo with spaces'
        self.src = self.repo / 'roles/desk_ntfy/runtime/upstream'
        self.patch = self.src.parent / 'upstream.patch'
        self.patch.parent.mkdir(parents=True)
        self.script = self.repo / 'scripts/prepare-ntfy.sh'
        self.script.parent.mkdir()
        self.script.write_text((ROOT / 'scripts/prepare-ntfy.sh').read_text().replace(PIN, self.pin))
        self.patch.write_text(self.old)
        (self.repo / 'tracked-parent').write_text('original\n')
        self.git(self.repo, 'init', '-q')
        self.git(self.repo, 'add', '.')
        self.commit(self.repo)
        self.patch.write_text(self.new)  # Current patch need not be committed yet.
        (self.repo / 'tracked-parent').write_text('unrelated local edit\n')
        (self.repo / 'unrelated-dirty').write_text('keep this\n')

    def git(self, directory, *args):
        return subprocess.run(['git', '-C', str(directory), *args], env=self.env,
                              check=True, capture_output=True, text=True).stdout

    def commit(self, directory):
        self.git(directory, '-c', 'user.name=Fixture', '-c',
                 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')

    def prepare(self, success=True):
        result = subprocess.run(['sh', str(self.script)], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        self.assertEqual((self.repo / 'unrelated-dirty').read_text(), 'keep this\n')
        self.assertEqual((self.repo / 'tracked-parent').read_text(), 'unrelated local edit\n')
        return result

    def snapshot(self):
        return ({str(p.relative_to(self.src)): p.read_bytes()
                 for p in self.src.rglob('*') if p.is_file()
                 and '.git' not in p.relative_to(self.src).parts},
                (self.src / '.git/index').read_bytes(),
                self.git(self.src, 'rev-parse', 'HEAD'))

    def assert_current(self):
        self.assertEqual(self.git(self.src, 'rev-parse', 'HEAD').strip(), self.pin)
        self.assertEqual(self.git(self.src, 'diff', '--cached'), '')
        self.assertEqual(self.git(self.src, 'diff', '--binary'), self.new)

    def old_checkout(self):
        self.patch.write_text(self.old)
        self.prepare()
        self.patch.write_text(self.new)

    def interrupt_and_retry(self, phase, signal):
        wrappers = Path(self.temp.name) / 'bin'
        wrappers.mkdir()
        real_git = shlex.quote(shutil.which('git'))
        wrapper = wrappers / 'git'
        wrapper.write_text(f'''#!/bin/sh
{real_git} "$@" || exit $?
for argument do
    if [ "$argument" = "${{INTERRUPT_PHASE:-}}" ]; then
        kill -{signal} "$PPID"
        exit 143
    fi
done
''')
        wrapper.chmod(0o755)
        self.env.update(PATH=str(wrappers) + os.pathsep + os.environ['PATH'],
                        INTERRUPT_PHASE=phase)
        self.prepare(False)
        self.assertFalse(os.path.lexists(self.src))
        abandoned = list(self.src.parent.glob('.upstream-prepare.*'))
        if signal == 'KILL':
            self.assertTrue(abandoned)
        else:
            self.assertEqual(abandoned, [])
        # An uncatchable interruption may leave staging behind. Retry must not
        # remove directories that could belong to another preparation process.
        for directory in abandoned:
            (directory / 'keep-me').write_text('unrelated staging\n')
        del self.env['INTERRUPT_PHASE']
        self.prepare()
        self.assert_current()
        self.prepare()
        for directory in abandoned:
            self.assertEqual((directory / 'keep-me').read_text(), 'unrelated staging\n')

    def test_interrupted_fetch_can_retry(self):
        self.interrupt_and_retry('fetch', 'KILL')

    def test_interrupted_checkout_can_retry(self):
        self.interrupt_and_retry('checkout', 'TERM')

    def test_invalid_fresh_patch_is_not_published_and_can_retry(self):
        self.patch.write_text('invalid patch\n')
        self.prepare(False)
        self.assertFalse(os.path.lexists(self.src))
        self.assertEqual(list(self.src.parent.glob('.upstream-prepare.*')), [])
        self.patch.write_text(self.new)
        self.prepare()
        self.assert_current()

    def test_legacy_incomplete_checkout_is_left_untouched(self):
        self.src.mkdir()
        self.git(self.src, 'init', '-q')
        (self.src / 'keep-me').write_text('unfinished local checkout\n')
        before = {str(p.relative_to(self.src)): p.read_bytes()
                  for p in self.src.rglob('*') if p.is_file()}
        self.prepare(False)
        self.assertEqual({str(p.relative_to(self.src)): p.read_bytes()
                          for p in self.src.rglob('*') if p.is_file()}, before)

    def test_destination_appearing_at_publication_is_not_overwritten(self):
        wrappers = Path(self.temp.name) / 'bin'
        wrappers.mkdir()
        wrapper = wrappers / 'mv'
        wrapper.write_text(f'''#!/bin/sh
mkdir {shlex.quote(str(self.src))}
printf 'local checkout\\n' > {shlex.quote(str(self.src / 'keep-me'))}
exec {shlex.quote(shutil.which('mv'))} "$@"
''')
        wrapper.chmod(0o755)
        self.env['PATH'] = str(wrappers) + os.pathsep + os.environ['PATH']
        self.prepare(False)
        self.assertEqual(list(self.src.iterdir()), [self.src / 'keep-me'])
        self.assertEqual((self.src / 'keep-me').read_text(), 'local checkout\n')
        self.assertEqual(list(self.src.parent.glob('.upstream-prepare.*')), [])

    def test_fresh_checkout_and_repeat_are_idempotent(self):
        self.prepare()
        self.assert_current()
        before = self.snapshot()
        modified = (self.src / 'subscribe').stat().st_mtime_ns
        self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.src / 'subscribe').stat().st_mtime_ns, modified)

    def test_existing_pristine_pinned_checkout(self):
        self.git(self.repo, 'clone', '-q', self.origin.as_uri(), str(self.src))
        self.prepare()
        self.assert_current()

    def test_migrates_repository_recorded_old_patch(self):
        self.old_checkout()
        self.prepare()
        self.assert_current()
        self.prepare()

    def test_rejects_local_edits_without_altering_checkout(self):
        for name in ('boundary', 'other'):
            with self.subTest(file=name):
                if not self.src.exists():
                    self.old_checkout()
                original = (self.src / name).read_bytes()
                (self.src / name).write_bytes(original + b'local edit\n')
                before = self.snapshot()
                self.assertIn('unexpected local upstream changes', self.prepare(False).stderr)
                self.assertEqual(self.snapshot(), before)
                (self.src / name).write_bytes(original)

    def test_rejects_staged_changes_without_altering_index(self):
        self.old_checkout()
        self.git(self.src, 'add', 'boundary')
        before = self.snapshot()
        self.prepare(False)
        self.assertEqual(self.snapshot(), before)

    def test_rejects_untracked_files_even_when_patch_is_current(self):
        self.prepare()
        (self.src / 'keep-me').write_text('local\n')
        before = self.snapshot()
        self.prepare(False)
        self.assertEqual(self.snapshot(), before)

    def test_rejects_wrong_revision(self):
        self.prepare()
        self.git(self.src, 'add', '.')
        self.commit(self.src)
        before = self.snapshot()
        self.prepare(False)
        self.assertEqual(self.snapshot(), before)

    def test_rejects_invalid_target_patch_without_changing_old_checkout(self):
        self.old_checkout()
        self.patch.write_text('invalid patch\n')
        before = self.snapshot()
        self.prepare(False)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
