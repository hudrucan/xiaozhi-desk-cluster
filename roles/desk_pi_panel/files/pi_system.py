"""Bounded local Pi telemetry and confirmed reboot; no cluster mutations."""
from pathlib import Path
import re
import subprocess
import threading
import time

FLAGS=((0,'Under-voltage'),(1,'Freq capped'),(2,'Throttled'),(3,'Temp limit'))


def read_text(path,limit=16384):
    try:
        with Path(path).open() as stream:return stream.read(limit)
    except (OSError,UnicodeError):return ''


def cpu_times(text):
    try:
        fields=text.splitlines()[0].split()
        if fields[0]!='cpu' or len(fields)<5:return None
        values=[int(v) for v in fields[1:9]]
        if any(v<0 for v in values):return None
        return sum(values),values[3]+(values[4] if len(values)>4 else 0)
    except (ValueError,IndexError):return None


def cpu_percent(previous,current):
    if previous is None or current is None:return None
    total,idle=current[0]-previous[0],current[1]-previous[1]
    if total<=0 or not 0<=idle<=total:return None
    return 100*(total-idle)/total


def memory(text):
    try:
        values={line.split(':',1)[0]:int(line.split()[1]) for line in text.splitlines() if ':' in line}
        total,available=values['MemTotal'],values['MemAvailable']
        if total<=0 or not 0<=available<=total:return None,None,None
        used=total-available
        return used/1024,total/1024,100*used/total
    except (ValueError,KeyError,IndexError):return None,None,None


def number(text,scale=1,maximum=1e12):
    try:
        value=float(text.split()[0])/scale
        return value if 0<=value<=maximum else None
    except (ValueError,IndexError):return None


def throttle_flags(raw):
    if not isinstance(raw,str) or not re.fullmatch(r'throttled=0x[0-9a-fA-F]{1,8}\s*',raw):return None
    bits=int(raw.split('=')[1],16)
    return {'raw':bits,'current':[label for bit,label in FLAGS if bits & (1<<bit)],
            'history':[label for bit,label in FLAGS if bits & (1<<(bit+16))],
            'unknown':bits & ~0x000f000f}


def get_throttle():
    try:
        result=subprocess.run(['/usr/bin/vcgencmd','get_throttled'],capture_output=True,text=True,timeout=1,check=True)
        return throttle_flags(result.stdout)
    except (OSError,subprocess.SubprocessError):return None


def uptime_label(seconds):
    if seconds is None:return 'Uptime unavailable'
    hours=int(seconds)//3600;days,hours=divmod(hours,24)
    return f'Uptime  {days}d {hours:02d}h' if days else f'Uptime  {hours}h {int(seconds)%3600//60:02d}m'


class PiSystem:
    def __init__(self):
        self.lock=threading.Lock();self.stopped=threading.Event();self.active=threading.Event()
        self.value={};self.version=0;self.previous=None;self.reboot_state=None
        self.confirm_at=None
    def sample(self):
        current=cpu_times(read_text('/proc/stat'))
        used,total,ram=memory(read_text('/proc/meminfo'))
        value={'cpu':cpu_percent(self.previous,current),'ram':ram,'used_mb':used,'total_mb':total,
               'temperature':number(read_text('/sys/class/thermal/thermal_zone0/temp'),1000,150),
               'mhz':number(read_text('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq'),1000,10000),
               'uptime':number(read_text('/proc/uptime')),'throttle':get_throttle()}
        self.previous=current
        with self.lock:self.value=value;self.version+=1
    def poll(self):
        while not self.stopped.is_set():
            if self.active.wait(.2):
                self.sample()
                self.stopped.wait(2)
            else:self.previous=None
    def snapshot(self):
        with self.lock:return dict(self.value),self.version,self.reboot_state
    def confirm(self,now):
        self.confirm_at=now
    def cancel(self):
        self.confirm_at=None
    def tick(self,now):
        if self.confirm_at is not None and now-self.confirm_at>=30:
            self.cancel();return True
        return False
    def request_reboot(self,now):
        confirmed=self.confirm_at is not None and 0<=now-self.confirm_at<30
        self.cancel()
        with self.lock:
            if not confirmed or self.reboot_state in ('pending','requested'):return False
            self.reboot_state='pending';self.version+=1
        threading.Thread(target=self._reboot,daemon=True).start()
        return True
    def _reboot(self):
        try:
            subprocess.run(['/usr/bin/systemctl','--no-ask-password','--no-block','reboot'],
                           capture_output=True,text=True,timeout=5,check=True)
            state='requested'
        except (OSError,subprocess.SubprocessError):state='failed'
        with self.lock:self.reboot_state=state;self.version+=1
