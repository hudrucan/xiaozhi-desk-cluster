import concurrent.futures
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

p=Path(__file__).resolve().parents[1]/'files/desk_dns_sync.py'
spec=importlib.util.spec_from_file_location('maintenance_sync',p)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        self.config={'node':'deskb1x','nodes':['deskb1x','deskb2x','deskb3x'],'adguard':'http://local'}
        self.jobs=patch.object(m,'other_filter_job',return_value=False);self.jobs.start();self.addCleanup(self.jobs.stop)
        self.lease=m.Maintenance(self.config,clock=lambda:self.now,warmup=180)
        self.ready=patch.object(m,'other_backend_ready',return_value=True);self.ready.start()
        self.addCleanup(self.ready.stop)
        self.lease.observe(True);self.now=180

    def test_coordinator_wait_reason_and_retry_deadline_survive_the_http_boundary(self):
        import io
        error=m.urllib.error.HTTPError('https://fixture',409,'busy',{},io.BytesIO(b'{"error":"filter_maintenance_busy","reason":"filter_update_or_owner_transition","retry_after":42}'))
        with patch.object(m,'owns',return_value=True),patch.object(m,'fetch',side_effect=error):
            with self.assertRaises(m.MaintenanceBusy) as pending:
                m.lease_request({**self.config,'vip':'vip','port':3002},{'action':'acquire'})
        self.assertEqual(pending.exception.reason,'filter_update_or_owner_transition')
        self.assertEqual(pending.exception.retry_after,42)

    def test_concurrent_updates_admit_exactly_one_node(self):
        def acquire(node):
            try: return self.lease.acquire(node)
            except m.MaintenanceBusy: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(acquire,self.config['nodes']))
        self.assertEqual(sum(x is not None for x in results),1)

    def test_expired_client_does_not_release_successors_lease(self):
        old=self.lease.acquire('deskb1x')['token'];self.now+=181
        new=self.lease.acquire('deskb2x')['token']
        self.assertFalse(self.lease.release('deskb1x',old))
        with self.assertRaises(m.MaintenanceBusy):self.lease.acquire('deskb3x')
        self.assertTrue(self.lease.release('deskb2x',new))

    def test_new_owner_waits_for_previous_clients_to_expire(self):
        self.lease.observe(False);self.now=200;self.lease.observe(True)
        with self.assertRaises(m.MaintenanceBusy):self.lease.acquire('deskb1x')
        self.now=380;self.assertIn('token',self.lease.acquire('deskb1x'))

    def test_last_healthy_adguard_is_not_taken_into_maintenance(self):
        with patch.object(m,'other_backend_ready',return_value=False):
            with self.assertRaises(m.MaintenanceBusy):self.lease.acquire('deskb1x')

    def test_successful_filter_update_releases_after_native_dns_health(self):
        with patch.object(m,'acquire_maintenance',return_value='token'),patch.object(m,'fetch',return_value={'updated':1}),patch.object(m,'native_ready',return_value=True),patch.object(m,'release_maintenance') as release:
            self.assertEqual(m.filter_write(self.config,'filtering/refresh',{'force':True}),{'updated':1})
            release.assert_called_once_with(self.config,'token')

    def test_plain_text_mutations_do_not_require_json_but_refresh_preserves_json(self):
        for route,value,json_reply in (
            ('filtering/add_url',{'url':'https://example.invalid/list','name':'Test','whitelist':False},False),
            ('filtering/remove_url',{'url':'https://example.invalid/list','whitelist':False},False),
            ('filtering/refresh',{'whitelist':False},True)):
            with patch.object(m,'acquire_maintenance',return_value='token'),patch.object(m,'fetch',return_value=None) as fetch,patch.object(m,'native_ready',return_value=True),patch.object(m,'release_maintenance') as release:
                m.filter_write(self.config,route,value)
                self.assertEqual(fetch.call_args.kwargs['return_mutation'],json_reply)
                release.assert_called_once_with(self.config,'token')

    def test_uncertain_timeout_retains_lease(self):
        with patch.object(m,'acquire_maintenance',return_value='token'),patch.object(m,'fetch',side_effect=TimeoutError),patch.object(m,'release_maintenance') as release:
            with self.assertRaises(TimeoutError):m.filter_write(self.config,'filtering/refresh',{})
            release.assert_not_called()

    def test_native_auto_schedule_cannot_bypass_serial_updates(self):
        value=m.validate_write('filtering/config',{'enabled':True,'interval':24},serialized=True)
        self.assertTrue(value['enabled']);self.assertEqual(value['interval'],0)

    def test_arbitrary_route_cannot_use_maintenance_to_mutate_other_settings(self):
        with patch.object(m,'acquire_maintenance') as acquire:
            with self.assertRaises(ValueError):m.filter_write(self.config,'dns_config',{})
            acquire.assert_not_called()

    def test_renewal_keeps_a_long_native_job_exclusive_past_original_ttl(self):
        token=self.lease.acquire('deskb1x')['token'];self.now+=170
        self.lease.renew('deskb1x',token);self.now+=30
        with self.assertRaises(m.MaintenanceBusy):self.lease.acquire('deskb2x')
        self.assertTrue(self.lease.release('deskb1x',token))

    def test_restart_or_expired_lease_does_not_bypass_live_native_job_guard(self):
        with patch.object(m,'other_filter_job',return_value=True):
            with self.assertRaises(m.MaintenanceBusy) as error:self.lease.acquire('deskb1x')
        self.assertEqual(error.exception.reason,'native_filter_job_still_running')

    def test_complete_native_server_error_is_not_mistaken_for_an_inflight_timeout(self):
        error=m.urllib.error.HTTPError('http://local',500,'native error',{},None)
        self.addCleanup(error.close)
        with patch.object(m,'held_maintenance'),patch.object(m,'record_filter_job') as marker,patch.object(m,'fetch',side_effect=error):
            with self.assertRaises(m.urllib.error.HTTPError):m.filter_write(self.config,'filtering/refresh',{})
        self.assertEqual([call.args[1] for call in marker.call_args_list],[True,False])

    def test_health_pending_never_returns_filter_success(self):
        now=iter([0,31])
        with patch.object(m,'held_maintenance') as held,patch.object(m,'fetch',return_value=None),patch.object(m,'native_ready',return_value=False),patch.object(m.time,'monotonic',side_effect=lambda:next(now)):
            held.return_value.__enter__.return_value.is_set.return_value=False
            with self.assertRaises(m.MaintenanceBusy):m.filter_write(self.config,'filtering/refresh',{})

if __name__=='__main__':unittest.main()
