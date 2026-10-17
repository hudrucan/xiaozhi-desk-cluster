import asyncio,pathlib,sys,time,types,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'files'))
import playback
class ViewerWorker(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.p=playback.Playback(types.SimpleNamespace(c={'workload':'tapo_c222_160'}));self.p.current='a'*32;self.p.attempt='b'*32;self.p.viewer_until=time.monotonic()+30
  self.gate=asyncio.Event();self.p.task=asyncio.create_task(self.gate.wait())
 async def asyncTearDown(self):
  self.p.task.cancel();await asyncio.gather(self.p.task,return_exceptions=True)
 async def test_old_attempt_cannot_cancel_or_touch_a_new_preparation(self):
  self.assertEqual(self.p.viewer_action('a'*32,'c'*32,True)['state'],'cancelled');self.assertFalse(self.p.stop_requested)
  self.assertEqual(self.p.viewer_action('a'*32,'c'*32)['state'],'missing')
 async def test_cancel_acknowledges_only_after_task_cleanup(self):
  self.assertEqual(self.p.viewer_action('a'*32,'b'*32,True)['state'],'cancelling')
  self.assertEqual(self.p.viewer_action('a'*32,'b'*32,True)['state'],'cancelling');self.assertTrue(self.p.stop_requested)
  self.assertFalse(self.p.task.cancelling())
  self.gate.set();await self.p.task
  self.assertEqual(self.p.viewer_action('a'*32,'b'*32,True)['state'],'cancelled')
 async def test_touch_cannot_resurrect_expired_lease(self):
  self.p.viewer_until=time.monotonic()-1;old=self.p.viewer_until
  self.assertEqual(self.p.viewer_action('a'*32,'b'*32)['state'],'missing');self.assertEqual(self.p.viewer_until,old)
 async def test_unmanaged_background_jobs_are_not_cancelled(self):
  self.p.viewer_until=None
  with self.assertRaises(ValueError):self.p.viewer_action('a'*32,'b'*32,True)
  self.assertFalse(self.p.stop_requested)
if __name__=='__main__':unittest.main()
