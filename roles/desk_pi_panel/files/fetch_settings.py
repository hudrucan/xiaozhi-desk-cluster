"""Local, bounded panel fetch interval; independent of touch/display state."""
import json
import os
from pathlib import Path
import tempfile

MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL = 1, 60, 5
STATE_PATH=Path('/var/lib/desk-cluster-panel/fetch.json')


def valid_interval(value):
    return type(value) is int and MIN_INTERVAL<=value<=MAX_INTERVAL


def parse_interval(text):
    if not isinstance(text,str) or not text.isascii() or not text.isdecimal() or len(text)>2:return None
    value=int(text)
    return value if valid_interval(value) else None


def restore(config,path=STATE_PATH):
    config.setdefault('fetch_interval',DEFAULT_INTERVAL)
    try:
        with Path(path).open('rb') as stream:raw=stream.read(1025)
        if len(raw)>1024:return False
        value=json.loads(raw)
        if not isinstance(value,dict) or value.get('protocol')!='desk-panel-fetch-v1' or not valid_interval(value.get('fetch_interval')):return False
        config['fetch_interval']=value['fetch_interval']
        return True
    except (OSError,ValueError,TypeError):return False


def save(interval,path=STATE_PATH):
    if not valid_interval(interval):raise ValueError('Invalid fetch interval')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix='.fetch-',delete=False) as stream:
            temp=Path(stream.name)
            json.dump({'protocol':'desk-panel-fetch-v1','fetch_interval':interval},stream)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        try:
            fd=os.open(path.parent,os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        except OSError:pass
    finally:
        if temp is not None:temp.unlink(missing_ok=True)
