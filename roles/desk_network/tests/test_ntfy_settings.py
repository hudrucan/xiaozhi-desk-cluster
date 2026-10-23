"""Native settings Save preserves scan configuration and requires checkpoint ACK."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('ntfy_settings', Path(__file__).parents[1]/'files/ntfy_settings.py')
ntfy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ntfy)


def row(key, kind, value):
    return dict(setKey=key, setType=repr({'dataType':kind}), setValue=value, setGroup='test')


def fixture():
    values = {'NTFY_RUN':'disabled', 'NTFY_HOST':'https://ntfy.sh', 'NTFY_TOPIC':'',
              'NTFY_TOKEN':'', 'NTFY_USER':'', 'NTFY_PASSWORD':'', 'NTFY_PRIORITY':'urgent',
              'NTFY_URL_QUERY_STRING':''}
    return ([row(k,'string',v) for k,v in values.items()]
            + [row('NTFY_CUSTOM_HEADERS','array','[]'),
               row('NTFPRCS_INCLUDED_SECTIONS','array',"['events']"),
               row('NTFPRCS_alert_down_time','integer','0'),
               row('LOADED_PLUGINS','array',"['ARPSCAN','NTFY']"),
               row('ARPSCAN_RUN_SCHD','string','*/1 * * * *'),
               row('NTFPRCS_sleep_time','integer','30'),
               row('ARPSCAN_RUN_SCHD__metadata','json','{}')])


class NativeSettings(unittest.TestCase):
    def test_only_managed_notification_settings_change_and_rerun_is_noop(self):
        rows = fixture()
        payload = ntfy.settings(rows,'t'*32,'http://192.168.1.186:2586')
        saved = {r[1]:r[3] for r in payload}
        self.assertEqual(saved['NTFPRCS_INCLUDED_SECTIONS'],['new_devices','down_devices'])
        self.assertEqual(saved['NTFPRCS_alert_down_time'],5)
        self.assertEqual(saved['NTFY_TOKEN'],'t'*32)
        self.assertEqual(saved['NTFY_RUN'],'on_notification')
        self.assertEqual(saved['NTFY_CUSTOM_HEADERS'],[])
        self.assertEqual(saved['ARPSCAN_RUN_SCHD'],'*/1 * * * *')
        self.assertEqual(saved['NTFPRCS_sleep_time'],30)
        self.assertEqual(saved['LOADED_PLUGINS'],['ARPSCAN','NTFY'])
        self.assertNotIn('ARPSCAN_RUN_SCHD__metadata',saved)
        updated = [row(k,t,v) for _,k,t,v in payload]
        self.assertIsNone(ntfy.settings(updated,'t'*32,'http://192.168.1.186:2586'))

    def test_unloaded_plugin_and_invalid_token_fail_before_save(self):
        with self.assertRaises(ValueError): ntfy.settings(fixture(),'bad','http://vip:2586')
        with self.assertRaises(ValueError): ntfy.settings(fixture()[1:],'t'*32,'http://vip:2586')

    def test_configured_rerun_does_not_post_or_checkpoint(self):
        payload=ntfy.settings(fixture(),'t'*32,'http://vip:2586')
        updated=[row(k,t,v) for _,k,t,v in payload]
        opener=Mock(side_effect=[io.BytesIO(b'{"state":"active","sync":"synced"}'),
                                 io.BytesIO(json.dumps({'data':updated}).encode())])
        self.assertFalse(ntfy.configure(opener,'t'*32,'http://vip:2586','10.10.10.11'))
        self.assertEqual(opener.call_count,2)
        self.assertTrue(all(c.args[0].get_method()=='GET' for c in opener.call_args_list))

    def test_save_requires_synced_owner_and_checkpoint_ack(self):
        def response(data, headers=None):
            result=io.BytesIO(json.dumps(data).encode());result.headers=headers or {};return result
        for sync in ('synced','pending'):
            opener=Mock(side_effect=[response({'state':'active','sync':'synced'}),
                                     response({'data':fixture()}),
                                     response({'success':True},{'X-Desk-Network-Sync':sync})])
            if sync=='synced':self.assertTrue(ntfy.configure(opener,'t'*32,'http://vip:2586','10.10.10.11'))
            else:
                with self.assertRaises(ValueError):ntfy.configure(opener,'t'*32,'http://vip:2586','10.10.10.11')
            request=opener.call_args.args[0]
            self.assertEqual(request.get_method(),'POST')
            self.assertNotIn('t'*32,request.full_url)
        opener=Mock(return_value=response({'state':'standby','sync':'synced'}))
        with self.assertRaises(ValueError):ntfy.configure(opener,'t'*32,'http://vip:2586','10.10.10.11')
        self.assertEqual(opener.call_count,1)


if __name__=='__main__': unittest.main()
