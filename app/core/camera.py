#LICENSE
#
#Copyright (C) 2026  sterling motuz <Sterling@dexabyte.ca>
#
#for https://github.com/printer-buddy/printer-buddy
#
#SPDX-License-Identifier: AGPL-3.0-only
#
#Additional term (AGPL-3.0 section 7b, keep author credit): keep this header
#
#Additional term (AGPL-3.0 section 7c, mark changed versions): if you make any changes, you must include a CHANGELOG with all changes and their dates
from __future__ import annotations

import asyncio
import io
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from urllib.parse import quote

from .net import http_get, http_put

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "data" / "streamer-bin"
CONF = ROOT / "data" / "go2rtc-pb.yaml"
BASE = "http://127.0.0.1:1984"

GO2RTC_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm", "armv6l": "armv6"}
FFMPEG_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
               "armv7l": "armhf", "armv6l": "armel", "i686": "i686", "i386": "i686"}


def stream_name(pid: int, index: int) -> str:
    return f"pb_{pid}_{index}"


def mjpeg_url(name: str) -> str:
    return f"{BASE}/api/stream.mjpeg?src={quote(name)}"


def frame_url(src: str) -> str:
    return f"{BASE}/api/frame.jpeg?src={quote(src, safe='')}"


def _fetch(url: str, timeout: int = 180) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "printer-buddy"}), timeout=timeout).read()


def _ensure_go2rtc() -> str | None:
    if found := shutil.which("go2rtc"):
        return found
    dest = BIN / "go2rtc"
    if dest.exists():
        return str(dest)
    a = GO2RTC_ARCH.get(platform.machine())
    if not a:
        log.warning("go2rtc: no build for %s", platform.machine())
        return None
    try:
        BIN.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_fetch(f"https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_{a}"))
        dest.chmod(0o755)
        return str(dest)
    except Exception as e:
        log.warning("go2rtc download failed: %s", e)
        return None


def _ensure_ffmpeg() -> str | None:
    if found := shutil.which("ffmpeg"):
        return found
    dest = BIN / "ffmpeg"
    if dest.exists():
        return str(dest)
    a = FFMPEG_ARCH.get(platform.machine())
    if not a:
        log.warning("ffmpeg: no static build for %s", platform.machine())
        return None
    try:
        BIN.mkdir(parents=True, exist_ok=True)
        data = _fetch(f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{a}-static.tar.xz")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as t:
            member = next((m for m in t.getmembers() if m.name.endswith("/ffmpeg")), None)
            if not member:
                log.warning("ffmpeg: binary not found in archive")
                return None
            dest.write_bytes(t.extractfile(member).read())
        dest.chmod(0o755)
        return str(dest)
    except Exception as e:
        log.warning("ffmpeg download failed: %s", e)
        return None


class Go2rtc:
    """Printer Buddy's own go2rtc, for turning RTSP/HLS cameras into MJPEG the relay can carry.
    Binaries (go2rtc + a static ffmpeg) are fetched into data/streamer-bin on first use, no root.
    An already-running go2rtc on the port (the streamer's advanced tier) is reused as-is."""

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._prep: asyncio.Task | None = None

    async def _serving(self) -> bool:
        try:
            status, _, _ = await http_get(f"{BASE}/api/streams", timeout=2)
            return status == 200
        except Exception:
            return False

    async def available(self) -> bool:
        if self.proc is not None and self.proc.returncode is None:
            return True
        if await self._serving():
            return True
        if self._prep is None or self._prep.done():
            self._prep = asyncio.create_task(self._prepare())
        return False

    async def _prepare(self):
        loop = asyncio.get_event_loop()
        go2rtc = await loop.run_in_executor(None, _ensure_go2rtc)
        ffmpeg = await loop.run_in_executor(None, _ensure_ffmpeg)
        if not go2rtc or (self._task and not self._task.done()):
            return
        CONF.parent.mkdir(parents=True, exist_ok=True)
        CONF.write_text('api:\n  listen: ":1984"\n', encoding="utf-8")
        env = {**os.environ}
        if ffmpeg:
            env["PATH"] = f"{BIN}{os.pathsep}{env.get('PATH', '')}"
        self._task = asyncio.create_task(self._run(go2rtc, env))

    async def _run(self, binary: str, env: dict):
        global _gen
        delay = 2
        while True:
            _registered.clear(); _gen += 1   # a (re)started go2rtc has no runtime streams; re-register on next ensure()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    binary, "-config", str(CONF), env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log.warning("go2rtc couldn't start: %s", e)
                return
            await self.proc.wait()
            if getattr(self.proc, "pb_stopping", False):
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def stop(self):
        for t in (self._prep, self._task):
            if t:
                t.cancel()
        self._prep = self._task = None
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            proc.pb_stopping = True
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


_g = Go2rtc()
_registered: dict[str, str] = {}   # stream name -> src already registered with the running go2rtc; reused instead of re-PUT
_gen = 0                           # bumped on every registry clear, so a PUT finishing across a (re)start isn't cached stale


async def available() -> bool:
    return await _g.available()


async def ensure(name: str, src: str) -> bool:
    if _registered.get(name) == src:   # already the warm stream for this source — don't replace it (keeps live viewers' stream)
        return True
    gen = _gen
    try:
        status, _, _ = await http_put(f"{BASE}/api/streams?src={quote(src, safe='')}&name={quote(name)}", timeout=5)
        if status in (200, 201) and gen == _gen:   # go2rtc wasn't (re)started while we registered
            _registered[name] = src
            return True
        return False
    except Exception as e:
        log.info("go2rtc add stream %s: %s", name, e)
        return False


def forget(name: str) -> None:   # drop a registration whose stream turned out to be gone (e.g. an external go2rtc restart)
    _registered.pop(name, None)


async def stop():
    global _gen
    _registered.clear(); _gen += 1
    await _g.stop()
