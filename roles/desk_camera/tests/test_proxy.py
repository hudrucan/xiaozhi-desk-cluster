import importlib.util,pathlib,unittest
ROOT=pathlib.Path(__file__).parent
if not (ROOT/'proxy_config.py').exists():ROOT=ROOT.parent/'files'
spec=importlib.util.spec_from_file_location('proxy_config',ROOT/'proxy_config.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
POLICY='  option httpchk\n  http-check send meth GET uri /ready ver HTTP/1.1 hdr Host media\n  http-check expect status 200\n'
CHECK=' check port 20226 inter 1s fall 1 rise 1\n'
CONFIG='global\n  maxconn 64\nfrontend untouched\n  bind 0.0.0.0:1986\nbackend camera_owner\n'+POLICY+'  server n1 10.10.10.11:1984'+CHECK+'backend camera_rtsp_owner\n  mode tcp\n'+POLICY+'  server n1 10.10.10.11:18554'+CHECK
class ProxyTests(unittest.TestCase):
 def test_track_same_readiness_without_changing_interval_or_frontend(self):
  v=m.tracked_health_checks(CONFIG)
  self.assertIn('frontend untouched\n  bind 0.0.0.0:1986',v)
  self.assertIn('server n1 10.10.10.11:1984'+CHECK,v)
  self.assertIn('server n1 10.10.10.11:18554 track camera_owner/n1',v)
  self.assertEqual(v.count('check port'),1);self.assertEqual(v,m.tracked_health_checks(v))
 def test_different_policy_is_not_silently_deduplicated(self):
  for old,new in [('/ready ver','/different ver'),('check port 20226 inter 1s','check port 20226 inter 2s')]:
   # Change only the RTSP block.
   prefix,rtsp=CONFIG.split('backend camera_rtsp_owner')
   with self.assertRaises(ValueError):m.tracked_health_checks(prefix+'backend camera_rtsp_owner'+rtsp.replace(old,new))
 def test_different_host_or_missing_target_fails_closed(self):
  for old,new in [('10.10.10.11:18554','10.10.10.12:18554'),('server n1 10.10.10.11:18554','server unknown 10.10.10.11:18554')]:
   with self.assertRaises(ValueError):m.tracked_health_checks(CONFIG.replace(old,new))
if __name__=='__main__':unittest.main()
