"""Run against the native test binary built from the same locked Go module."""
import json
import os
import subprocess
import tempfile
import unittest
from test_https import render


@unittest.skipUnless(os.environ.get('CADDY_BIN'), 'Set CADDY_BIN to the native test build')
class Binary(unittest.TestCase):
    def test_actual_adapter_and_module_provisioning(self):
        _,_,config=render()
        storage=tempfile.TemporaryDirectory(prefix='caddy-validate-')
        self.addCleanup(storage.cleanup)
        env={**os.environ,'CLOUDFLARE_API_TOKEN':'a'*40,'XDG_DATA_HOME':storage.name+'/data','XDG_CONFIG_HOME':storage.name+'/config'}
        adapted=subprocess.run([env['CADDY_BIN'],'adapt','--config','/dev/stdin','--adapter','caddyfile'],input=config,text=True,capture_output=True,env=env)
        self.assertEqual(adapted.returncode,0,adapted.stderr)
        data=json.loads(adapted.stdout)
        server=next(iter(data['apps']['http']['servers'].values()))
        self.assertEqual(server['listen'],['10.10.10.11:8443'])
        wrappers=server['listener_wrappers']
        self.assertEqual([w['wrapper'] for w in wrappers],['desk_proxy_v2','proxy_protocol','tls'])
        self.assertEqual(wrappers[1]['allow'],['10.10.10.11/32','10.10.10.12/32','10.10.10.13/32'])
        self.assertEqual(wrappers[1]['fallback_policy'],'REJECT')
        valid=subprocess.run([env['CADDY_BIN'],'validate','--config','/dev/stdin','--adapter','caddyfile'],input=config,text=True,capture_output=True,env=env)
        self.assertEqual(valid.returncode,0,valid.stderr)


if __name__=='__main__':unittest.main()
