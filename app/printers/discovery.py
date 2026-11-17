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
import ipaddress
import logging
import os
import socket

from ..core.net import get_json

log = logging.getLogger(__name__)

MDNS_TYPE = "_moonraker._tcp.local."
PROBE_PORTS = [int(p) for p in os.getenv("PROBE_PORTS", "7125,80").split(",") if p.strip()]
MAX_HOSTS = 4096


def local_subnets() -> list[ipaddress.IPv4Network]:
    env = os.getenv("SCAN_SUBNETS")
    if env:
        return [ipaddress.ip_network(s.strip(), strict=False) for s in env.split(",") if s.strip()]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return []
    finally:
        s.close()
    return [ipaddress.ip_network(f"{ip}/24", strict=False)]


async def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except Exception:
        return False
    w.close()
    return True


async def _mdns(duration: float) -> dict[str, int]:
    try:
        from zeroconf import IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:
        return {}
    names: set[str] = set()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            names.add(name)

    found: dict[str, int] = {}
    azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        browser = AsyncServiceBrowser(azc.zeroconf, [MDNS_TYPE], handlers=[on_change])
        await asyncio.sleep(duration)
        for name in names:
            info = AsyncServiceInfo(MDNS_TYPE, name)
            if await info.async_request(azc.zeroconf, 2000):
                for addr in info.parsed_addresses(IPVersion.V4Only):
                    found[addr] = info.port or 7125
        await browser.async_cancel()
    except Exception as e:
        log.warning("mDNS browse failed: %s", e)
    finally:
        await azc.async_close()
    return found


async def probe(host: str, port: int, api_key: str | None = None) -> dict | None:
    base = f"http://{host}:{port}"
    hdrs = {"X-Api-Key": api_key} if api_key else {}
    try:
        status, data = await get_json(base + "/server/info", hdrs, timeout=3)
    except Exception:
        return None
    info = {
        "host": host, "port": port, "name": host, "needs_auth": False, "auth_ok": True,
        "klippy_state": None, "moonraker_version": None, "klipper_version": None, "has_webcam": False,
    }
    if status in (401, 403):
        # Moonraker with auth enabled; /access/info is always public, so use it to confirm.
        try:
            s2, d2 = await get_json(base + "/access/info", timeout=3)
        except Exception:
            return None
        if s2 == 200 and isinstance(d2, dict) and "result" in d2:
            info.update(needs_auth=True, auth_ok=False)
            return info
        return None
    if status != 200 or not isinstance(data, dict) or "klippy_state" not in (data.get("result") or {}):
        return None
    r = data["result"]
    info["klippy_state"] = r.get("klippy_state")
    info["moonraker_version"] = r.get("moonraker_version")
    info["needs_auth"] = bool(api_key)

    async def opt(path):
        try:
            s, d = await get_json(base + path, hdrs, timeout=3)
            return (d or {}).get("result") if s == 200 and isinstance(d, dict) else None
        except Exception:
            return None

    pinfo, cams = await asyncio.gather(opt("/printer/info"), opt("/server/webcams/list"))
    if pinfo:
        info["name"] = pinfo.get("hostname") or host
        info["klipper_version"] = pinfo.get("software_version")
    if cams:
        info["has_webcam"] = any(c.get("enabled", True) for c in cams.get("webcams", []))
    return info


async def scan(mdns_seconds: float = 3.0) -> list[dict]:
    candidates: dict[str, list[int]] = {}
    sem = asyncio.Semaphore(256)

    async def sweep(host: str):
        async with sem:
            for port in PROBE_PORTS:
                if await _port_open(host, port):
                    candidates.setdefault(host, []).append(port)

    hosts = []
    for net in local_subnets():
        if net.num_addresses > MAX_HOSTS:
            log.warning("skipping %s: too large to sweep", net)
            continue
        hosts += [str(h) for h in (net.hosts() if net.num_addresses > 1 else [net.network_address])]

    mdns_task = asyncio.create_task(_mdns(mdns_seconds))
    await asyncio.gather(*(sweep(h) for h in hosts))
    for host, port in (await mdns_task).items():
        ports = candidates.setdefault(host, [])
        if port not in ports:
            ports.insert(0, port)

    async def identify(host: str, ports: list[int]):
        for port in ports:
            if info := await probe(host, port):
                return info
        return None

    results = [r for r in await asyncio.gather(*(identify(h, p) for h, p in candidates.items())) if r]
    return sorted(results, key=lambda r: ipaddress.ip_address(r["host"]))
