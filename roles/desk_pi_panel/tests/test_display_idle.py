import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]/'files'))
import display_idle as idle


class DisplayIdleTests(unittest.TestCase):
    def test_disabled_and_exact_timeout_without_repeated_blanking(self):
        display=idle.IdleDisplay(0,0)
        self.assertFalse(display.tick(1000000))
        display.configure(30,10)
        self.assertFalse(display.tick(39.99))
        self.assertTrue(display.tick(40))
        self.assertTrue(display.sleeping)
        self.assertFalse(display.tick(41))

    def test_wake_consumes_entire_first_gesture_only(self):
        display=idle.IdleDisplay(30,0);display.tick(30)
        self.assertTrue(display.touch_down(31))
        self.assertFalse(display.sleeping)
        # Duplicate down events must not turn the waking touch into a UI action.
        self.assertFalse(display.touch_down(31.1))
        self.assertTrue(display.touch_up(32))
        self.assertFalse(display.touch_down(33))
        self.assertFalse(display.touch_up(34))
        self.assertFalse(display.tick(63.99))
        self.assertTrue(display.tick(64))

    def test_held_touch_and_calibration_suspend_idle_countdown(self):
        display=idle.IdleDisplay(30,0)
        display.touch_down(1)
        self.assertFalse(display.tick(100))
        display.touch_up(101)
        self.assertFalse(display.tick(120,inhibit=True))
        self.assertFalse(display.tick(149))
        self.assertTrue(display.tick(150))

    def test_timeout_changes_reset_countdown_and_off_disables_it(self):
        display=idle.IdleDisplay(30,0)
        display.configure(60,29)
        self.assertFalse(display.tick(88))
        self.assertTrue(display.tick(89))
        display.touch_down(90);display.touch_up(91)
        display.configure(0,92)
        self.assertFalse(display.tick(100000))
        for value in (True,-1,1,30.0,'30',None):
            with self.assertRaises(ValueError):display.configure(value,0)

    def test_save_reload_permissions_and_calibration_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'display.json';cal=Path(directory)/'calibration.json'
            cal.write_text('user calibration')
            idle.save(120,path)
            config={};self.assertTrue(idle.restore(config,path))
            self.assertEqual(config['screen_timeout'],120)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(cal.read_text(),'user calibration')

    def test_invalid_missing_or_oversize_saved_state_keeps_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'display.json';config={}
            self.assertFalse(idle.restore(config,path));self.assertEqual(config['screen_timeout'],0)
            for value in ({'protocol':'wrong','screen_timeout':30},
                          {'protocol':'desk-panel-display-v1','screen_timeout':True},[],None):
                path.write_text(json.dumps(value));config={'screen_timeout':60}
                self.assertFalse(idle.restore(config,path));self.assertEqual(config['screen_timeout'],60)
            for raw in ('broken','x'*1025):
                path.write_text(raw);self.assertFalse(idle.restore(config,path))

    def test_failed_atomic_save_keeps_previous_timeout_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'display.json';idle.save(60,path);before=path.read_bytes()
            with patch.object(idle.os,'replace',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):idle.save(120,path)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(list(Path(directory).iterdir()),[path])


if __name__=='__main__':unittest.main()
