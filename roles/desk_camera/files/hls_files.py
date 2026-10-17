"""Fixed HLS artifact names and bounded, local-only playlist validation."""
import math
import pathlib
import re

MAX_BYTES = 256 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024

def valid_name(name):
    return name in {'index.m3u8', 'init.mp4'} or bool(re.fullmatch(r's[0-9]{6}\.m4s', name))

def artifact_path(base, generation, name):
    if not re.fullmatch('[a-f0-9]{32}', generation) or not valid_name(name):
        raise ValueError('Invalid HLS artifact')
    path = pathlib.Path(base) / 'hls' / generation / name
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('HLS symlink rejected')
    return path

def playlist(text):
    if len(text.encode()) > 131072 or not text.startswith('#EXTM3U\n'):
        raise ValueError('Invalid playlist')
    names = []
    duration = 0.0
    init = False
    for line in text.splitlines():
        if line.startswith('#EXT-X-MAP:'):
            if line != '#EXT-X-MAP:URI="init.mp4"':
                raise ValueError('Invalid initialization URI')
            init = True
        elif line.startswith('#EXTINF:'):
            seconds = float(line.split(':', 1)[1].rstrip(','))
            if not math.isfinite(seconds) or not 0 < seconds <= 120:
                raise ValueError('Invalid segment duration')
            duration += seconds
        elif 'URI=' in line or line.startswith('#EXT-X-KEY'):
            raise ValueError('Unexpected HLS URI')
        elif line and not line.startswith('#'):
            if line != f's{len(names):06d}.m4s':
                raise ValueError('Unexpected segment path')
            names.append(line)
    if not init or not 0 < len(names) <= 1024 or duration > 1810:
        raise ValueError('Incomplete playlist')
    return {'names': names, 'duration': duration, 'complete': '#EXT-X-ENDLIST' in text.splitlines()}

def snapshot(base, generation):
    index = artifact_path(base, generation, 'index.m3u8')
    if not index.is_file() or index.stat().st_size > 131072:
        raise ValueError('Playlist unavailable')
    value = playlist(index.read_text())
    paths = [artifact_path(base, generation, name) for name in ['init.mp4', *value['names']]]
    sizes = [p.stat().st_size for p in paths if p.is_file()]
    if len(sizes) != len(paths) or not all(0 < size <= MAX_FILE for size in sizes) or sum(sizes) > MAX_BYTES:
        raise ValueError('HLS cache is incomplete or too large')
    return dict(value, bytes=sum(sizes))
