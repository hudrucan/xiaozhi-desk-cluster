"""Incremental microSD HLS on the existing fenced worker; no HA runtime."""
import asyncio
import contextlib
import json
import os
import pathlib
import time
from hls_files import MAX_BYTES, snapshot

class ByteQueue:
    def __init__(self, limit):
        self.limit = limit
        self.size = 0
        self.items = asyncio.Queue(128)
        self.changed = asyncio.Condition()

    async def put(self, data):
        if data is None:
            await asyncio.wait_for(self.items.put(None), 10)
            return
        if len(data) > self.limit:
            raise ValueError('Media packet exceeds queue limit')
        async with self.changed:
            await asyncio.wait_for(self.changed.wait_for(lambda: self.size + len(data) <= self.limit), 10)
            self.size += len(data)
        await asyncio.wait_for(self.items.put(data), 10)

    async def pump(self, writer):
        try:
            while True:
                data = await self.items.get()
                if data is None:
                    break
                writer.write(data)
                await asyncio.wait_for(writer.drain(), 10)
                async with self.changed:
                    self.size -= len(data)
                    self.changed.notify_all()
        finally:
            writer.close()

async def audio_writer(fd):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.connect_write_pipe(lambda: asyncio.streams.FlowControlMixin(loop=loop), os.fdopen(fd, 'wb', buffering=0))
    return asyncio.StreamWriter(transport, protocol, None, loop)

