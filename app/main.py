from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import re
import logging
import os
import secrets
import signal
import time
import zlib
from pathlib import Path
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .printers import discovery
from .printers.manager import Manager
from .core.net import get_json, http_get, http_stream
from .core.notify import ALL_EVENTS, PROGRESS_STEPS, SERVICES, Notifier, advanced as notify_advanced
from .core.store import Store
from .core.bambu import BambuBridge
from .core import components as comp
from .media import timelapse as tl
from .media.timelapse import Timelapse
from .ai import detector as ai_mod
from .ai.detector import AI
from .ai import collect as collect_mod
from .ai.collect import Collector

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class _QuietShutdown(logging.Filter):
    """On restart, uvicorn cuts open live-update connections after 3 s (--timeout-graceful-shutdown) and logs
    each one as a long CancelledError traceback. That's expected, not an error: drop just those records."""
    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, asyncio.CancelledError)


logging.getLogger("uvicorn.error").addFilter(_QuietShutdown())
STATIC = Path(__file__).resolve().parent.parent / "ui"

store = Store(os.getenv("DB_PATH", "data/monitor.db"))
manager = Manager(store, Notifier(store))
from .printers import moonraker as _mr   # noqa: E402
_mr.ETA_MODE = store.settings().get("eta_mode", "file") if store.settings().get("eta_mode") in ("file", "slicer", "filament") else "file"
manager.timelapse = Timelapse(store, os.path.dirname(os.path.abspath(os.getenv("DB_PATH", "data/monitor.db"))),
                              manager.snapshot, manager.cam_transform)
manager.ai = AI(store, os.path.dirname(os.path.abspath(os.getenv("DB_PATH", "data/monitor.db"))),
                manager.snapshot, manager.cam_transform, manager.ai_mode, manager.ai_alert, manager.ai_update)
manager.collect = Collector(store, os.path.dirname(os.path.abspath(os.getenv("DB_PATH", "data/monitor.db"))),
                            lambda: share_user_id(), lambda: manager.ai.model is not None)
bambu = BambuBridge(os.path.dirname(os.path.abspath(os.getenv("DB_PATH", "data/monitor.db"))))
scan_lock = asyncio.Lock()


def err(msg: str, status: int = 400):
    return JSONResponse({"error": msg}, status_code=status)


async def body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pid(request: Request) -> int | None:
    pid = int(request.path_params["pid"])
    return pid if pid in manager.clients else None


