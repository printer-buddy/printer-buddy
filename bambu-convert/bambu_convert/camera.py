import asyncio
import logging
import shutil
import ssl
import struct
import time

log = logging.getLogger("bambu.camera")
IDLE_STOP = 30


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Camera:
    """Latest JPEG from the printer: port 6000 on P1 / A1, RTSP (via ffmpeg) on X1 / H2. Runs only while watched."""

    def __init__(self, host: str, access_code: str):
        self.host, self.code = host, access_code
        self.rtsp_url: str | None = None
        self.frame: bytes | None = None
        self.frame_at = 0.0
        self._wanted = 0.0
        self._task: asyncio.Task | None = None
        self._new = asyncio.Condition()

    def _touch(self):
        self._wanted = time.monotonic()
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def snapshot(self, timeout: float = 8) -> bytes | None:
        self._touch()
        if self.frame and time.monotonic() - self.frame_at < 2:
            return self.frame
        try:
            async with self._new:
                await asyncio.wait_for(self._new.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.frame

    async def frames(self):
        last = None
        while True:
            self._touch()
            try:
                async with self._new:
                    await asyncio.wait_for(self._new.wait(), 10)
            except asyncio.TimeoutError:
                continue
            if self.frame is not last:
                last = self.frame
                yield last

    async def _publish(self, jpeg: bytes):
        self.frame, self.frame_at = jpeg, time.monotonic()
        async with self._new:
            self._new.notify_all()

    async def _run(self):
        try:
            while time.monotonic() - self._wanted < IDLE_STOP:
                try:
                    if self.rtsp_url:
                        await self._rtsp()
                    else:
                        await self._jpeg()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.info("camera %s: %s", self.host, e)
                    await asyncio.sleep(3)
        finally:
            self._task = None

    async def _jpeg(self):
        r, w = await asyncio.wait_for(asyncio.open_connection(self.host, 6000, ssl=_ctx(), server_hostname=None), 10)
        try:
            auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
            auth += b"bblp".ljust(32, b"\0") + self.code.encode().ljust(32, b"\0")
            w.write(auth)
            await w.drain()
            while time.monotonic() - self._wanted < IDLE_STOP:
                head = await asyncio.wait_for(r.readexactly(16), 15)
                size = struct.unpack("<I", head[:4])[0]
                if size > 4_000_000:
                    raise ConnectionError("camera refused the access code")
                data = await asyncio.wait_for(r.readexactly(size), 15)
                if data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
                    await self._publish(data)
        finally:
            w.close()

    async def _rtsp(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is needed for this printer's camera (RTSP)")
        url = self.rtsp_url.replace("rtsps://", f"rtsps://bblp:{self.code}@", 1)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-loglevel", "error", "-tls_verify", "0", "-rtsp_transport", "tcp", "-i", url, "-an", "-r", "5",
            "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        buf = b""
        try:
            while time.monotonic() - self._wanted < IDLE_STOP:
                chunk = await asyncio.wait_for(proc.stdout.read(65536), 15)
                if not chunk:
                    err = (await proc.stderr.read()).decode(errors="replace").strip().splitlines()
                    raise RuntimeError(f"ffmpeg: {err[-1] if err else 'stream ended'}")
                buf += chunk
                while (end := buf.find(b"\xff\xd9")) != -1:
                    start = buf.find(b"\xff\xd8")
                    if start != -1 and start < end:
                        await self._publish(buf[start:end + 2])
                    buf = buf[end + 2:]
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
