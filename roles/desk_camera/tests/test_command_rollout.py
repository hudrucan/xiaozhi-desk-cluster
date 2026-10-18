"""Exercise the deployment's real isolation-guard CLI with fixture state."""
import ast
import contextlib
import io
import pathlib
import sys
import unittest
from unittest.mock import patch
import jinja2
import yaml

REPO=pathlib.Path(__file__).resolve().parents[3]
BOOK=yaml.safe_load((REPO/'camera-commands.yml').read_text())
FREEZE=next(t['ansible.builtin.command'] for p in BOOK for t in p.get('tasks',[]) if t.get('register')=='commands_guard')
GUARD=BOOK[-1]['tasks'][0]['ansible.builtin.command']
BASE={'units':{'nats-server':{'MainPID':'123','ActiveState':'active'}},'dns_policy':'a'*64}

def render(value,**scope):
    def lookup(kind,path):
        if kind!='file':raise ValueError('Unexpected lookup')
        return pathlib.Path(path).read_text().rstrip()
    return jinja2.Template(value).render(playbook_dir=str(REPO),lookup=lookup,**scope)

def invoke(spec,state,**scope):
    args=[render(v,**scope) for v in spec['argv']]
    tree=ast.parse(args[2])
    # Only I/O sampling is stubbed; CLI parsing and drift comparison run intact.
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='state')
    node.body=[ast.Return(value=ast.Name(id='_fixture_state',ctx=ast.Load()))]
    ast.fix_missing_locations(tree);out=io.StringIO()
    stdin=render(spec.get('stdin',''),**scope)
    with patch.object(sys,'argv',['guard-fixture']+args[3:]),patch.object(sys,'stdin',io.StringIO(stdin)),contextlib.redirect_stdout(out):
        exec(compile(tree,'ui_guard.py','exec'),{'__name__':'__main__','_fixture_state':state})
    return out.getvalue()

class RolloutGuardTests(unittest.TestCase):
    def test_real_deployment_freeze_and_guard_cli_round_trip(self):
        old=invoke(FREEZE,BASE)
        self.assertIn('unchanged',invoke(GUARD,BASE,commands_guard={'stdout':old}))
    def test_deployment_guard_still_rejects_protected_pid_drift(self):
        old=invoke(FREEZE,BASE);current=dict(BASE,units={'nats-server':{'MainPID':'456','ActiveState':'active'}})
        with self.assertRaisesRegex(SystemExit,'protected state changed'):
            invoke(GUARD,current,commands_guard={'stdout':old})
if __name__=='__main__':unittest.main()