async def manifest(request: Request):
    return JSONResponse({
        "name": "Printer Buddy", "short_name": "Printer Buddy", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#0B0B0D", "theme_color": "#0B0B0D",
        "icons": [{"src": "/static/assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/assets/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
    }, media_type="application/manifest+json")


SW = """// Printer Buddy service worker: only here so it can be installed as an app. Everything always comes from the network.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
"""


async def service_worker(request: Request):
    return Response(SW, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


async def index(request: Request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def scan(request: Request):
    if scan_lock.locked():
        return err("a scan is already running", 409)
    async with scan_lock:
        found = await discovery.scan()
    for p in found:
        row = store.find(p["host"], p["port"])
        p["added_id"] = row["id"] if row else None
    return JSONResponse({"printers": found})


async def probe(request: Request):
    b = await body(request)
    host = str(b.get("host", "")).strip()
    if not host:
        return err("host is required")
    ports = [int(b["port"])] if b.get("port") else discovery.PROBE_PORTS
    for port in ports:
        if info := await discovery.probe(host, port, b.get("api_key") or None):
            row = store.find(host, port)
            info["added_id"] = row["id"] if row else None
            return JSONResponse(info)
    return err(f"no Moonraker found at {host}", 404)


async def list_printers(request: Request):
    return JSONResponse({"printers": manager.views()})


async def add_printer(request: Request):
    b = await body(request)
    host = str(b.get("host", "")).strip()
    try:
        port = int(b.get("port") or 7125)
    except ValueError:
        return err("bad port")
    api_key = (b.get("api_key") or "").strip() or None
    if not host:
        return err("host is required")
    bam = b.get("bambu") if isinstance(b.get("bambu"), dict) else None
    if bam:
        serial, code = str(bam.get("serial", "")).strip(), str(bam.get("access_code", "")).strip()
        if not serial or not code:
            return err("a Bambu printer needs its serial number and access code")
        try:
            port = await bambu.add((b.get("name") or "").strip() or f"Bambu {serial[-4:]}", host, serial, code)
        except ValueError as e:
            return err(str(e), 409)
        real, host = host, "127.0.0.1"
        info = None
        for _ in range(20):
            info = await discovery.probe(host, port, None)
            if info:
                break
            await asyncio.sleep(1)
        if not info:
            await bambu.remove(port)
            return err(f"couldn't reach the Bambu printer at {real}: check the IP, serial number and access code, and that LAN mode is on", 404)
    if store.find(host, port):
        return err("that printer is already added", 409)
    if not bam:
        info = await discovery.probe(host, port, api_key)
    if not info:
        return err(f"no Moonraker found at {host}:{port}", 404)
    if info["needs_auth"] and not info["auth_ok"]:
        return err("this printer needs a valid API key" if api_key else "this printer needs an API key", 401)
    name = (b.get("name") or "").strip() or info["name"]
    return JSONResponse(manager.add(name, host, port, api_key), status_code=201)


async def update_printer(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    b = await body(request)
    if "ai" in b:
        if b["ai"] not in ai_mod.MODES:
            return err(f"ai must be one of: {', '.join(ai_mod.MODES)}")
        manager.set_ai_mode(pid, b["ai"])
    if "notify" in b:
        manager.set_notify(pid, bool(b["notify"]))
    if not any(k in b for k in ("name", "group", "camera", "notify", "ai")):
        return err("nothing to change")
    if "camera" in b:
        c = b.get("camera")
        if c is None:
            cam = None
        elif isinstance(c, dict) and isinstance(c.get("cams") or {}, dict):
            cams = {}
            for k, v in (c.get("cams") or {}).items():
                if not str(k).isdigit() or not isinstance(v, dict):
                    return err("bad camera settings")
                try:
                    rotation = int(v.get("rotation") or 0)
                except (TypeError, ValueError):
                    return err("bad camera settings")
                refresh = v.get("refresh")
                if rotation not in (0, 90, 180, 270) or refresh not in (None, 1, 2, 5, 10):
                    return err("bad camera settings")
                cams[str(int(k))] = {"flip_h": bool(v.get("flip_h")), "flip_v": bool(v.get("flip_v")),
                                     "rotation": rotation, "refresh": refresh}
            try:
                main_cam = max(0, int(c.get("main") or 0))
            except (TypeError, ValueError):
                return err("bad camera settings")
            cam = {"main": main_cam, "cams": cams}
        else:
            return err("bad camera settings")
        manager.set_camera(pid, cam)
    if "name" in b:
        name = str(b.get("name") or "").strip()
        if not name:
            return err("name is required")
        manager.rename(pid, name)
    if "group" in b:
        manager.set_group(pid, str(b.get("group") or "").strip()[:40] or None)
    return JSONResponse(manager.view(pid))


async def set_order(request: Request):
    ids = (await body(request)).get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        return err("ids must be a list of printer ids")
    manager.set_order(ids)
    return JSONResponse({"ok": True})


async def delete_printer(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    row = store.get(pid)
    await manager.remove(pid)
    if row and bambu.port_of(row.get("host", ""), row.get("port")):
        await bambu.remove(row["port"])
    return Response(status_code=204)


async def snapshot(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    try:
        cam = int(request.query_params.get("cam", 0))
    except ValueError:
        return err("bad camera number")
    url = manager.clients[pid].snapshot_url(cam)
    if not url or not url.startswith("http://"):
        return err("no webcam", 404)
    try:
        status, headers, data = await http_get(url, timeout=6)
    except Exception as e:
        return err(f"webcam unreachable: {e}", 502)
    if status != 200:
        return err(f"webcam returned {status}", 502)
    return Response(data, media_type=headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "no-store"})


async def live_stream(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    if setup()["video"] not in ("live", "timelapse"):
        return err("live video is off (installer: Video)", 404)
    try:
        cam = int(request.query_params.get("cam", 0))
    except ValueError:
        return err("bad camera number")
    url = manager.clients[pid].stream_url(cam)
    if not url or not url.startswith("http://"):
        return err("this camera has no MJPEG stream", 404)
    try:
        upstream = await http_stream(url)
    except Exception as e:
        return err(f"camera unreachable: {e}", 502)
    if upstream.status != 200:
        upstream.close()
        return err(f"camera returned {upstream.status}", 502)

    async def relay():
        try:
            async for chunk in upstream.chunks():
                yield chunk
        finally:
            upstream.close()

    return StreamingResponse(relay(), media_type=upstream.headers.get("content-type", "multipart/x-mixed-replace"),
                             headers={"Cache-Control": "no-store"})


async def ai_settings(request: Request):
    if request.method == "PUT":
        b = await body(request)
        values = {}
        for k, choices in ai_mod.CHOICES.items():
            if k in b:
                if b[k] not in choices:
                    return err(f"bad value for {k}")
                values[k] = b[k]
        store.save_settings(values)
        if "ai_contribute" in b:
            manager.collect.set_sharing(bool(b["ai_contribute"]))
    return JSONResponse({**manager.ai.settings(), "choices": ai_mod.CHOICES, "modes": ai_mod.MODES,
                         "model": manager.ai.info(), "ai_contribute": manager.collect.sharing()})


async def ui_layout(request: Request):
    key = "ui_layout_home" if request.query_params.get("scope") == "home" else "ui_layout"
    if request.method == "PUT":
        b = await body(request)
        layout = b.get("layout")
        if not isinstance(layout, dict) or not isinstance(layout.get("tabs"), list) or len(json.dumps(layout)) > 200_000:
            return err("bad layout")
        store.save_settings({key: layout})
    return JSONResponse({"layout": store.settings().get(key)})


# source -> (theme folder in the config root, {our part: their file names, first found wins}, Moonraker database namespace)
THEME_SOURCES = {
    "fluidd": (".fluidd-theme", {"background": ["background"], "logo": ["sidebar-logo", "logo"]}, "fluidd"),
    "mainsail": (".theme", {"background": ["main-background"], "logo": ["sidebar-logo"], "sidebar": ["sidebar-background"]}, "mainsail"),
}
THEME_IMG = (".png", ".jpg", ".jpeg", ".gif", ".svg")
THEME_DIR = Path(os.path.dirname(os.path.abspath(os.getenv("DB_PATH", "data/monitor.db")))) / "theme"
THEME_CSS = {"page": ("html", "body"), "panel": (".v-card",), "head": (".card-heading", ".v-toolbar"),
             "over": ("body::before", "body::after", "html::before", "html::after", "#app::before", "#app::after")}
THEME_PROP = re.compile(r"^(-webkit-)?(background(-[a-z]+)?|backdrop-filter)$")
THEME_OVER = {"opacity", "width", "height", "top", "left", "right", "bottom", "transform"}
THEME_URL = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""")


def _theme_css(css: str) -> dict:
    """{page | panel | head: "prop: value; ..."} from a custom.css: backgrounds and blur only, nothing that could run or break out."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    found = {k: {} for k in THEME_CSS}
    for sel, block in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
        sels = [x.strip() for x in sel.split(",")]
        for part, keys in THEME_CSS.items():
            if not any(x == k or (k.startswith(".") and k in x) for x in sels for k in keys):
                continue
            if part == "head" and any(".v-card" in x for x in sels):
                continue
            for d in block.split(";"):
                prop, _, val = d.partition(":")
                prop, val = prop.strip().lower(), val.replace("!important", "").strip()
                if (not (THEME_PROP.match(prop) or (part == "over" and prop in THEME_OVER)) or not val or val in ("transparent", "none", "initial", "inherit")
                        or re.search(r"[<>{}\\]|expression\(|javascript:", val, re.I)):
                    continue
                found[part][prop] = val
    return {k: "; ".join(f"{p}: {v}" for p, v in d.items()) for k, d in found.items() if d}


def _config_url(u: str) -> str | None:
    m = re.match(r"^/?server/files/config/(.+)$", u.strip())
    return m.group(1) if m and ".." not in m.group(1).split("/") else None


async def _read_file_bytes(client, path: str, cap: int) -> bytes:
    up = await client.read_file("config", path)
    data = bytearray()
    try:
        async for chunk in up.chunks():
            data += chunk
            if len(data) > cap:
                raise ValueError(f"{path} is too big")
    finally:
        up.close()
    return bytes(data)


async def _read_theme(client, source: str) -> dict:
    folder, parts, ns = THEME_SOURCES[source]
    out = {"source": source, "color": None, "mode": None, "files": {}, "css": {}}
    logo = None
    with contextlib.suppress(Exception):
        ui = (await client.rpc("server.database.get_item", {"namespace": ns, "key": "uiSettings"}, timeout=10) or {}).get("value") or {}
        if source == "fluidd":
            t = ui.get("theme") or {}
            out["color"], out["mode"] = t.get("color"), (None if t.get("isDark") is None else "dark" if t["isDark"] else "light")
            logo = _config_url(str((t.get("logo") or {}).get("src") or ""))
        else:
            out["color"], out["mode"] = ui.get("primary"), ui.get("mode")
    names = set()
    with contextlib.suppress(Exception):
        names = {f.get("filename", "") for f in (await client.rpc("server.files.get_directory", {"path": f"config/{folder}"}, timeout=10) or {}).get("files", [])}
    for part, bases in parts.items():
        hit = next((b + x for b in bases for x in THEME_IMG if b + x in names), None)
        if hit:
            out["files"][part] = f"{folder}/{hit}"
    if logo and logo.startswith(folder + "/") and logo.split("/", 1)[1] in names:
        out["files"]["logo"] = logo
    if "custom.css" in names:
        with contextlib.suppress(Exception):
            out["css"] = _theme_css((await _read_file_bytes(client, f"{folder}/custom.css", 500_000)).decode("utf-8", "replace"))
    if not isinstance(out["color"], str) or not out["color"].startswith("#"):
        out["color"] = None
    return out


async def _copy_theme_file(client, path: str, name: str) -> str:
    name += os.path.splitext(path)[1].lower()
    (THEME_DIR / name).write_bytes(await _read_file_bytes(client, path, 15_000_000))
    return name


async def printer_theme(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    b = await body(request) if request.method == "PUT" else {}
    source = request.query_params.get("source") if request.method == "GET" else b.get("source")
    if source not in THEME_SOURCES:
        return err("source must be fluidd or mainsail")
    found = await _read_theme(client, source)
    if request.method == "GET":
        pid = request.path_params["pid"]

        def to(m):
            p = _config_url(m.group(1))
            return f'url("/api/printers/{pid}/file?root=config&path={quote(p)}")' if p else m.group(0) if m.group(1).startswith("http") else "none"
        found["css"] = {k: THEME_URL.sub(to, v) for k, v in found["css"].items()}
        return JSONResponse(found)
    parts = set(b.get("parts") or [])
    theme = {"source": source}
    if "color" in parts and found["color"]:
        theme["color"] = found["color"]
    if "mode" in parts and found["mode"] in ("dark", "light"):
        theme["mode"] = found["mode"]
    THEME_DIR.mkdir(parents=True, exist_ok=True)
    for f in THEME_DIR.iterdir():
        f.unlink()
    try:
        for part, path in found["files"].items():
            if part in parts:
                theme[part] = await _copy_theme_file(client, path, part)
        css = {}
        for part, decls in found["css"].items():
            if part not in parts:
                continue
            urls, n = {}, 0
            for u in THEME_URL.findall(decls):
                if u not in urls:
                    p = _config_url(u)
                    urls[u] = f'url("/api/ui/theme/{await _copy_theme_file(client, p, f"{part}-{n}")}")' if p else f'url("{u}")' if u.startswith("http") else "none"
                    n += 1
            css[part] = THEME_URL.sub(lambda m: urls[m.group(1)], decls)
        if css:
            theme["css"] = css
    except Exception as ex:
        return err(f"couldn't copy the theme: {ex}", 502)
    store.save_settings({"ui_theme": theme})
    return JSONResponse({"theme": theme})


async def ui_theme(request: Request):
    if request.method == "DELETE":
        if THEME_DIR.exists():
            for f in THEME_DIR.iterdir():
                f.unlink()
        store.save_settings({"ui_theme": {}})
    return JSONResponse({"theme": store.settings().get("ui_theme") or {}})


async def ui_theme_file(request: Request):
    name = request.path_params["name"]
    path = THEME_DIR / name
    if "/" in name or "\\" in name or not path.is_file():
        return err("not found", 404)
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


KV_KEYS = ("presets", "notes", "maintenance", "console_filters")


async def ui_eta(request: Request):
    mr = _mr
    if request.method == "PUT":
        mode = (await body(request)).get("mode")
        if mode not in ("file", "slicer", "filament"):
            return err("bad mode")
        store.save_settings({"eta_mode": mode})
        mr.ETA_MODE = mode
    return JSONResponse({"mode": mr.ETA_MODE})


async def printer_kv(request: Request):
    pid, key = request.path_params["pid"], request.query_params.get("key", "")
    if key not in KV_KEYS:
        return err("unknown key", 404)
    name = f"kv_{pid}_{key}"
    if request.method == "PUT":
        b = await body(request)
        if "value" not in b or len(json.dumps(b["value"])) > 200_000:
            return err("bad value")
        store.save_settings({name: b["value"]})
    return JSONResponse({"value": store.settings().get(name)})


_notices = {"at": 0.0, "items": []}


async def notices(request: Request):
    if time.time() - _notices["at"] < 600 and request.query_params.get("refresh") != "1":
        return JSONResponse({"items": _notices["items"]})
    names = {p["id"]: p["name"] for p in store.list()}

    async def one(pid, client):
        out = []
        if client.connection != "online":
            return out
        name = names.get(pid, f"Printer {pid}")
        try:
            info = await client.rpc("server.info", timeout=8) or {}
            for w in info.get("warnings") or []:
                out.append({"id": f"w-{pid}-{zlib.crc32(str(w).encode())}", "title": f"{name}: Moonraker warning", "body": str(w)[:600], "level": "important"})
            for c in info.get("failed_components") or []:
                out.append({"id": f"f-{pid}-{c}", "title": f"{name}: Moonraker part failed to start: {c}",
                            "body": "Check moonraker.log on that printer.", "level": "important"})
        except Exception:
            pass
        try:
            res = await client.rpc("server.announcements.list", {"include_dismissed": False}, timeout=8) or {}
            for a in res.get("entries") or []:
                if a.get("dismissed"):
                    continue
                out.append({"id": f"a-{pid}-{a.get('entry_id')}", "title": f"{name}: {a.get('title') or 'Announcement'}",
                            "body": str(a.get("description") or "")[:600], "link": a.get("url"),
                            "level": "important" if a.get("priority") == "high" else "update",
                            "date": time.strftime("%Y-%m-%d", time.localtime(a["date"])) if a.get("date") else None})
        except Exception:
            pass
        return out

    groups = await asyncio.gather(*(one(pid, c) for pid, c in list(manager.clients.items())))
    seen, items = set(), []
    for g in groups:
        for it in g:
            key = it["id"].split("-", 2)[0] + it["title"].split(": ", 1)[-1] + (it.get("body") or "")[:80]
            if it["id"].startswith("a-") and key in seen:
                continue
            seen.add(key)
            items.append(it)
    _notices.update(at=time.time(), items=items)
    return JSONResponse({"items": items})


_news = {"at": 0.0, "items": []}


def _fetch_json(url: str) -> tuple[int, dict]:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "printer-buddy", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, json.loads(r.read(2_000_000))


async def news(request: Request):
    if time.time() - _news["at"] > 6 * 3600 or request.query_params.get("refresh") == "1":
        domain = os.getenv("PB_SHARE_DOMAIN", "").strip().strip("/") or "printerbuddy.stream"
        try:
            status, data = await asyncio.to_thread(_fetch_json, f"https://www.{domain}/api/news")
            if status == 200 and isinstance(data, dict) and isinstance(data.get("items"), list):
                _news["items"] = [n for n in data["items"] if isinstance(n, dict) and n.get("id") and n.get("title")][:50]
            _news["at"] = time.time()
        except Exception as e:
            logging.getLogger(__name__).warning("news: %s", e)
            _news["at"] = time.time() - 5.5 * 3600
    return JSONResponse({"items": _news["items"]})


def _git(*args) -> str:
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(Path(__file__).resolve().parent.parent), *args],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


VERSION_INFO = {"commit": _git("rev-parse", "--short", "HEAD"), "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                "date": _git("log", "-1", "--format=%cs")}


async def ai_check(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    try:
        return JSONResponse(await manager.ai.check_now(pid))
    except RuntimeError as e:
        return err(str(e), 409)


async def ai_history(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    return JSONResponse({"history": manager.ai.state.get(pid, {}).get("history", [])})


async def ai_overlay(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    mode = (await body(request)).get("mode")
    if mode not in ai_mod.BOX_MODES:
        return err("mode must be one of: " + ", ".join(ai_mod.BOX_MODES), 400)
    manager.ai.set_box_mode(pid, mode)
    return JSONResponse({"box_mode": manager.ai.box_mode(pid)})


async def ai_area(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    area = (await body(request)).get("area")
    if area is not None:
        try:
            area = [min(1., max(0., float(v))) for v in area]
        except (TypeError, ValueError):
            area = []
        if len(area) != 4 or area[2] - area[0] < 0.05 or area[3] - area[1] < 0.05:
            return err("area must be [x0, y0, x1, y1] between 0 and 1, or null", 400)
    manager.ai.set_area(pid, area)
    return JSONResponse({"area": manager.ai.area(pid)})


async def ai_photos(request: Request):
    out = manager.collect.summary()
    for it in out["items"]:
        it["img"] = f"/api/ai/photos/{it['id']}.jpg" if manager.collect.image_path(it["id"]) else None
    out["labels"] = collect_mod.LABELS
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


async def ai_photo_image(request: Request):
    path = manager.collect.image_path(request.path_params["iid"])
    if not path:
        return err("not found", 404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def ai_photo_label(request: Request):
    b = await body(request)
    if "severity" in b and "label" not in b:
        item = manager.collect.set_severity(request.path_params["iid"], b["severity"])
        return JSONResponse(item) if item else err("can't rate that photo (needs a failure label, 1-10)")
    label = b.get("label")
    item = manager.collect.set_label(request.path_params["iid"], label)
    if not item:
        return err("can't label that photo (unknown label, already sent, or gone)")
    for st in manager.ai.state.values():
        if st.get("alert_item") == item["id"]:
            st["alert_item"] = None
    if item.get("pid") in manager.rows:
        manager.ai_update(item["pid"])
    return JSONResponse(item)


async def ai_photo_delete(request: Request):
    iid = request.path_params["iid"]
    if not manager.collect.delete(iid):
        return err("not found", 404)
    for st in manager.ai.state.values():
        if st.get("alert_item") == iid:
            st["alert_item"] = None
    return JSONResponse({"ok": True})


async def ai_save(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    label = (await body(request)).get("label")
    if label not in collect_mod.LABELS:
        return err(f"label must be one of: {', '.join(collect_mod.LABELS)}")
    if not manager.ai.model:
        return err("AI detection isn't running", 409)
    item = await manager.save_for_ai(pid, label)
    if not item:
        return err("Couldn't get a picture from this printer's camera", 409)
    await manager.collect.send_now()
    item = manager.collect._read(item["id"]) or item
    return JSONResponse({**item, "sharing": manager.collect.sharing(), "error": manager.collect.last_error}, status_code=201)


async def components_list(request: Request):
    return JSONResponse(await asyncio.to_thread(comp.components, manager.ai.info().get("tag")))


async def components_action(request: Request):
    """POST {update: core|parts|ai} | {channel} | {part, tier} | {part, dev} | {ai: kind}. Settings changes re-fetch, then restart."""
    b = await body(request)
    what = b.get("update")
    if "channel" in b:
        if b["channel"] not in ("stable", "nightly"):
            return err("channel must be stable or nightly")
        comp.env_set("PB_CHANNEL", b["channel"]); what = "parts"
    elif "dev" in b:
        part = b.get("part")
        if part not in (*comp.PARTS, "core"):
            return err("bad part")
        dev = comp.updater.dev_parts() - {part} | ({part} if b["dev"] else set())
        comp.env_set("PB_NIGHTLY", ",".join(sorted(dev)))
        what = "core" if part == "core" else "streamer" if part == "streamer" else "parts"
    elif "tier" in b:
        var, tiers, _ = comp.PARTS.get(b.get("part"), (None, (), None))
        if not var or b["tier"] not in tiers:
            return err("bad part or tier")
        comp.env_set(var, b["tier"]); what = "streamer" if b["part"] == "streamer" else "parts"
    elif "ai" in b:
        if b["ai"] not in comp.AI_KINDS:
            return err("bad AI choice")
        comp.env_set("PB_AI", b["ai"]); what = "ai" if b["ai"] != "off" else None
    if what not in ("core", "parts", "ai", "streamer", None):
        return err("update must be core, parts, ai or streamer")
    ok, out = await asyncio.to_thread(comp.run, what) if what else (True, "")
    restart = ok and what != "streamer"
    if restart:
        asyncio.get_running_loop().call_later(1.5, os.kill, os.getpid(), signal.SIGTERM)
    return JSONResponse({"ok": ok, "output": out, "restarting": restart})


async def system_list(request: Request):
    return JSONResponse(await asyncio.to_thread(comp.system))


async def system_action(request: Request):
    """POST {action: restart|start|stop, service} | {action: reboot|shutdown}."""
    b = await body(request)
    action, service = b.get("action"), b.get("service")
    if (action, service) == ("restart", "printer-buddy"):
        asyncio.get_running_loop().call_later(1.5, os.kill, os.getpid(), signal.SIGTERM)
        return JSONResponse({"ok": True, "output": "", "restarting": True})
    ok, out = await asyncio.to_thread(comp.system_action, action, service)
    return JSONResponse({"ok": ok, "output": out, "restarting": False})


async def ai_reload(request: Request):
    await asyncio.to_thread(manager.ai.load)
    return JSONResponse(manager.ai.info())


async def timelapse_settings(request: Request):
    if request.method == "PUT":
        b = await body(request)
        allowed = {"tl_mode": tl.MODES, "tl_interval": tl.INTERVALS, "tl_fps": tl.FPS, "tl_keep": tl.KEEPS}
        values = {}
        for k, choices in allowed.items():
            if k in b:
                if b[k] not in choices:
                    return err(f"bad value for {k}")
                values[k] = b[k]
        store.save_settings(values)
    return JSONResponse({**manager.timelapse.settings(), "enabled": tl.enabled(),
                         "choices": {"tl_mode": tl.MODES, "tl_interval": tl.INTERVALS, "tl_fps": tl.FPS, "tl_keep": tl.KEEPS}})


async def list_timelapses(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    return JSONResponse({"timelapses": manager.timelapse.list(pid), "recording": manager.timelapse.recording(pid)})


async def timelapse_video(request: Request):
    pid = _pid(request)
    path = manager.timelapse.video_path(pid, request.path_params["tid"]) if pid is not None else None
    if not path:
        return err("not found", 404)
    download = request.query_params.get("download") == "1"
    return FileResponse(path, media_type="video/mp4",
                        filename=f"timelapse-{pid}-{request.path_params['tid']}.mp4" if download else None)


async def delete_timelapse(request: Request):
    pid = _pid(request)
    if pid is None or not manager.timelapse.delete(pid, request.path_params["tid"]):
        return err("not found", 404)
    return Response(status_code=204)


SHARE_EXPIRY = {"1h": 3600, "1d": 86400, "7d": 7 * 86400, "never": None, "print": None}


def remote_on() -> bool:
    return setup()["remote"] == "on"


def share_user_id() -> str:
    env_uid = os.getenv("PB_USER_ID", "").strip()
    if env_uid.isdigit():
        return env_uid
    uid = store.settings().get("share_user_id")
    if not uid:
        uid = str(secrets.randbelow(9_000_000) + 1_000_000)
        store.save_settings({"share_user_id": uid})
    return uid


def share_url(request: Request, pid: int, code: str) -> str:
    domain = os.getenv("PB_SHARE_DOMAIN", "").strip().strip("/")
    if not domain and setup()["remote"] == "on":
        domain = "printerbuddy.stream"
    if domain:
        host = domain if domain.startswith("www.") else f"www.{domain}"
        return f"https://{host}/share/user/{share_user_id()}/printer/{pid}/{code}"
    return f"{str(request.base_url).rstrip('/')}/share/printer/{pid}/{code}"


def share_view(request: Request, row: dict) -> dict:
    return {"code": row["code"], "url": share_url(request, row["printer_id"], row["code"]),
            "created_at": row["created_at"], "expires_at": row["expires_at"], "until_print": bool(row.get("until_print"))}


async def share_user(request: Request):
    return JSONResponse({"user_id": share_user_id()})


async def create_share(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    if not remote_on():
        return err("share links need Remote access on (installer: Change setup)", 403)
    expiry = (await body(request)).get("expires", "1d")
    if expiry not in SHARE_EXPIRY:
        return err("bad expiry")
    until = expiry == "print"
    client = manager.clients.get(pid)
    if until and (client.status.get("print_stats", {}).get("state") if client else None) not in ("printing", "paused"):
        return err("no print is running on this printer", 409)
    seconds = SHARE_EXPIRY[expiry]
    row = store.add_share(secrets.token_urlsafe(32), pid, time.time() + seconds if seconds else None, until)
    return JSONResponse(share_view(request, row), status_code=201)


async def list_shares(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    return JSONResponse({"shares": [share_view(request, r) for r in store.list_shares(pid)], "remote": remote_on()})


async def revoke_share(request: Request):
    if not store.revoke_share(request.path_params["code"]):
        return err("not found", 404)
    return Response(status_code=204)


def _valid_share(request: Request):
    if not remote_on():
        return None
    row = store.get_share(request.path_params["code"])
    pid = request.path_params["pid"]
    if (not row or row["revoked"] or row["printer_id"] != pid or pid not in manager.clients
            or (row["expires_at"] and row["expires_at"] < time.time())):
        return None
    if row["until_print"] and manager.clients[pid].status.get("print_stats", {}).get("state") in ("complete", "cancelled", "error", "standby"):
        store.revoke_share(row["code"])
        return None
    return pid


SHARE_GONE = "This share link has expired or was turned off."


async def share_page(request: Request):
    return FileResponse(STATIC / "share.html", headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex"})


async def share_status(request: Request):
    pid = _valid_share(request)
    if pid is None:
        return err(SHARE_GONE, 404)
    v = manager.view(pid)
    keep = ("name", "connection", "klippy_state", "state", "filename", "progress", "print_duration", "eta",
            "nozzle", "nozzle_target", "bed", "bed_target", "has_webcam", "layer", "total_layers")
    out = {k: v.get(k) for k in keep}
    out["cams"] = [{"name": c["name"], "live": c["live"]} for c in (v.get("cams") or [])]
    out["main_cam"] = (v.get("cam_settings") or {}).get("main", 0)
    out["video_live"] = setup()["video"] in ("live", "timelapse")
    row = store.get_share(request.path_params["code"])
    out["expires_at"] = row["expires_at"] if row else None
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


async def share_snapshot(request: Request):
    if _valid_share(request) is None:
        return err(SHARE_GONE, 404)
    return await snapshot(request)


async def share_stream(request: Request):
    if _valid_share(request) is None:
        return err(SHARE_GONE, 404)
    return await live_stream(request)


async def printer_action(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    action = request.path_params["action"]
    if action not in ("pause", "resume", "cancel", "estop"):
        return err("unknown action")
    client = manager.clients[pid]
    if client.connection != "online":
        return err("printer is offline", 409)
    try:
        await client.command(action)
    except Exception as e:
        return err(str(e) or type(e).__name__, 502)
    return JSONResponse({"ok": True})


GCODE_EXT = (".gcode", ".gco", ".g")
CONFIG_EXT = (".conf", ".cfg", ".md", ".css", ".jpg", ".jpeg", ".png", ".gif")
MAX_UPLOAD = 1024 * 1024 * 1024


def _online_client(request: Request):
    pid = _pid(request)
    if pid is None:
        return None, err("not found", 404)
    client = manager.clients[pid]
    if client.connection != "online":
        return None, err("printer is offline", 409)
    return client, None


async def temps(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    try:
        return JSONResponse({"history": await client.temperature_history(), "sensors": client.sensors()},
                            headers={"Cache-Control": "no-store"})
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)


async def thumbnail(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    name = request.query_params.get("file", "").strip().lstrip("/")
    if not name or ".." in name.split("/"):
        return err("file is required")
    try:
        got = await client.thumbnail(name)
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)
    if not got:
        return err("no preview picture in this file", 404)
    ctype, data = got
    return Response(data, media_type=ctype, headers={"Cache-Control": "max-age=3600"})


async def macros(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    return JSONResponse({"macros": client.macros})


async def list_files(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    try:
        return JSONResponse({"files": await client.list_files()})
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)


async def print_file(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    b = await body(request)
    name = str(b.get("path", "")).strip()
    if not name:
        return err("path is required")
    pid = request.path_params["pid"]
    if b.get("timelapse") is False:
        manager.timelapse.skip.add(pid)
    else:
        manager.timelapse.skip.discard(pid)
    try:
        await client.start_print(name)
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)
    return JSONResponse({"ok": True})


async def job_queue(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    action, params = "status", None
    if request.method == "POST":
        b = await body(request)
        action = b.get("action")
        if action == "add":
            names = [str(n).strip() for n in b.get("filenames") or [] if str(n).strip() and ".." not in str(n).split("/")]
            if not names:
                return err("filenames is required")
            params = {"filenames": names}
            if b.get("reset"):
                params["reset"] = True
        elif action == "remove":
            params = {"job_ids": [str(j) for j in b.get("job_ids") or []]}
            if not params["job_ids"]:
                return err("job_ids is required")
        elif action == "jump":
            params = {"job_id": str(b.get("job_id") or "")}
        elif action not in ("start", "pause"):
            return err("bad action")
    try:
        return JSONResponse(await client.job_queue(action, params))
    except Exception as ex:
        text = str(ex) or type(ex).__name__
        if "not found" in text.lower() or "-32601" in text:
            return err("The job queue isn't turned on for this printer: add [job_queue] to moonraker.conf and restart Moonraker.", 404)
        return err(text, 502)


# Moonraker methods the dashboard panels may call directly (LAN only: the tunnel only reaches /share/).
# method -> timeout in seconds. Updates can take many minutes.
RPC_ALLOW = {
    "server.files.get_directory": 20, "server.files.list": 20, "server.files.move": 30, "server.files.copy": 60, "server.files.post_directory": 15,
    "server.files.delete_directory": 30, "server.files.delete_file": 15, "server.files.metadata": 15, "server.files.zip": 120,
    "machine.proc_stats": 10, "machine.system_info": 10, "machine.services.restart": 30, "machine.services.stop": 30,
    "machine.services.start": 30, "machine.reboot": 15, "machine.shutdown": 15,
    "printer.restart": 15, "printer.firmware_restart": 15, "printer.gcode.help": 15, "printer.objects.query": 15,
    "machine.update.status": 60, "machine.update.refresh": 300, "machine.update.upgrade": 1800, "machine.update.full": 3600,
    "machine.update.client": 1800, "machine.update.klipper": 1800, "machine.update.moonraker": 1800,
    "machine.update.system": 3600, "machine.update.recover": 1800,
    "machine.device_power.devices": 15, "machine.device_power.post_device": 30,
    "server.spoolman.status": 15, "server.spoolman.get_spool_id": 15, "server.spoolman.post_spool_id": 15,
    "server.spoolman.proxy": 20,
    "machine.peripherals.usb": 20, "machine.peripherals.serial": 20, "machine.peripherals.video": 20, "machine.peripherals.canbus": 30,
    "server.history.get_job": 15, "printer.query_endstops.status": 10, "server.sensors.list": 10, "server.webcams.list": 10, "server.webcams.post_item": 15, "server.webcams.delete_item": 15, "printer.objects.list": 10,
    "server.database.list": 15, "server.database.post_backup": 120, "server.database.delete_backup": 15, "server.database.restore": 180, "server.database.compact": 180,
    "server.info": 10, "server.announcements.list": 15, "server.announcements.dismiss": 15, "printer.info": 10,
}


async def rpc(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    b = await body(request)
    method, params = b.get("method"), b.get("params")
    if method not in RPC_ALLOW:
        return err("method not allowed", 403)
    if params is not None and not isinstance(params, dict):
        return err("params must be an object")
    try:
        result = await client.rpc(method, params, timeout=RPC_ALLOW[method])
        if method in ("server.webcams.post_item", "server.webcams.delete_item"):
            await client._load_webcams()
        return JSONResponse({"result": result})
    except Exception as ex:
        text = str(ex) or type(ex).__name__
        return err(text, 404 if "not found" in text.lower() else 502)


FILE_ROOTS = ("gcodes", "config", "logs", "config_examples", "docs")


def _root_path(request: Request):
    root, path = request.query_params.get("root", "gcodes"), request.query_params.get("path", "").strip().lstrip("/")
    if root not in FILE_ROOTS or not path or ".." in path.split("/"):
        return None, None
    return root, path


async def printer_file(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    root, path = _root_path(request)
    if not root:
        return err("bad root or path")
    if request.method == "PUT":
        if root != "config":
            return err("only config files can be saved here")
        data = await request.body()
        if len(data) > 5_000_000:
            return err("file is too big", 413)
        folder, _, name = path.rpartition("/")
        try:
            await client.upload(name, data, root="config", folder=folder)
        except Exception as ex:
            return err(str(ex) or type(ex).__name__, 502)
        return JSONResponse({"ok": True})
    try:
        upstream = await client.read_file(root, path)
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)
    if upstream.status != 200:
        upstream.close()
        return err(f"Moonraker said HTTP {upstream.status}", 404 if upstream.status == 404 else 502)

    async def relay():
        try:
            async for chunk in upstream.chunks():
                yield chunk
        finally:
            upstream.close()
    name = path.rsplit("/", 1)[-1].replace('"', "")
    headers = {"Cache-Control": "no-store"}
    if request.query_params.get("download") == "1":
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    ctype = "text/plain; charset=utf-8" if root != "gcodes" or request.query_params.get("download") != "1" else "application/octet-stream"
    if name.lower().endswith(THEME_IMG) and request.query_params.get("download") != "1":
        ctype = mimetypes.guess_type(name)[0] or ctype
    return StreamingResponse(relay(), media_type=ctype, headers=headers)


async def delete_file(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    name = request.query_params.get("path", "").strip()
    if not name or ".." in name.split("/"):
        return err("bad path")
    try:
        await client.delete_file(name)
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)
    return Response(status_code=204)


async def upload_file(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    root = request.query_params.get("root", "gcodes")
    if root not in ("gcodes", "config"):
        return err("bad root")
    name = request.query_params.get("name", "").replace("\\", "/").split("/")[-1].strip()
    if not name or not name.lower().endswith(GCODE_EXT if root == "gcodes" else CONFIG_EXT) or '"' in name:
        return err("pick a .gcode file" if root == "gcodes" else f"pick a config file ({' '.join(CONFIG_EXT)})")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_UPLOAD:
            return err("file is too big (1 GB max)", 413)
    if not data:
        return err("the file is empty")
    try:
        folder = request.query_params.get("dir", "").strip().strip("/")
        if ".." in folder.split("/"):
            return err("bad folder")
        await client.upload(name, bytes(data), start=root == "gcodes" and request.query_params.get("print") == "1",
                            root=root, folder=folder)
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)
    return JSONResponse({"ok": True, "name": name})


async def history(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    try:
        start = max(0, int(request.query_params.get("start", 0)))
        limit = min(100, max(1, int(request.query_params.get("limit", 30))))
    except ValueError:
        return err("bad start/limit")
    try:
        return JSONResponse(await client.history(start, limit))
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)


async def history_totals(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    try:
        return JSONResponse(await client.history_totals())
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)


async def printer_gcode(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    script = str((await body(request)).get("script", "")).strip()
    if not script:
        return err("script is required")
    client = manager.clients[pid]
    if client.connection != "online":
        return err("printer is offline", 409)
    try:
        await client.gcode(script)
    except Exception as e:
        return err(str(e) or type(e).__name__, 502)
    return JSONResponse({"ok": True})


async def events(request: Request):
    q = manager.subscribe()

    async def stream():
        try:
            yield f"event: snapshot\ndata: {json.dumps(manager.views())}\n\n"
            while True:
                try:
                    kind, data = await asyncio.wait_for(q.get(), 15)
                    yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def get_notify_settings(request: Request):
    return JSONResponse({**manager.notifier.config(), "all_events": ALL_EVENTS, "progress_steps": PROGRESS_STEPS,
                         "advanced": notify_advanced(), "active": manager.notifier.targets()})


async def save_notify_settings(request: Request):
    b = await body(request)
    values = {}
    for k in ("ntfy_url", "ntfy_token", "discord_webhook"):
        if k in b:
            v = str(b[k] or "").strip()
            if v and k != "ntfy_token" and not v.startswith(("http://", "https://")):
                return err(f"{k.replace('_', ' ')} must start with http:// or https://")
            values[k] = v
    if "events" in b:
        if not isinstance(b["events"], list):
            return err("events must be a list")
        values["events"] = [e for e in b["events"] if e in ALL_EVENTS]
    if "attach_snapshot" in b:
        values["attach_snapshot"] = bool(b["attach_snapshot"])
    for k in ("telegram_token", "telegram_chat", "pushover_user", "pushover_token",
              "smtp_host", "smtp_user", "smtp_pass", "smtp_from", "smtp_to"):
        if k in b:
            values[k] = str(b[k] or "").strip()
    if "smtp_port" in b:
        try:
            values["smtp_port"] = int(b["smtp_port"] or 587)
        except (TypeError, ValueError):
            return err("SMTP port must be a number")
    if "smtp_tls" in b:
        if b["smtp_tls"] not in ("starttls", "ssl", "none"):
            return err("bad SMTP security option")
        values["smtp_tls"] = b["smtp_tls"]
    if "progress_step" in b:
        if b["progress_step"] not in PROGRESS_STEPS:
            return err("bad progress step")
        values["progress_step"] = b["progress_step"]
    store.save_settings(values)
    return await get_notify_settings(request)


async def test_notification(request: Request):
    only = (await body(request)).get("service")
    if only is not None and only not in SERVICES:
        return err("unknown service")
    active = manager.notifier.targets()
    if only and only not in active:
        return err(f"fill in the {only} settings and save first")
    if not active:
        return err("add a notification service first")
    image = None
    for pid in list(manager.clients):
        if image := await manager.snapshot(pid):
            break
    errors = await manager.notifier.send("Printer Buddy", "Test notification - it works!", image, only=only)
    if errors:
        return err("; ".join(errors), 502)
    return JSONResponse({"ok": True, "with_snapshot": bool(image)})


SETUP = {
    "ui": ("PB_UI", ("advanced",)),
    "ai": ("PB_AI", ("off", "basic", "heavy")),
    "video": ("PB_VIDEO", ("snapshots", "live", "timelapse")),
    "notify": ("PB_NOTIFY", ("basic", "advanced")),
    "remote": ("PB_REMOTE", ("off", "on")),
}
BUILT = {"ui.advanced", "ai.off", "ai.basic", "ai.heavy", "video.snapshots", "notify.basic", "notify.advanced", "video.live", "video.timelapse", "remote.off", "remote.on"}


def setup() -> dict:
    out = {}
    for key, (env, choices) in SETUP.items():
        value = os.getenv(env, choices[0]).strip().lower()
        out[key] = value if value in choices else choices[0]
    return out


STARTED = str(time.time())


async def get_config(request: Request):
    chosen = setup()
    return JSONResponse({"started": STARTED, "setup": chosen, "not_built": [k for k, v in chosen.items() if f"{k}.{v}" not in BUILT],
                         "version": VERSION_INFO})


async def health(request: Request):
    return JSONResponse({"ok": True, "printers": len(manager.clients)})


@contextlib.asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(manager.ai.load)
    await manager.start()
    manager.collect.start()
    bambu.start()
    yield
    await bambu.stop()
    await manager.collect.stop()
    await manager.ai.stop()
    await manager.stop()


class TunnelGuard:
    """Requests that come in through the Cloudflare tunnel may only reach the /share/... pages.
    (The tunnel is also set to /share/ only; this is a second lock in case that's misconfigured.)"""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            names = {k.lower() for k, _ in scope.get("headers", [])}
            if (b"cf-ray" in names or b"cf-connecting-ip" in names) and not scope["path"].startswith("/share/"):
                await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
                return
        await self.inner(scope, receive, send)


_app = Starlette(routes=[
    Route("/", index),
    Route("/manifest.webmanifest", manifest),
    Route("/sw.js", service_worker),
    Route("/healthz", health),
    Route("/api/config", get_config),
    Route("/api/scan", scan, methods=["POST"]),
    Route("/api/probe", probe, methods=["POST"]),
    Route("/api/printers", list_printers),
    Route("/api/printers", add_printer, methods=["POST"]),
    Route("/api/printers/order", set_order, methods=["POST"]),
    Route("/api/printers/{pid:int}", update_printer, methods=["PATCH"]),
    Route("/api/printers/{pid:int}", delete_printer, methods=["DELETE"]),
    Route("/api/printers/{pid:int}/snapshot", snapshot),
    Route("/api/printers/{pid:int}/stream", live_stream),
    Route("/api/printers/{pid:int}/shares", list_shares),
    Route("/api/printers/{pid:int}/shares", create_share, methods=["POST"]),
    Route("/api/shares/{code}", revoke_share, methods=["DELETE"]),
    Route("/api/share/user-id", share_user),
    # public share pages: the only paths the tunnel will let in from outside
    Route("/share/printer/{pid:int}/{code}", share_page),
    Route("/share/printer/{pid:int}/{code}/status", share_status),
    Route("/share/printer/{pid:int}/{code}/snapshot", share_snapshot),
    Route("/share/printer/{pid:int}/{code}/stream", share_stream),
    Route("/api/timelapse/settings", timelapse_settings, methods=["GET", "PUT"]),
    Route("/api/ai/settings", ai_settings, methods=["GET", "PUT"]),
    Route("/api/ui/layout", ui_layout, methods=["GET", "PUT"]),
    Route("/api/ui/theme", ui_theme, methods=["GET", "DELETE"]),
    Route("/api/ui/theme/{name}", ui_theme_file),
    Route("/api/printers/{pid:int}/theme", printer_theme, methods=["GET", "PUT"]),    Route("/api/news", news),
    Route("/api/ai/reload", ai_reload, methods=["POST"]),
    Route("/api/components", components_list),
    Route("/api/components", components_action, methods=["POST"]),
    Route("/api/system", system_list),
    Route("/api/system", system_action, methods=["POST"]),
    Route("/api/printers/{pid:int}/ai/check", ai_check, methods=["POST"]),
    Route("/api/printers/{pid:int}/ai/history", ai_history),
    Route("/api/printers/{pid:int}/ai/save", ai_save, methods=["POST"]),
    Route("/api/printers/{pid:int}/ai/overlay", ai_overlay, methods=["POST"]),
    Route("/api/printers/{pid:int}/ai/area", ai_area, methods=["POST"]),
    Route("/api/ai/photos", ai_photos),
    Route("/api/ai/photos/{iid}.jpg", ai_photo_image),
    Route("/api/ai/photos/{iid}", ai_photo_label, methods=["POST"]),
    Route("/api/ai/photos/{iid}", ai_photo_delete, methods=["DELETE"]),
    Route("/api/printers/{pid:int}/timelapses", list_timelapses),
    Route("/api/printers/{pid:int}/timelapses/{tid}.mp4", timelapse_video),
    Route("/api/printers/{pid:int}/timelapses/{tid}", delete_timelapse, methods=["DELETE"]),
    Route("/api/printers/{pid:int}/gcode", printer_gcode, methods=["POST"]),
    Route("/api/printers/{pid:int}/temps", temps),
    Route("/api/printers/{pid:int}/thumbnail", thumbnail),
    Route("/api/printers/{pid:int}/macros", macros),
    Route("/api/printers/{pid:int}/files", list_files),
    Route("/api/printers/{pid:int}/history", history),
    Route("/api/printers/{pid:int}/history/totals", history_totals),
    Route("/api/printers/{pid:int}/files", delete_file, methods=["DELETE"]),
    Route("/api/printers/{pid:int}/files/print", print_file, methods=["POST"]),
    Route("/api/printers/{pid:int}/queue", job_queue, methods=["GET", "POST"]),
    Route("/api/printers/{pid:int}/rpc", rpc, methods=["POST"]),
    Route("/api/printers/{pid:int}/kv", printer_kv, methods=["GET", "PUT"]),
    Route("/api/notices", notices),
    Route("/api/ui/eta", ui_eta, methods=["GET", "PUT"]),
    Route("/api/printers/{pid:int}/file", printer_file, methods=["GET", "PUT"]),
    Route("/api/printers/{pid:int}/files/upload", upload_file, methods=["POST"]),
    Route("/api/printers/{pid:int}/{action}", printer_action, methods=["POST"]),
    Route("/api/events", events),
    Route("/api/notify/settings", get_notify_settings),
    Route("/api/notify/settings", save_notify_settings, methods=["PUT"]),
    Route("/api/notify/test", test_notification, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
], lifespan=lifespan)
app = TunnelGuard(_app)
