"""Focused publisher configuration regressions; no real services are contacted."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeIntegrations(unittest.TestCase):
    def test_beszel_rotates_only_managed_url_and_uses_quorum_patch(self):
        module = load('ntfy_settings', 'roles/desk_monitoring/files/ntfy_settings.py')
        hub = Mock()
        old = 'ntfy://:old@192.168.1.186:2586/desk-events?Scheme=http'
        new = old.replace(':old@', ':new@')
        settings = {'emails':['owner@example.org'], 'webhooks':[old, 'https://other.example/hook'], 'other':True}
        record = {'id':'native-settings-id', 'user':'native-user-id', 'settings':settings}
        hub.records.side_effect = lambda name: [{'id':'native-user-id','email':'admin@desk.cluster','role':'admin'}] if name == 'users' else [record]
        self.assertTrue(module.configure(hub, new))
        expected = dict(settings, webhooks=['https://other.example/hook',new])
        hub.request.assert_called_once_with('/api/collections/user_settings/records/native-settings-id','PATCH',{'settings':expected})
        record['settings'] = expected
        self.assertFalse(module.configure(hub, new))
        hub.request.side_effect = RuntimeError('quorum unavailable')
        with self.assertRaises(RuntimeError):module.configure(hub, old)
        hub.records.side_effect = lambda name: [{'id':'native-user-id','email':'admin@desk.cluster','role':'user'}] if name == 'users' else [record]
        with self.assertRaises(ValueError):module.configure(hub,new)

    def test_native_ha_package_preserves_yaml_and_limits_payload(self):
        module = load('component_config', 'roles/desk_homeassistant/files/component_config.py')
        sys.path.insert(0,str(ROOT/'roles/desk_homeassistant/files'))
        self.addCleanup(sys.path.pop,0)
        env = jinja2.Environment(undefined=jinja2.StrictUndefined,trim_blocks=True,lstrip_blocks=True)
        env.filters['to_json'] = json.dumps
        template = env.from_string((ROOT/'roles/desk_ntfy/templates/homeassistant-ntfy.yaml.j2').read_text())
        package = template.render(desk_ntfy_base_url='http://192.168.1.186:2586',ntfy_publishers={'homeassistant':'test-token'})
        value = yaml.safe_load(package)
        command = value['rest_command']['desk_ntfy']
        self.assertEqual(command['url'],'http://192.168.1.186:2586/desk-events')
        self.assertEqual(command['headers']['Authorization'],'Bearer test-token')
        self.assertLessEqual(len(env.from_string(command['payload']).render(message='😀'*2000).encode()),4096)
        self.assertNotIn('\n',env.from_string(command['headers']['Title']).render(title='one\r\ntwo'))
        trigger = value['automation'][0]['triggers'][0]
        self.assertEqual(trigger['trigger'],'persistent_notification')
        for update_type, expected in [('added',True),('updated',True),('removed',False),('current',False)]:
            with self.subTest(update_type=update_type):
                self.assertEqual(update_type in trigger['update_type'],expected)
        fixtures = [
            'homeassistant:\n  name: Desk',
            'default_config:\nautomation: !include automations.yaml\n',
            'homeassistant:\n    name: Desk\nautomation: !include automations.yaml\n',
            'homeassistant:\n  packages:\n    other: !include other.yaml\nsecret: !secret example\n',
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);source=root/'package';source.write_text(package)
            config=root/'configuration.yaml'
            for fixture in fixtures:
                config.write_text(fixture)
                module.configure_ntfy(root,source)
                first=config.read_text();module.configure_ntfy(root,source)
                self.assertEqual(first,config.read_text())
                self.assertEqual(first.count('desk_ntfy: !include desk_ntfy.yaml'),1)
                for line in fixture.splitlines():self.assertIn(line,first)
                self.assertEqual((root/'desk_ntfy.yaml').read_text(),package)
            for conflict in ['homeassistant:\n  packages: !include_dir_named packages\n','homeassistant:\n  packages:\n    desk_ntfy: !include user.yaml\n']:
                config.write_text(conflict)
                with self.assertRaises(ValueError):module.configure_ntfy(root,source)
                self.assertEqual(config.read_text(),conflict)


if __name__ == '__main__':unittest.main()
