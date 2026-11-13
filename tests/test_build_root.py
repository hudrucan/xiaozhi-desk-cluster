"""Controller artifact and Go cache paths remain portable and compatible."""
import hashlib
from pathlib import Path
import subprocess
import unittest

from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILD = 'roles/desk_dashboard/tasks/build.yml'


class BuildRoot(unittest.TestCase):
    def setUp(self):
        self.tasks = yaml.safe_load((ROOT / BUILD).read_text())
        self.defaults = yaml.safe_load((ROOT / 'roles/desk_dashboard/defaults/main.yml').read_text())
        self.env = Environment(undefined=StrictUndefined)
        self.env.filters['hash'] = lambda value, algorithm: hashlib.new(algorithm, value.encode()).hexdigest()
        self.env.globals['lookup'] = lambda plugin, path: Path(path).read_text().rstrip()

    def paths(self, tasks, variables):
        variables = {'desk_repo_root': str(ROOT), **variables}
        artifact = self.env.from_string(tasks[0]['ansible.builtin.set_fact']['dashboard_artifact']).render(variables)
        cache = self.env.from_string(tasks[-1]['environment']['GOCACHE']).render(variables)
        return artifact, cache

    def test_default_matches_original_paths_even_without_role_defaults(self):
        # Pin the original build contract so this test survives future commits.
        before = yaml.safe_load(subprocess.check_output(
            ['git', 'show', 'd0fbcc93f5ffd3cc231c4e88c2848761c3ad71d6:' + BUILD], cwd=ROOT, text=True))
        expected = self.paths(before, {})
        self.assertEqual(self.paths(self.tasks, self.defaults), expected)
        # dashboard-helpers imports tasks directly, without loading role defaults.
        self.assertEqual(self.paths(self.tasks, {}), expected)

    def test_override_moves_both_paths_and_preserves_explicit_artifact_use(self):
        original = self.paths(self.tasks, self.defaults)
        root = '/tmp/desk-dashboard'
        actual = self.paths(self.tasks, {**self.defaults, 'desk_build_root': root})
        self.assertEqual(actual, tuple(path.replace('/private/tmp', root, 1) for path in original))
        self.assertNotIn('/private/tmp', ''.join(actual))
        # Extra vars keep priority over set_fact; consumers must use that artifact.
        variables = {'dashboard_artifact': '/tmp/prebuilt/desk-dashboard', 'desk_repo_root': str(ROOT)}
        build = self.tasks[-1]['ansible.builtin.command']
        self.assertEqual(self.env.from_string(build['creates']).render(variables), variables['dashboard_artifact'])
        output = build['argv'][build['argv'].index('-o') + 1]
        self.assertEqual(self.env.from_string(output).render(variables), variables['dashboard_artifact'])


# All controller paths with the same compatibility contract share these checks.
BASELINE = 'd0fbcc93f5ffd3cc231c4e88c2848761c3ad71d6'
PORTABLE_YAML = [
    'roles/desk_healthcheck/tasks/main.yml',
    'roles/desk_vpn/tasks/main.yml',
    'roles/desk_vpn_recovery/tasks/main.yml',
    'roles/desk_vpn_ui/tasks/main.yml',
    'roles/desk_https/defaults/main.yml',
    'roles/desk_ntfy/defaults/main.yml',
    'roles/desk_vpn_ui/defaults/main.yml',
    'playbooks/vpn/vpn-ui.yml',
]


def baseline(path):
    return subprocess.check_output(['git', 'show', BASELINE + ':' + path], cwd=ROOT, text=True)


