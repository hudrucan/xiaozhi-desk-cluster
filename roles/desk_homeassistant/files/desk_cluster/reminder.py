"""One native notification per unsynced-config epoch, without automatic sync."""
import time

class PendingReminder:
 def __init__(self,delay=0):
  self.delay=delay;self.since=None;self.notified=False;self.notification_known=None
 def observe(self,status,now=None):
  now=time.monotonic() if now is None else now
  if status.get('ready') is not True:return None
  pending=status.get('pending_disk_changes')
  if pending is False:
   # Dismiss an old notification after restart too; never touch other notices.
   dismiss=self.notification_known or not self.notified
   self.since=None;self.notified=False
   if dismiss and self.notification_known is not False:self.notification_known=False;return 'dismiss'
   return None
  if pending is not True:return None
  if self.since is None:self.since=now
  if status.get('operation',{}).get('state') in ('preparing','replicating'):return None
  if not self.notified and now-self.since>=self.delay:
   self.notified=True;self.notification_known=True;return 'notify'
  return None
