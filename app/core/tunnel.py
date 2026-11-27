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
import json
import logging
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .updater import github_asset

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "printer-buddy.env"
BIN = ROOT / "data" / "streamer-bin"
CLOUDFLARED = BIN / "cloudflared"
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
        "armv7l": "arm", "armv6l": "arm", "i686": "386", "i386": "386"}
IDLE = 3 * 24 * 3600


def _env(key: str) -> str:
    v = ""
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith(f"{key}="):
                v = line.split("=", 1)[1].strip()
    except OSError:
        v = os.getenv(key, "")
    return v.strip()


def _setting(key: str) -> str:
    return _env(key).strip("\"'").strip().lower()


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _api() -> str:
    custom = _env("PB_TUNNEL_API")
    if custom:
        return custom
    domain = (_env("PB_SHARE_DOMAIN") or "printerbuddy.stream").strip("/").removeprefix("www.")
    return f"https://www.{domain}/api/tunnel"


def _call(method: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(_api(), data=json.dumps(body).encode(), method=method,
                                 headers={"content-type": "application/json", "user-agent": "printer-buddy"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, _dict(json.loads(r.read() or b"{}"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, _dict(json.loads(e.read() or b"{}"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def _ensure_bin() -> bool:
    if CLOUDFLARED.exists():
        return True
    a = ARCH.get(platform.machine())
    if not a:
        log.warning("tunnel: no cloudflared build for %s", platform.machine())
        return False
    BIN.mkdir(parents=True, exist_ok=True)
    part = BIN / "cloudflared.part"
    try:
        part.write_bytes(github_asset("cloudflare/cloudflared", f"cloudflared-linux-{a}"))
        part.chmod(0o755)
        part.replace(CLOUDFLARED)
        return True
    except Exception as e:
        part.unlink(missing_ok=True)
        log.warning("tunnel: couldn't download cloudflared: %s", e)
        return False


class Tunnel:
    def __init__(self, active_shares: Callable[[], int], settings: Callable[[], dict], save: Callable[[dict], None]):
        self.active_shares = active_shares
        self.settings = settings
        self.save = save
        self.proc: asyncio.subprocess.Process | None = None
        self._started = 0.0
        self._retry_at = 0.0
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self.error: str | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def start(self):
        if self._task and not self._task.done():
            return
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    def kick(self):
        if self._wake:
            self._wake.set()

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._stop_proc()

    def _setup(self) -> tuple[str, str, int] | None:
        uid, key = _setting("PB_USER_ID"), _env("PB_TUNNEL_KEY")
        if _setting("PB_REMOTE") != "on" or not uid.isdigit() or not key:
            return None
        port = _env("PORT")
        return uid, key, int(port) if port.isdigit() else 8080

    async def _loop(self):
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("tunnel: check failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 60)
            except asyncio.TimeoutError:
                pass

    async def _tick(self):
        setup = self._setup()
        if not setup:
            self.error = ("Remote access isn't finished: run ./scripts/remote.sh off and then ./scripts/remote.sh on in the Printer Buddy folder."
                          if _setting("PB_REMOTE") == "on" else None)
            await self._stop_proc()
            return
        now = time.time()
        if self.proc and self.proc.returncode is not None:
            if now - self._started < 60:
                self._retry_at = now + 300
            log.warning("tunnel: cloudflared stopped (exit %s)", self.proc.returncode)
            self.proc = None
        if await asyncio.to_thread(self.active_shares) > 0:
            if now - float(self.settings().get("tunnel_last_active") or 0) > 300:
                self.save({"tunnel_last_active": now})
            if not self.running() and now >= self._retry_at:
                await self._launch(*setup)
        elif self.running() or self.settings().get("tunnel_up"):
            if now - float(self.settings().get("tunnel_last_active") or now) > IDLE:
                await self._release(*setup)

    async def _launch(self, uid: str, key: str, port: int):
        if not await asyncio.to_thread(_ensure_bin):
            self.error = "Couldn't download cloudflared, trying again in 10 minutes."
            self._retry_at = time.time() + 600
            return
        status, reply = await asyncio.to_thread(_call, "POST", {"id": uid, "port": port, "key": key})
        token = reply.get("token")
        if not token:
            self.error = reply.get("error") or f"the share server answered HTTP {status}"
            log.warning("tunnel: setup server said %s", self.error)
            self._retry_at = time.time() + 600
            return
        try:
            self.proc = await asyncio.create_subprocess_exec(
                str(CLOUDFLARED), "tunnel", "--no-autoupdate", "run", env={**os.environ, "TUNNEL_TOKEN": token},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.error = f"Couldn't start cloudflared ({e}), trying again in 10 minutes."
            log.warning("tunnel: %s", self.error)
            self._retry_at = time.time() + 600
            return
        self._started = time.time()
        self.error = None
        self.save({"tunnel_up": True})
        log.info("tunnel: running for box %s", uid)

    async def _release(self, uid: str, key: str, _port: int):
        await self._stop_proc()
        status, reply = await asyncio.to_thread(_call, "DELETE", {"id": uid, "key": key, "hold": True})
        if 200 <= status < 300:
            self.save({"tunnel_up": False})
            log.info("tunnel: no share link for 3 days, tunnel released (ID kept)")
        else:
            log.warning("tunnel: couldn't release the tunnel yet: %s", reply.get("error") or f"HTTP {status}")

    async def _stop_proc(self):
        proc, self.proc = self.proc, None
        if not proc or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
