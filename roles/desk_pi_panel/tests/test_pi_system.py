from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).parents[1]/'files'))
import pi_system as pi


class PiSystemTests(unittest.TestCase):
    def test_cpu_delta_excludes_guest_and_counts_iowait_as_idle(self):
        before=pi.cpu_times('cpu 10 0 10 70 10 0 0 0 5 5\ncpu0 1 2 3 4')
        after=pi.cpu_times('cpu 20 0 20 140 20 0 0 0 50 50')
        self.assertEqual(before,(100,80));self.assertEqual(pi.cpu_percent(before,after),20)
        for old,new in ((None,after),(after,after),(after,before),((0,0),(10,20))):
            self.assertIsNone(pi.cpu_percent(old,new))
        for text in ('','cpu0 1 2 3 4','cpu bad','cpu 1 2 -3 4'):self.assertIsNone(pi.cpu_times(text))
    def test_ram_uses_available_memory_and_rejects_impossible_values(self):
        self.assertEqual(pi.memory('MemTotal: 102400 kB\nMemAvailable: 76800 kB'),(25,100,25))
        for text in ('','MemTotal: 0 kB\nMemAvailable: 0 kB','MemTotal: 1 kB\nMemAvailable: 2 kB'):
            self.assertEqual(pi.memory(text),(None,None,None))
    def test_current_throttle_history_and_unknown_bits_are_distinct(self):
        self.assertEqual(pi.throttle_flags('throttled=0x0\n'),{'raw':0,'current':[],'history':[],'unknown':0})
        value=pi.throttle_flags('throttled=0x50001')
        self.assertEqual(value['current'],['Under-voltage'])
        self.assertEqual(value['history'],['Under-voltage','Throttled'])
        self.assertEqual(pi.throttle_flags('throttled=0x10')['unknown'],16)
        for text in ('','0x0','throttled=0x0 extra','throttled=0x100000000'):
            self.assertIsNone(pi.throttle_flags(text))
    def test_missing_or_denied_firmware_status_is_unavailable(self):
        with patch.object(pi.subprocess,'run',side_effect=PermissionError):self.assertIsNone(pi.get_throttle())
        with patch.object(pi.subprocess,'run',side_effect=subprocess.TimeoutExpired('vcgencmd',1)):self.assertIsNone(pi.get_throttle())
        with patch.object(pi.subprocess,'run',return_value=Mock(stdout='throttled=0x0')) as command:
            self.assertEqual(pi.get_throttle()['raw'],0)
            self.assertEqual(command.call_args.args[0],['/usr/bin/vcgencmd','get_throttled'])
    def test_sampling_first_cpu_is_unknown_then_uses_delta(self):
        values={'/proc/stat':'cpu 10 0 10 80 0','/proc/meminfo':'MemTotal: 102400 kB\nMemAvailable: 76800 kB',
                '/sys/class/thermal/thermal_zone0/temp':'46540',
                '/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq':'1000000','/proc/uptime':'10000 1'}
        sampler=pi.PiSystem()
        with patch.object(pi,'read_text',side_effect=lambda path:values.get(path,'')),patch.object(pi,'get_throttle',return_value=None):
            sampler.sample();self.assertIsNone(sampler.snapshot()[0]['cpu'])
            values['/proc/stat']='cpu 20 0 20 160 0';sampler.sample()
        value,version,_=sampler.snapshot()
        self.assertEqual(value['cpu'],20);self.assertEqual(value['ram'],25)
        self.assertEqual(value['temperature'],46.54);self.assertEqual(value['mhz'],1000)
        self.assertIsNone(value['throttle']);self.assertEqual(version,2)
    def test_reboot_requires_fresh_confirmation_and_cannot_duplicate_request(self):
        controller=pi.PiSystem()
        with patch.object(pi.threading,'Thread') as thread:
            self.assertFalse(controller.request_reboot(0))
            controller.confirm(0);controller.cancel();self.assertFalse(controller.request_reboot(1))
            controller.confirm(0);self.assertTrue(controller.tick(30));self.assertFalse(controller.request_reboot(30))
            controller.confirm(31);self.assertTrue(controller.request_reboot(32))
            controller.confirm(33);self.assertFalse(controller.request_reboot(34))
            thread.return_value.start.assert_called_once()
    def test_reboot_uses_only_fixed_normal_systemctl_command_and_reports_failure(self):
        controller=pi.PiSystem()
        with patch.object(pi.subprocess,'run') as command:
            controller._reboot();self.assertEqual(controller.snapshot()[2],'requested')
            self.assertEqual(command.call_args.args[0],['/usr/bin/systemctl','--no-ask-password','--no-block','reboot'])
            self.assertEqual(command.call_args.kwargs['timeout'],5)
        with patch.object(pi.subprocess,'run',side_effect=PermissionError):
            controller._reboot();self.assertEqual(controller.snapshot()[2],'failed')
    def test_uptime_and_sensor_values_reject_nonfinite_or_invalid_data(self):
        for text in ('nan','inf','-1',''):
            self.assertIsNone(pi.number(text))
        self.assertEqual(pi.uptime_label(187200),'Uptime  2d 04h')
        self.assertEqual(pi.uptime_label(3660),'Uptime  1h 01m')
        self.assertEqual(pi.uptime_label(None),'Uptime unavailable')


if __name__=='__main__':unittest.main()
