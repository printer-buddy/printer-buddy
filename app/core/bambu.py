from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


class BambuBridge:
    """Runs bambu-convert (Bambu Lab printers as Moonraker) next to the app while data/bambu.json lists printers."""

    def __init__(self, data_dir: str):
        self.conf = Path(data_dir) / "bambu.json"
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    def configured(self) -> bool:
        try:
            return bool(json.loads(self.conf.read_text()).get("printers"))
        except (OSError, ValueError, AttributeError):
            return False

    def start(self):
        if self.configured():
            self._task = asyncio.create_task(self._run(), name="bambu-convert")

    async def _run(self):
        while True:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "bambu_convert", "-c", str(self.conf), cwd=ROOT / "bambu-convert")
            code = await self._proc.wait()
            (log.info if code < 0 else log.warning)("bambu-convert stopped (exit %s), restarting in 10s", code)
            await asyncio.sleep(10)

    async def stop(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                self._proc.kill()
