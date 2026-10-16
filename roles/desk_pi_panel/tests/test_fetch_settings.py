import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]/'files'))
import fetch_settings as fetch


class FetchSettingsTests(unittest.TestCase):
    def test_numeric_input_and_range(self):
        for text,value in (('1',1),('5',5),('60',60),('01',1)):
            self.assertEqual(fetch.parse_interval(text),value)
        for text in ('','0','61','99','100','1.0','-1',' 5','５','١',None,5):
            self.assertIsNone(fetch.parse_interval(text))
        for value in (True,False,1.0,'1',0,61,None):self.assertFalse(fetch.valid_interval(value))
    def test_default_and_saved_bounds_restore_without_touching_other_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fetch.json';config={}
            self.assertFalse(fetch.restore(config,path));self.assertEqual(config['fetch_interval'],5)
            calibration=Path(directory)/'calibration.json';calibration.write_text('touch')
            display=Path(directory)/'display.json';display.write_text('timeout')
            for interval in (1,60,5):
                fetch.save(interval,path);self.assertTrue(fetch.restore(config,path))
                self.assertEqual(config['fetch_interval'],interval)
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(calibration.read_text(),'touch');self.assertEqual(display.read_text(),'timeout')
    def test_invalid_or_oversized_state_retains_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fetch.json'
            for value in ([],None,{'protocol':'wrong','fetch_interval':1},
                          {'protocol':'desk-panel-fetch-v1','fetch_interval':True},
                          {'protocol':'desk-panel-fetch-v1','fetch_interval':61}):
                path.write_text(json.dumps(value));config={'fetch_interval':10}
                self.assertFalse(fetch.restore(config,path));self.assertEqual(config['fetch_interval'],10)
            for raw in ('broken','x'*1025):
                path.write_text(raw);self.assertFalse(fetch.restore({},path))
    def test_save_failure_retains_previous_value_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fetch.json';fetch.save(5,path);before=path.read_bytes()
            with patch.object(fetch.os,'replace',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):fetch.save(1,path)
            self.assertEqual(path.read_bytes(),before);self.assertEqual(list(Path(directory).iterdir()),[path])
            with self.assertRaises(ValueError):fetch.save(0,path)


if __name__=='__main__':unittest.main()