def strings(value, position=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from strings(child, position + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from strings(child, position + (index,))
    elif isinstance(value, str):
        yield position, value


class PortableBuildRoots(unittest.TestCase):
    setUp = BuildRoot.setUp

    def test_all_ansible_defaults_match_baseline_and_root_moves_every_path(self):
        for name in PORTABLE_YAML:
            before = dict(strings(yaml.safe_load(baseline(name))))
            after = dict(strings(yaml.safe_load((ROOT / name).read_text())))
            variables = {'desk_repo_root': str(ROOT), 'role_path': str(ROOT / name.split('/tasks/')[0])}
            for position, original in before.items():
                if '/private/tmp' not in original:
                    continue
                with self.subTest(file=name, field=position):
                    expected = self.env.from_string(original).render(variables)
                    template = self.env.from_string(after[position])
                    self.assertEqual(template.render(variables), expected)
                    actual = template.render(variables, desk_build_root='/tmp/desk build')
                    self.assertEqual(actual, expected.replace('/private/tmp', '/tmp/desk build', 1))
                    self.assertNotIn('/private/tmp', actual)

    def test_shell_package_defaults_environment_and_explicit_argument(self):
        import os
        for name in ('scripts/build-caddy.sh', 'scripts/build-ntfy.sh'):
            def output(source, root=None, argument=None):
                # Execute only the actual output assignment, never prepare/build.
                assignment = next(line for line in source.splitlines() if line.startswith('out='))
                environment = dict(os.environ)
                environment.pop('DESK_BUILD_ROOT', None)
                if root is not None:
                    environment['DESK_BUILD_ROOT'] = root
                return subprocess.check_output(
                    ['sh', '-c', assignment + '\nprintf "%s" "$out"', 'fixture'] + ([argument] if argument else []),
                    env=environment, text=True)
            with self.subTest(file=name):
                source = (ROOT / name).read_text()
                expected = output(baseline(name))
                self.assertEqual(output(source), expected)
                self.assertEqual(output(source, ''), expected)
                actual = output(source, '/tmp/desk build')
                self.assertEqual(actual, expected.replace('/private/tmp', '/tmp/desk build', 1))
                self.assertNotIn('/private/tmp', actual)
                self.assertEqual(output(source, '/tmp/desk build', '/tmp/explicit package'), '/tmp/explicit package')

    def test_vpn_script_cli_defaults_environment_and_explicit_arguments(self):
        import argparse
        import ast
        import os
        from unittest.mock import patch
        name = 'scripts/build-vpn-ui.py'

        def options(source, root=None, arguments=()):
            function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == 'main')
            # main's first line parses CLI options; stop before clone/build/package.
            statements = []
            for statement in function.body:
                if statement.lineno != function.body[0].lineno:
                    break
                statements.append(statement)
            namespace = {'argparse': argparse, 'Path': Path, 'os': os, '__file__': str(ROOT / name)}
            with patch.dict(os.environ), patch('sys.argv', [name, *arguments]):
                os.environ.pop('DESK_BUILD_ROOT', None)
                if root is not None:
                    os.environ['DESK_BUILD_ROOT'] = root
                exec(compile(ast.Module(body=statements, type_ignores=[]), name, 'exec'), namespace)
            return namespace['a'].source, namespace['a'].out

        source = (ROOT / name).read_text()
        expected = options(baseline(name))
        self.assertEqual(options(source), expected)
        self.assertEqual(options(source, ''), expected)
        actual = options(source, '/tmp/desk build')
        self.assertEqual(actual, tuple(Path(str(path).replace('/private/tmp', '/tmp/desk build', 1)) for path in expected))
        self.assertNotIn('/private/tmp', str(actual))
        self.assertEqual(options(source, '/tmp/desk build', ['--source', '/tmp/source', '--out', '/tmp/output']),
                         (Path('/tmp/source'), Path('/tmp/output')))

    def test_self_hashed_builders_remain_byte_identical(self):
        for name in ('scripts/build-beszel-ui.py', 'scripts/build-ntfy-web.py'):
            with self.subTest(file=name):
                self.assertEqual((ROOT / name).read_text(), baseline(name))

    def test_fixtures_use_managed_temporary_directories(self):
        import contextlib
        import io
        import runpy
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            fixture = yaml.safe_load((ROOT / 'roles/desk_dns/tests/freeze_fixture.yml').read_text())[0]
            self.env.globals['lookup'] = lambda plugin, key: directory
            actual = self.env.from_string(fixture['vars']['ansible_remote_tmp']).render()
            self.assertEqual(Path(actual).parent, Path(directory))
            self.assertNotIn('/private/tmp', actual)
        created = []
        factory = tempfile.TemporaryDirectory

        def temporary(**kwargs):
            instance = factory(**kwargs)
            created.append(Path(instance.name))
            return instance

        with patch('tempfile.TemporaryDirectory', temporary), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / 'roles/desk_monitoring/tests/render_fixtures.py'))
        self.assertEqual(len(created), 2)
        self.assertTrue(all(not path.exists() for path in created))
        go = (ROOT / 'roles/desk_healthcheck/files/healthcheck_test.go').read_text()
        self.assertIn('directory := t.TempDir()', go)
        self.assertNotIn('/private/tmp', go)

    def test_documented_package_and_cache_examples_expand_consistently(self):
        import os
        import re
        for name in ('DESK_DASHBOARD', 'DESK_HTTPS', 'DESK_NTFY', 'DESK_VPN_UI'):
            path = 'docs/' + name + '.md'
            expressions = re.findall(r'\$\{DESK_BUILD_ROOT:-/private/tmp\}(/[^"\s`]*)', (ROOT / path).read_text())
            self.assertTrue(expressions, path)
            for suffix in expressions:
                original = '/private/tmp' + suffix
                with self.subTest(file=path, path=original):
                    self.assertIn(original, baseline(path))
                    expression = '${DESK_BUILD_ROOT:-/private/tmp}' + suffix
                    environment = dict(os.environ)
                    environment.pop('DESK_BUILD_ROOT', None)
                    command = ['sh', '-c', 'printf "%s" "' + expression + '"']
                    self.assertEqual(subprocess.check_output(command, env=environment, text=True), original)
                    environment['DESK_BUILD_ROOT'] = '/tmp/desk build'
                    actual = subprocess.check_output(command, env=environment, text=True)
                    self.assertEqual(actual, '/tmp/desk build' + suffix)
                    self.assertNotIn('/private/tmp', actual)
