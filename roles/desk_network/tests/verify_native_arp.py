"""Run inside the pinned NetAlertX image with synthetic data and --network=none."""
import sys,pathlib,json,types,os
from unittest.mock import patch,Mock
sys.path[:0]=['/app/desk_network','/app/server','/app/server/plugins']
for name in ('/data/config','/data/db','/tmp/api','/tmp/log/plugins'):pathlib.Path(name).mkdir(parents=True,exist_ok=True)
pathlib.Path('/data/config/app.conf').write_text("TIMEZONE='UTC'\nLOG_LEVEL='none'\n")
type_string=json.dumps({'dataType':'string','elements':[{'elementType':'input','transformers':[]}]})
pathlib.Path('/tmp/api/table_settings.json').write_text(json.dumps({'data':[{'setKey':k,'setValue':v,'setType':type_string} for k,v in [('TIMEZONE','UTC'),('LOG_LEVEL','none')]]}))
os.environ['DESK_NETWORK_DISTRIBUTED']='1'
import arp,plugin
from const import logPath
import mdns,plugin_helper
stamp='2026-10-23 13:00:00'
keys=['primaryId','secondaryId','watched1','watched2','watched3','watched4','extra','foreignKey']
rows=[('02:11:22:33:44:01','192.168.1.1','192.168.1.1','Router','192.168.1.0/24 --interface=wlan0','','ARPSCAN',''),('02:11:22:33:44:02','192.168.1.102','','device.local','','','','02:11:22:33:44:02')]
with patch.object(plugin_helper,'timeNowUTC',return_value=stamp),patch.object(mdns,'datetime') as clock:
 clock.now.return_value.strftime.return_value=stamp
 for selected in [[],rows[:1],rows[1:],rows]:
  expected=plugin_helper.Plugin_Objects('/tmp/native-result')
  for row in selected:expected.add_object(**dict(zip(keys,row)))
  expected.write_result_file();mdns.write_plugin_results('/tmp/desk-result',selected)
  assert pathlib.Path('/tmp/native-result').read_bytes()==pathlib.Path('/tmp/desk-result').read_bytes()
print('PASS byte-for-byte ARP/mDNS results against pinned native Plugin_Objects, including UTC and empty output')
result=pathlib.Path(logPath)/'plugins/last_result.ARPSCAN.log'
arp.lookup=lambda:({'192.168.1.1':['02:11:22:33:44:01','Router']},{'received':1})
assert arp.main()==0
fields=result.read_text().strip().split('|')
assert fields[0]=='02:11:22:33:44:01' and fields[1]=='192.168.1.1' and fields[3]=='192.168.1.1' and fields[4]=='Router' and fields[7]=='ARPSCAN',fields
# Use the pinned real execute_plugin file import path, replacing only providers/DB.
obj={'unique_prefix':'ARPSCAN','data_source':'script','settings':[]}
settings=lambda obj,key:{'value':'python3 /app/desk_network/arp.py' if key=='CMD' else 60}
with patch.object(plugin,'get_plugin_setting_obj',settings),patch.object(plugin,'process_plugin_events') as promote,patch.object(plugin,'update_api'),patch.object(plugin,'UserEventsQueueInstance') as queue:
 queue.return_value.has_update_devices.return_value=False
 with patch.object(plugin,'run_plugin',return_value='ok'):
  plugin.execute_plugin(types.SimpleNamespace(sql=Mock()),[obj],obj)
 assert promote.call_count==1
 assert promote.call_args.args[2][0][2]=='02:11:22:33:44:01'
 promote.reset_mock()
 def unavailable():raise OSError('no worker')
 arp.lookup=unavailable
 assert arp.main()==1 and result.read_text()==''
 with patch.object(plugin,'run_plugin',return_value=None):
  plugin.execute_plugin(types.SimpleNamespace(sql=Mock()),[obj],obj)
 promote.assert_not_called()
print('PASS pinned ARPSCAN serialization and native execute_plugin: success imports; unavailable clears stale rows and skips CurrentScan promotion')
