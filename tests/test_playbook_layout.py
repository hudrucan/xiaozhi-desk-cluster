"""Offline checks for entrypoint, inventory and controller path resolution."""
import unittest
from pathlib import Path

import yaml
from ansible import constants
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible.vars.manager import VariableManager

ROOT = Path(__file__).resolve().parents[1]


class PlaybookLayoutTests(unittest.TestCase):
    def inventory_vars(self, inventory, hostname):
        loader = DataLoader()
        loader.set_basedir(str(ROOT))
        inventory = InventoryManager(loader=loader, sources=[str(ROOT / inventory)])
        variables = VariableManager(loader=loader, inventory=inventory)
        return loader, variables.get_vars(host=inventory.get_host(hostname))

    def test_root_entrypoints_and_role_search(self):
        self.assertEqual({p.name for p in ROOT.glob('*.yml')}, {'site.yml', 'bootstrap.yml'})
        self.assertIn(str(ROOT / 'roles'), constants.DEFAULT_ROLES_PATH)

    def test_controller_delegation_keeps_original_inventory_root(self):
        loader, values = self.inventory_vars('inventory.ini', 'deskb1x')
        # A delegated localhost has no inventory_dir. Resolve from the original host.
        variables = {
            'inventory_hostname': 'deskb1x',
            'hostvars': {'deskb1x': {'inventory_dir': values['inventory_dir']}},
            'desk_repo_root': values['desk_repo_root'],
        }
        result = Templar(loader=loader, variables=variables).template(values['desk_repo_root'])
        self.assertEqual(Path(result), ROOT)

    def test_pi_inventory_discovers_adjacent_host_vars(self):
        _, values = self.inventory_vars('playbooks/pi-panel/inventory.ini', 'zerow')
        expected = yaml.safe_load((ROOT / 'playbooks/pi-panel/host_vars/zerow.yml').read_text())
        for key, value in expected.items():
            self.assertEqual(values[key], value, key)
        self.assertEqual(values['desk_pi_panel_enabled'], 'true')
        self.assertNotIn('deskboxes', values['group_names'])

    def test_static_imports_and_vault_paths_exist(self):
        def check(value, parent):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('ansible.builtin.import_playbook', 'ansible.builtin.import_tasks'):
                        self.assertIsInstance(item, str)
                        self.assertNotIn('{{', item, 'Static imports must resolve before host variables')
                        self.assertTrue((parent / item).is_file(), (parent, item))
                    elif key == 'vars_files':
                        for filename in item:
                            self.assertTrue((parent / filename).is_file(), (parent, filename))
                    check(item, parent)
            elif isinstance(value, list):
                for item in value:
                    check(item, parent)

        for path in [ROOT / 'site.yml', ROOT / 'bootstrap.yml', *ROOT.joinpath('playbooks').rglob('*.yml')]:
            with self.subTest(path=path.relative_to(ROOT)):
                check(yaml.safe_load(path.read_text()), path.parent)


if __name__ == '__main__':
    unittest.main()
