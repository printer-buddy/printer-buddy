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
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "bin" / "python"
STREAMER_DIR = ROOT / "streamer"
CONF = ROOT / "data" / "streamer.conf"
BIN = ROOT / "data" / "streamer-bin"


def enabled() -> bool:
    return os.getenv("PB_STREAM", "off").strip().lower() != "off"


class Streamer:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def start(self):
        if self._task and not self._task.done():
            return
        if enabled():
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._stop_proc()

    async def restart(self):
        await self.stop()
        self.start()

    def _env(self) -> dict:
        return {**os.environ,
                "PB_MJPG_DIR": str(BIN / "mjpg-streamer"),
                "PATH": f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}",
                "LD_LIBRARY_PATH": f"{BIN / 'lib'}{os.pathsep}{os.environ.get('LD_LIBRARY_PATH', '')}"}

    async def _run(self):
        if os.getenv("PB_STREAM", "").strip().lower() == "advanced":
            from .camera import _ensure_ffmpeg
            await asyncio.to_thread(_ensure_ffmpeg)
        delay = 2
        while True:
            if not CONF.exists() or not PYTHON.exists() or not STREAMER_DIR.exists():
                return
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    str(PYTHON), "-m", "streamer", "-c", str(CONF),
                    cwd=str(STREAMER_DIR), env=self._env(),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except Exception as e:
                log.warning("camera streamer: couldn't start: %s", e)
                return
            started = asyncio.get_event_loop().time()
            asyncio.create_task(self._drain(self.proc))
            await self.proc.wait()
            if getattr(self.proc, "pb_stopping", False):
                return
            delay = 2 if asyncio.get_event_loop().time() - started > 30 else min(delay * 2, 60)
            log.warning("camera streamer stopped (exit %s); restarting in %ds", self.proc.returncode, delay)
            await asyncio.sleep(delay)

    async def _drain(self, proc):
        try:
            async for line in proc.stderr:
                t = line.decode(errors="replace").strip()
                if " ERROR " in t or " WARNING " in t:
                    log.warning("streamer: %s", t[-300:])
        except Exception:
            pass

    async def _stop_proc(self):
        proc, self.proc = self.proc, None
        if not proc or proc.returncode is not None:
            return
        proc.pb_stopping = True
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