async def package(tapo, job, correction, root, generation, save):
    from pytapo.media_stream.downloader import Downloader
    from pytapo.media_stream._utils import StreamType
    from rtp import PayloadType
    root = pathlib.Path(root)
    output = root / 'hls' / generation
    output.mkdir(parents=True, mode=0o700)
    started = time.monotonic()
    sample_rate = None
    try:
        config = await asyncio.to_thread(tapo.getAudioConfig)
        sample_rate = int(config['audio_config']['microphone']['sampling_rate']) * 1000
    except (KeyError, TypeError, ValueError):
        pass
    if sample_rate not in (8000, 16000, 32000, 48000):
        sample_rate = None
    downloader = Downloader(tapo, job['start'], job['end'], correction, padding=0, stall_timeout=15)
    method = 'download'
    process = None
    tasks = []
    received = 0
    playable_at = None
    finished = False
    has_audio = False
    last_value = {}

    async def start_mux(audio_type):
        nonlocal process, tasks, has_audio
        has_audio = audio_type is not None
        cmd = ['ffmpeg','-hide_banner','-loglevel','error','-y','-probesize','524288','-analyzeduration','500000','-f','mpegts','-i','pipe:0']
        read_fd = write_fd = None
        if has_audio:
            rate = sample_rate or (16000 if audio_type == PayloadType.PCMU else 8000)
            read_fd, write_fd = os.pipe()
            cmd += ['-probesize','32','-analyzeduration','0','-f','mulaw' if audio_type == PayloadType.PCMU else 'alaw','-ar',str(rate),'-ac','1','-i',f'pipe:{read_fd}']
        cmd += ['-map','0:v:0']
        if has_audio:
            cmd += ['-map','1:a:0','-c:a','aac','-b:a','32k']
        cmd += ['-c:v','copy','-threads','1',*(['-t',str(job['end']-job['start'])] if method=='playback' else []),'-f','hls','-hls_time','2','-hls_playlist_type','event','-hls_segment_type','fmp4','-hls_flags','temp_file+independent_segments','-hls_fmp4_init_filename','init.mp4','-hls_segment_filename',str(output/'s%06d.m4s'),str(output/'index.m3u8')]
        try:
            process = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, pass_fds=(read_fd,) if has_audio else ())
        except BaseException:
            if write_fd is not None:os.close(write_fd)
            raise
        finally:
            if read_fd is not None:os.close(read_fd)
        video = ByteQueue(2*1024*1024)
        audio = ByteQueue(256*1024) if has_audio else None
        tasks.append(asyncio.create_task(video.pump(process.stdin)))
        if audio:
            tasks.append(asyncio.create_task(audio.pump(await audio_writer(write_fd))))
        return video, audio

    async def publish():
        nonlocal playable_at, last_value
        while True:
            try:
                view = snapshot(root, generation)
                if len(view['names']) >= 2 or view['complete']:
                    if playable_at is None:playable_at = time.monotonic()-started
                    last_value = {'state':'playable','format':'hls','generation':generation,'segments':len(view['names']),'progress':view['duration'],'total':job['end']-job['start'],'bytes':view['bytes'],'has_audio':has_audio,'first_playable_seconds':round(playable_at,3),'received_bytes':received,'mode':method}
                    save(last_value)
            except (OSError, ValueError):
                pass
            if sum(p.stat().st_size for p in output.iterdir() if p.is_file()) > MAX_BYTES:
                raise ValueError('HLS output exceeds cache budget')
            await asyncio.sleep(1)

    publisher = asyncio.create_task(publish())
    try:
        while True:
            downloader.method = method
            pending = []
            pending_bytes = 0
            video = audio = None
            unsupported = False
            session = tapo.getMediaSession(StreamType.Download)
            session.set_window_size(50)
            async with session:
                stream = session.transceive(json.dumps(downloader._buildRequest()), no_data_timeout=15)
                try:
                    async for response in stream:
                        if publisher.done():publisher.result()
                        for task in tasks:
                            if task.done():task.result()
                        if process and process.returncode is not None:
                            if method == 'playback' and process.returncode == 0:finished = True;break
                            raise ValueError('Muxer stopped before camera completion')
                        if response.mimetype == 'application/json':
                            value = json.loads(response.plaintext)
                            params = value.get('params') or {}
                            if value.get('type') == 'response' and params.get('error_code') not in (None,0):
                                if method == 'download' and not received:unsupported=True;break
                                raise ValueError('Recording request rejected')
                            if value.get('type') == 'notification' and params.get('event_type') == 'stream_status' and params.get('status') == 'finished':finished=True;break
                            continue
                        if response.mimetype != 'video/mp2t':continue
                        data, sound = response.plaintext, response.audioPayload
                        # Buffered SDK packets must not starve the warm process's
                        # root-local heartbeats, cancellation or lease watcher.
                        await asyncio.sleep(0)
                        received += len(data)+len(sound)
                        if len(data)>1024*1024 or len(sound)>256*1024 or received>MAX_BYTES:
                            raise ValueError('Recording input exceeds budget')
                        if video is None:
                            pending.append((data,sound));pending_bytes+=len(data)+len(sound)
                            if not sound and pending_bytes<512*1024 and time.monotonic()-started<10:continue
                            video,audio = await start_mux(response.audioPayloadType if sound else None)
                            for data,sound in pending:
                                await video.put(data)
                                if audio and sound:await audio.put(sound)
                            pending=[]
                        else:
                            await video.put(data)
                            if audio and sound:await audio.put(sound)
                    if unsupported:
                        method = 'playback'
                        continue
                    if video is None and pending:
                        video,audio = await start_mux(None)
                        for data,_ in pending:await video.put(data)
                    if not finished and not (method=='playback' and process and process.returncode==0):
                        raise ValueError('Recording ended without completion')
                    if video:
                        await video.put(None)
                        if audio:await audio.put(None)
                    break
                finally:
                    await stream.aclose()
        await asyncio.wait_for(asyncio.gather(*tasks),15)
        if not process or await asyncio.wait_for(process.wait(),20) != 0:
            raise ValueError('Recording mux failed')
        view = snapshot(root,generation)
        if not view['complete'] or view['duration'] < job['end']-job['start']-5:
            raise ValueError('Incomplete recording duration')
        if playable_at is None:playable_at=time.monotonic()-started
        save(dict(last_value,state='ready',format='hls',generation=generation,segments=len(view['names']),bytes=view['bytes'],has_audio=has_audio,first_playable_seconds=round(playable_at,3),finished_seconds=round(time.monotonic()-started,3),mode=method))
    finally:
        publisher.cancel()
        for task in tasks:task.cancel()
        await asyncio.gather(publisher,*tasks,return_exceptions=True)
        if process and process.returncode is None:
            process.terminate()
            try:await asyncio.wait_for(process.wait(),3)
            except TimeoutError:process.kill();await process.wait()
