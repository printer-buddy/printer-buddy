"""Bambu Lab and Elegoo printers inside Printer Buddy, with no Moonraker server and no extra port.

bambu-convert's protocol code and its Klipper-style objects run in this process. BridgeClient is a MoonrakerClient
whose connection is a direct call into that code, so every view, panel and API built on MoonrakerClient works as is.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from .moonraker import MoonrakerClient, RPCError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "bambu-convert") not in sys.path:
    sys.path.insert(0, str(ROOT / "bambu-convert"))
from bambu_convert.server import Bridge, Client, RPCError as BridgeError  # noqa: E402

log = logging.getLogger(__name__)
PORTS = {"bambu": 8883, "cc1": 3030, "cc2": 1883}


_ffmpeg_tried = False


async def ensure_ffmpeg():
    """The camera of an X1 or H2 is a video stream that only ffmpeg can turn into pictures: install it once if it's missing."""
    global _ffmpeg_tried
    if shutil.which("ffmpeg") or _ffmpeg_tried:
        return
    _ffmpeg_tried = True
    from ..core import updater
    log.info("installing ffmpeg for the Bambu camera")
    ok = await asyncio.to_thread(updater.root, "pkg", "ffmpeg")
    log.info("ffmpeg installed" if ok and shutil.which("ffmpeg") else "couldn't install ffmpeg: run 'sudo apt install ffmpeg' on the box")


def data_dir() -> Path:
    return Path(os.path.dirname(os.path.abspath(os.getenv("DB_PATH", "data/monitor.db")))) / "bambu"


class _Pipe:
    """Where the bridge pushes its notifications: straight into the client, no socket."""

    def __init__(self, owner: "BridgeClient"):
        self.owner = owner

    async def send_text(self, text: str):
        msg = json.loads(text)
        if msg.get("method"):
            self.owner._notification(msg["method"], msg.get("params") or [])


class BridgeClient(MoonrakerClient):
    def __init__(self, printer_id: int, host: str, port: int, api_key: str | None, on_change, cfg: dict):
        super().__init__(printer_id, host, port, api_key, on_change)
        self.cfg = dict(cfg, host=host, port=port)
        self.bridge = Bridge(self.cfg, data_dir())
        self.native_camera = self.bridge.p.camera
        self._cl = Client(1, _Pipe(self), host)
        self._names: frozenset = frozenset()

    # ------------------------------------------------------------ connection

    async def _session(self):
        b = self.bridge
        if self.cfg.get("brand") != "elegoo":
            asyncio.create_task(ensure_ffmpeg())
        b.clients[self._cl.id] = self._cl
        b.p.listeners.append(self._objects_changed)
        b.start()
        try:
            info = await self._call("server.info")
            self._set(connection="online", error=None, klippy_state=info.get("klippy_state"),
                      moonraker_warnings=[], failed_components=[])
            await self._load_webcams()
            if info.get("klippy_state") == "ready":
                await self._subscribe()
            else:
                await self._load_klippy_message()
            while True:
                await asyncio.sleep(3600)
        finally:
            b.clients.pop(self._cl.id, None)
            if self._objects_changed in b.p.listeners:
                b.p.listeners.remove(self._objects_changed)
            await b.stop()

    async def _subscribe(self):
        self._names = frozenset(self.bridge.p.objects())
        await super()._subscribe()

    def _objects_changed(self):
        """The printer's first full report brings the AMS, chamber sensor and so on: subscribe to them when they appear."""
        if self.klippy_state == "ready" and frozenset(self.bridge.p.objects()) != self._names:
            asyncio.create_task(self._safe(self._subscribe()))

    async def _call(self, method: str, params: dict | None = None, timeout: float = 10.0):
        try:
            return await asyncio.wait_for(self.bridge.call(method, params or {}, self._cl), timeout)
        except BridgeError as e:
            raise RPCError(str(e)) from e

    def reason(self) -> str | None:
        return self.bridge.p.error

    # ------------------------------------------------------------ files and pictures

    async def upload(self, filename: str, data: bytes, start: bool = False, root: str = "gcodes", folder: str = ""):
        try:
            await self.bridge.upload(root, folder, filename, data, start)
        except BridgeError as e:
            raise RuntimeError(str(e)) from e

    async def read_file(self, root: str, path: str):
        raise RuntimeError("downloading printer files isn't available here")

    async def thumbnail(self, filename: str):
        if filename in self._thumbs:
            return self._thumbs[filename]
        try:
            meta = await self._call("server.files.metadata", {"filename": filename}) or {}
        except RPCError:
            return None
        thumbs = [t for t in meta.get("thumbnails") or [] if t.get("relative_path")]
        if not thumbs:
            self._thumbs[filename] = None
            return None
        fit = [t for t in thumbs if (t.get("width") or 0) <= 400]
        pick = max(fit, key=lambda t: t.get("width") or 0) if fit else min(thumbs, key=lambda t: t.get("width") or 0)
        try:
            data = await self.bridge.download(pick["relative_path"])
        except BridgeError:
            return None
        if len(self._thumbs) > 100:
            self._thumbs.clear()
        self._thumbs[filename] = ("image/png", data)
        return self._thumbs[filename]

    def snapshot_url(self, index: int = 0):
        return None

    def stream_url(self, index: int = 0):
        return None


def migrate_bridged(store) -> int:
    """Printers added through the old bambu-convert bridge (host 127.0.0.1, one port each) become direct printers."""
    conf = data_dir().parent / "bambu.json"
    try:
        entries = json.loads(conf.read_text()).get("printers", [])
    except (OSError, ValueError, AttributeError):
        return 0
    done = 0
    rows = {(r["host"], r["port"]): r for r in store.list()}
    for e in entries:
        row = rows.get(("127.0.0.1", int(e.get("port") or 0)))
        if not row or not e.get("host") or not e.get("serial"):
            continue
        kind = "elegoo" if e.get("brand") == "elegoo" else "bambu"
        key = "cc2" if str(e.get("model", "")).lower() == "cc2" else "cc1" if kind == "elegoo" else "bambu"
        extra = {k: v for k, v in e.items() if k not in ("name", "host", "port")}
        store.set_native(row["id"], kind, e["host"], PORTS[key], extra)
        done += 1
    try:
        conf.rename(conf.with_name("bambu.json.migrated"))
    except OSError:
        pass
    if done:
        log.info("moved %s bridged printer(s) to direct connections", done)
    return done
