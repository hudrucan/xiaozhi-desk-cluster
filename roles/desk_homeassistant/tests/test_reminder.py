import importlib.util,pathlib,unittest
p=pathlib.Path(__file__).resolve().parents[1]/'files/desk_cluster/reminder.py'
s=importlib.util.spec_from_file_location('reminder',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class ReminderTests(unittest.TestCase):
 def test_debounce_once_and_reset_after_sync(self):
  r=m.PendingReminder(delay=60);pending={'ready':True,'pending_disk_changes':True}
  self.assertIsNone(r.observe(pending,0));self.assertIsNone(r.observe(pending,59));self.assertEqual(r.observe(pending,60),'notify')
  self.assertIsNone(r.observe(pending,120));self.assertEqual(r.observe(dict(pending,pending_disk_changes=False),130),'dismiss')
  self.assertIsNone(r.observe(pending,140));self.assertEqual(r.observe(pending,200),'notify')
 def test_default_notifies_on_first_confirmed_change(self):
  r=m.PendingReminder();self.assertEqual(r.observe({'ready':True,'pending_disk_changes':True},0),'notify')
  self.assertIsNone(r.observe({'ready':True,'pending_disk_changes':True},1))
 def test_sync_busy_and_unknown_status_do_not_create_or_dismiss(self):
  r=m.PendingReminder(delay=60);p={'ready':True,'pending_disk_changes':True}
  r.observe(p,0);self.assertIsNone(r.observe(dict(p,operation={'state':'replicating'}),90))
  self.assertEqual(r.observe(p,100),'notify');self.assertIsNone(r.observe({'ready':False},120));self.assertIsNone(r.observe({'ready':True},130))
 def test_manual_dismiss_is_respected_until_new_dirty_epoch(self):
  r=m.PendingReminder(delay=60);p={'ready':True,'pending_disk_changes':True};r.observe(p,0);r.observe(p,60)
  self.assertIsNone(r.observe(p,1000));r.observe(dict(p,pending_disk_changes=False),1001);r.observe(p,1002);self.assertEqual(r.observe(p,1062),'notify')
if __name__=='__main__':unittest.main()
