import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'files'))
import calibration_ui as cal
import tempfile
import unittest
from unittest.mock import patch

# Synthetic raw sensor axes are swapped and inverted relative to mounted pixels.
def raw(point):
    x,y=point
    return (y*10+120,(479-x)*8+200)
def taps(point):
    x,y=raw(point)
    return [(x+1,y),(x-1,y+2),(x,y-2),(x+2,y+1),(x-2,y),(x,y),(x,y+1),(x,y-1)]

class CalibrationTests(unittest.TestCase):
    def collect(self,rotation=180):
        c=cal.Calibration(rotation,0);c.start(1)
        for i,t in enumerate(cal.TARGETS):c.sample(taps(t),0.3,2+i)
        self.assertEqual(c.phase,'review');return c
    def test_swapped_inverted_axes_fit_and_independent_centre_validate(self):
        for rotation in (0,180):
            c=self.collect(rotation)
            self.assertLess(c.error,1)
            for t in cal.TARGETS:self.assertLess(__import__('math').dist(cal.project(raw(t),c.transform,rotation),t),1)
    def test_median_rejects_single_edge_outliers(self):
        points=[(0,4095)]+taps(cal.TARGETS[0])+[(4095,0)]
        x,y=cal.stable_sample(points,.3)
        self.assertLess(abs(x-raw(cal.TARGETS[0])[0]),2)
        self.assertLess(abs(y-raw(cal.TARGETS[0])[1]),2)
    def test_brief_or_moving_taps_retry_current_target(self):
        c=cal.Calibration(180,0);c.start(1)
        c.sample(taps(cal.TARGETS[0]),.05,2)
        self.assertEqual(c.samples,[]);self.assertEqual(c.phase,'collect')
        c.sample([(i*700,i*300) for i in range(8)],.3,3)
        self.assertEqual(c.samples,[])
    def test_bad_centre_cannot_apply(self):
        c=cal.Calibration(180,0);c.start(1)
        for i,t in enumerate(cal.TARGETS[:4]):c.sample(taps(t),.3,2+i)
        c.sample(taps((280,190)),.3,7)
        self.assertEqual(c.phase,'failed');self.assertIsNone(c.transform)
        with self.assertRaises(ValueError):c.apply({})
    def test_repeated_corner_points_fail(self):
        c=cal.Calibration(180,0);c.start(1)
        for _ in range(4):c.sample(taps(cal.TARGETS[0]),.3,3)
        self.assertEqual(c.phase,'failed')
    def test_save_requires_apply_and_cancel_preserves_old_mapping(self):
        old=[.1,0,0,0,.1,0];config={'touch_transform':old.copy()}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'calibration.json';c=self.collect();c.cancel()
            self.assertFalse(path.exists());self.assertEqual(config['touch_transform'],old)
            with self.assertRaises(ValueError):c.apply(config,path)
    def test_apply_persists_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'calibration.json';config={};c=self.collect();c.apply(config,path)
            self.assertEqual(c.phase,'applied');self.assertEqual(path.stat().st_mode & 0o777,0o600)
            reload={'touch_transform':[1,0,0,0,1,0]}
            self.assertTrue(cal.restore(reload,path));self.assertEqual(reload['touch_transform'],config['touch_transform'])
            path.write_text('invalid');old=reload.copy();self.assertFalse(cal.restore(reload,path));self.assertEqual(reload,old)
    def test_disk_failure_keeps_active_and_saved_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'calibration.json';c=self.collect();config={'touch_transform':[1,0,0,0,1,0]}
            cal.save(config['touch_transform'],0,path);old=path.read_bytes()
            with patch.object(cal.os,'replace',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):c.apply(config,path)
            self.assertEqual(config['touch_transform'],[1,0,0,0,1,0]);self.assertEqual(path.read_bytes(),old)
    def test_timeout_and_retry_discard_candidate(self):
        c=self.collect();self.assertTrue(c.tick(100));self.assertEqual(c.phase,'cancelled')
        c.start(101);self.assertEqual(c.samples,[]);self.assertIsNone(c.transform)
    def test_first_touch_cannot_apply_confirmation(self):
        c=cal.Calibration(180,0)
        with self.assertRaises(ValueError):c.apply({})
    def test_host_fallback_retained_if_no_or_invalid_local_override(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'calibration.json';config={'touch_transform':[1,0,0,0,1,0]};old=config.copy()
            self.assertFalse(cal.restore(config,path))
            path.write_text('{"protocol":"desk-panel-touch-v1","transform":[0,0,0,0,0,0],"error_pixels":0}')
            self.assertFalse(cal.restore(config,path));self.assertEqual(config,old)

if __name__=='__main__':unittest.main()
