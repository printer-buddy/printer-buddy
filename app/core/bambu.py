from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
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

    def _read(self) -> dict:
        try:
            cfg = json.loads(self.conf.read_text())
            return cfg if isinstance(cfg, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, cfg: dict):
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        self.conf.write_text(json.dumps(cfg, indent=2))

    @staticmethod
    def _free(port: int) -> bool:
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                return False
        return True

    def port_of(self, host: str, port: int) -> bool:
        return host in ("127.0.0.1", "localhost") and any(p.get("port") == port for p in self._read().get("printers", []))

    async def reload(self):
        await self.stop()
        self._task = self._proc = None
        self.start()

    async def add(self, name: str, host: str, serial: str, access_code: str) -> int:
        cfg = self._read()
        ps = cfg.setdefault("printers", [])
        if any(p.get("serial") == serial or p.get("host") == host for p in ps):
            raise ValueError("that Bambu printer is already added")
        used = {p.get("port") for p in ps}
        port = 7130
        while port in used or not self._free(port):
            port += 1
        ps.append({"name": name, "host": host, "serial": serial, "access_code": access_code, "port": port, "camera": "auto"})
        self._write(cfg)
        await self.reload()
        return port

    async def remove(self, port: int):
        cfg = self._read()
        cfg["printers"] = [p for p in cfg.get("printers", []) if p.get("port") != port]
        self._write(cfg)
        await self.reload()

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
