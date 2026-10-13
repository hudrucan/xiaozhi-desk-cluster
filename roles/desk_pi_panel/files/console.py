#!/usr/bin/python3
"""Fixed tty1 display mode, used only by systemd before/after the panel."""
import fcntl
import os
import struct
import sys
mode={'graphics':1,'text':0}[sys.argv[1]]
fd=os.open('/dev/tty1',os.O_WRONLY)
try:
    fcntl.ioctl(fd,0x4B3A,mode)  # KDSETMODE: suppress fbcon text/cursor while showing UI.
finally:
    os.close(fd)
