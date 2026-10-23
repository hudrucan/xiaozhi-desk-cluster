"""Offline composition, source/origin boundaries and safe opt-in deployment."""
import ast
import base64
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


proxy = load('https_composer', 'roles/desk_monitoring/files/install_proxy.py')
ha = load('https_ha_composer', 'roles/desk_homeassistant/files/install_proxy.py')
probe = load('https_probe', 'roles/desk_https/files/probe.py')


def render(node='deskb1x'):
    nodes = ['deskb1x','deskb2x','deskb3x']
    values = yaml.safe_load((ROOT/'roles/desk_https/defaults/main.yml').read_text())
    values.update(yaml.safe_load((ROOT/'group_vars/deskboxes.yml').read_text()))
    values.update(groups={'deskboxes': nodes, 'xiaozhi_ingress': nodes, 'desk_monitoring': nodes},
        hostvars={n: {'cluster_ip': f'10.10.10.{11+i}', 'ansible_host':f'192.168.1.{101+i}'} for i,n in enumerate(nodes)},
        inventory_hostname=node, desk_repo_root=str(ROOT), cluster_ip=f'10.10.10.{11+nodes.index(node)}',
        desk_https_acme_email='fixture@example.com', xiaozhi_ingress_vip='192.168.1.186', xiaozhi_ingress_port=80,
        xiaozhi_ingress_stats_socket='/run/xiaozhi-haproxy.sock', xiaozhi_ingress_backend_port=8004, desk_dns_vip='192.168.1.250', desk_ntfy_vip='192.168.1.186')
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    env.filters['to_json'] = json.dumps
    env.filters['b64decode'] = lambda v:base64.b64decode(v).decode()
    def template(path): return env.from_string((ROOT/path).read_text()).render(values)
    values['lookup'] = lambda kind,path:template(Path(path))
    values['xiaozhi_ntfy_fragment'] = {'content':base64.b64encode(template('roles/desk_ntfy/templates/ingress.cfg.j2').encode()).decode()}
    return template('roles/xiaozhi_ingress/templates/haproxy.cfg.j2'), template('roles/desk_https/templates/ingress.cfg.j2'), template('roles/desk_https/templates/Caddyfile.j2')


