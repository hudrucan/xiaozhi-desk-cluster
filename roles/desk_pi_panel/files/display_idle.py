"""Local picture timeout and wake gesture; no backlight or GPIO control."""
import json
import os
from pathlib import Path
import tempfile
import time

TIMEOUT_OPTIONS=((0,'Off'),(30,'30 sec'),(60,'1 min'),(120,'2 min'),(300,'5 min'),(600,'10 min'),(900,'15 min'),(1800,'30 min'))
STATE_PATH=Path('/var/lib/desk-cluster-panel/display.json')


def valid_timeout(value):
    return type(value) is int and value in tuple(seconds for seconds,_ in TIMEOUT_OPTIONS)


def restore(config,path=STATE_PATH):
    config.setdefault('screen_timeout',0)
    try:
        with Path(path).open('rb') as stream:
            raw=stream.read(1025)
        if len(raw)>1024:return False
        value=json.loads(raw)
        if not isinstance(value,dict) or value.get('protocol')!='desk-panel-display-v1' or not valid_timeout(value.get('screen_timeout')):return False
        config['screen_timeout']=value['screen_timeout']
        return True
    except (OSError,ValueError,TypeError):return False


def save(timeout,path=STATE_PATH):
    if not valid_timeout(timeout):raise ValueError('Invalid screen timeout')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix='.display-',delete=False) as stream:
            temp=Path(stream.name)
            json.dump({'protocol':'desk-panel-display-v1','screen_timeout':timeout},stream)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        try:
            fd=os.open(path.parent,os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        except OSError:pass
    finally:
        if temp is not None:temp.unlink(missing_ok=True)


class IdleDisplay:
    def __init__(self,timeout=0,now=None):
        self.pressed=False;self.consume_touch=False;self.sleeping=False
        self.configure(timeout,time.monotonic() if now is None else now)

    def configure(self,timeout,now):
        if not valid_timeout(timeout):raise ValueError('Invalid screen timeout')
        self.timeout=timeout;self.last_activity=now;self.sleeping=False

    def activity(self,now):
        self.last_activity=now

    def touch_down(self,now):
        woke=self.sleeping
        self.consume_touch=self.consume_touch or woke
        self.sleeping=False;self.pressed=True;self.activity(now)
        return woke

    def touch_up(self,now):
        consume=self.consume_touch
        self.pressed=False;self.consume_touch=False;self.activity(now)
        return consume

    def tick(self,now,inhibit=False):
        if inhibit or self.pressed:
            self.activity(now)
            return False
        if not self.sleeping and self.timeout and now-self.last_activity>=self.timeout:
            self.sleeping=True
            return True
        return False