class HTTPS(unittest.TestCase):
    def test_local_bindings_keep_source_acls_and_do_not_publish_private_ports(self):
        for node in ['deskb1x','deskb2x','deskb3x']:
            current, fragment, caddy = render(node)
            updated = proxy.https_candidate(current,fragment)
            self.assertEqual(proxy.https_candidate(updated),updated)
            self.assertEqual(proxy.https_candidate(updated,''),current.rstrip()+'\n')
            old_acls = [l for l in current.splitlines() if 'deny unless { src ' in l]
            self.assertEqual([l for l in updated.splitlines() if 'deny unless { src ' in l], old_acls)
            self.assertEqual(updated.count('mode 600 accept-proxy'),3)
            self.assertIn('path / /api/cluster/status /api/cluster/panel }',updated)
            self.assertIn('use_backend desk_vpn_ui if { path_beg /vpn/ }',updated)
            self.assertEqual(caddy.count('reverse_proxy unix//run/haproxy/desk-https-'),3)
            for private in [':8000',':8004',':8007',':1986',':20226',':3002',':51855']:
                self.assertNotIn(private,caddy)
            self.assertLess(caddy.index('desk_proxy_v2'),caddy.index('proxy_protocol {'))
            self.assertLess(caddy.index('proxy_protocol {'),caddy.index('\n\t\t\ttls'))
            self.assertIn('fallback_policy reject',caddy)
            self.assertEqual(fragment.count('send-proxy-v2 check-send-proxy check-ssl'),9)
            self.assertEqual(fragment.count('verify required verifyhost '),9)
            self.assertNotIn(' ssl ',fragment)
            self.assertIn('auto_https disable_redirects',caddy)
            self.assertIn('protocols h1 h2',caddy)

    def test_every_composer_retains_https_and_preparation_never_withdraws_it(self):
        current,fragment,_=render()
        active=proxy.https_candidate(current,fragment)
        backend='backend desk_monitoring_hubs\n    server one 10.10.10.11:8091\n'
        first=proxy.candidate(active,backend)
        self.assertEqual(proxy.candidate(first,backend),first)
        self.assertEqual(proxy.candidate(first,backend,https_config=proxy.HTTPS_PREPARED),first)
        self.assertEqual(proxy.https_fragment(proxy.full_candidate(active,current)),fragment)
        ha_fragment=current.split('# BEGIN DESK HOME ASSISTANT\n')[1].split('# END DESK HOME ASSISTANT\n')[0]
        replaced=ha.candidate(active,ha_fragment)
        self.assertEqual(proxy.https_fragment(replaced),fragment)
        self.assertEqual(replaced.count('desk-https-ha.sock'),1)
        self.assertEqual(ha.candidate(replaced,ha_fragment),replaced)
        prepared=proxy.https_candidate(current,proxy.HTTPS_PREPARED)
        self.assertNotIn('bind 192.168.1.186:443',prepared)
        self.assertEqual(proxy.https_candidate(prepared),prepared)

    def test_malformed_activation_and_missing_upstream_fail_closed(self):
        current,fragment,_=render()
        for bad in [fragment+fragment, '# BEGIN DESK HTTPS\n', '# END DESK HTTPS\n']:
            with self.assertRaises(ValueError):proxy.https_candidate(current,bad)
        with self.assertRaises(ValueError):proxy.https_candidate(current.replace('frontend desk_ntfy_vip\n','frontend unexpected\n'),fragment)

    def test_no_public_mutation_by_default_and_no_secret_or_election_replacement(self):
        defaults=yaml.safe_load((ROOT/'roles/desk_https/defaults/main.yml').read_text())
        self.assertEqual(defaults['desk_https_phase'],'preflight')
        self.assertFalse(defaults['desk_https_apply_approved'])
        playbook=yaml.safe_load((ROOT/'playbooks/https/caddy.yml').read_text())
        self.assertNotIn('serial',playbook[0]);self.assertEqual(playbook[1]['serial'],1)
        self.assertNotIn('desk_https',(ROOT/'site.yml').read_text())
        tasks='\n'.join(p.read_text() for p in (ROOT/'roles/desk_https/tasks').glob('*.yml'))
        for forbidden in ['sysctl -w','keepalived.conf','adguard-home.yaml','redis','consul']:
            self.assertNotIn(forbidden,tasks)
        self.assertNotIn('base-url',(ROOT/'roles/desk_https/templates/Caddyfile.j2').read_text())

    def test_approved_origins_and_spoofed_host_or_scheme(self):
        nc=load('https_node_control','roles/desk_node_control/files/node_control.py')
        legacy={'Host':'192.168.1.186','Origin':'http://192.168.1.186'}
        secure={'Host':'cluster.huynm.dev','Origin':'https://cluster.huynm.dev','X-Forwarded-Proto':'https'}
        self.assertTrue(nc.same_origin(legacy,'192.168.1.186'))
        self.assertTrue(nc.same_origin(secure,'192.168.1.186'))
        for bad in [{**secure,'Host':'evil.example'},{**secure,'Origin':'http://cluster.huynm.dev'},{**secure,'X-Forwarded-Proto':'http'},{}]:
            self.assertFalse(nc.same_origin(bad,'192.168.1.186'))
        # Load only the standalone origin predicates, without network runtime workers.
        tree=ast.parse((ROOT/'roles/desk_network/files/ha.py').read_text())
        scope={};exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ['https_origin','allowed_origin']],type_ignores=[]),'origins','exec'),scope)
        self.assertTrue(scope['allowed_origin'](legacy));self.assertTrue(scope['allowed_origin'](secure))
        self.assertFalse(scope['allowed_origin']({**secure,'Host':'evil.example'}))
        self.assertFalse(scope['allowed_origin']({**secure,'Origin':'https://evil.example'}))

    def test_validation_or_reload_failure_retains_entire_http_mqtt_https_configuration(self):
        current,fragment,_=render()
        active=proxy.https_candidate(current,fragment)
        for failure in ['validate','reload','composition']:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                main=Path(tmp)/'haproxy.cfg';mqtt=Path(tmp)/'xiaozhi-mqtt.cfg'
                main.write_text(active);mqtt.write_text('mqtt sentinel\n')
                def mapped(value):
                    return main if value=='/etc/haproxy/haproxy.cfg' else mqtt if value=='/etc/haproxy/xiaozhi-mqtt.cfg' else Path(value)
                unit=f'ExecStart=/usr/sbin/haproxy -f {main} -f {mqtt}\nExecReload=/usr/sbin/haproxy -c -f {main} -f {mqtt}\n'
                if failure=='composition':unit=unit.split('ExecReload=')[0]+'ExecReload=unsafe\n'
                reloads=[]
                def command(argv,**kwargs):
                    if '-c' in argv and failure=='validate':raise subprocess.CalledProcessError(1,argv)
                    if argv==['systemctl','reload','haproxy']:
                        reloads.append(argv)
                        if failure=='reload' and len(reloads)==1:raise subprocess.CalledProcessError(1,argv)
                    return subprocess.CompletedProcess(argv,0)
                with patch.object(proxy,'Path',side_effect=mapped), patch.object(proxy.subprocess,'check_output',return_value=unit), patch.object(proxy.subprocess,'run',side_effect=command):
                    with self.assertRaises((ValueError,subprocess.CalledProcessError)):
                        proxy.install('backend desk_monitoring_hubs\n    server changed 10.10.10.12:8091\n')
                self.assertEqual(main.read_text(),active)
                self.assertEqual(mqtt.read_text(),'mqtt sentinel\n')
                self.assertEqual(len(reloads),2 if failure=='reload' else 0)


if __name__=='__main__':unittest.main()
