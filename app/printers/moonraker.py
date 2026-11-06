from __future__ import annotations

import asyncio
import re
import itertools
import json
import logging
import time
from typing import Callable

import uuid

from ..core.net import HandshakeError, WebSocket, get_json, http_get, http_post, http_stream

log = logging.getLogger(__name__)
VERSION = "0.1.0"
ETA_MODE = "file"

SUBSCRIBE = {
    "print_stats": None,
    "extruder": ["temperature", "target", "power", "pressure_advance", "smooth_time", "can_extrude"],
    "heater_bed": ["temperature", "target", "power"],
    "display_status": ["progress", "message"],
    "virtual_sdcard": ["progress", "is_active", "file_position", "file_size"],
    "webhooks": ["state", "state_message"],
    "configfile": ["warnings", "save_config_pending", "save_config_pending_items"],
    "gcode_move": ["speed_factor", "extrude_factor", "homing_origin"],
    "toolhead": ["homed_axes", "position", "axis_minimum", "axis_maximum", "extruder",
                 "max_velocity", "max_accel", "square_corner_velocity", "minimum_cruise_ratio", "max_accel_to_decel"],
}

SENSOR_PREFIXES = ("temperature_sensor ", "temperature_fan ", "heater_generic ")
FAN_PREFIXES = ("heater_fan ", "controller_fan ", "fan_generic ")
EXTRA_HEATER = re.compile(r"extruder\d+")
OUTPUT_PREFIXES = ("output_pin ", "led ", "neopixel ", "dotstar ", "pca9533 ", "pca9632 ")
FSENSOR_PREFIXES = ("filament_switch_sensor ", "filament_motion_sensor ")
MMU_FIELDS = ["enabled", "num_gates", "gate", "tool", "unit", "filament", "action", "has_bypass", "is_in_print",
              "gate_status", "gate_material", "gate_color", "gate_filament_name", "gate_temperature", "gate_remaining"]
FEATURES = ("quad_gantry_level", "z_tilt", "bed_mesh", "exclude_object", "firmware_retraction", "probe", "bltouch",
            "safe_z_home", "screws_tilt_adjust")


HELPER_MACROS = {"load": ("LOAD_FILAMENT", "FILAMENT_LOAD", "M701"), "unload": ("UNLOAD_FILAMENT", "FILAMENT_UNLOAD", "M702"),
                 "clean": ("CLEAN_NOZZLE", "NOZZLE_CLEAN", "WIPE_NOZZLE", "NOZZLE_WIPE", "G12"),
                 "park": ("PARK_TOOLHEAD", "TOOLHEAD_PARK", "G27")}


_PARAM_RE = re.compile(r"""params\s*(?:\.\s*([A-Za-z_]\w*)|\[\s*['"]([A-Za-z_]\w*)['"]\s*\])(?:\s*\|\s*default\s*\(\s*([^,)]*))?""")


def _macro_params(gcode: str) -> list[dict]:
    out: dict[str, str | None] = {}
    for m in _PARAM_RE.finditer(gcode):
        name = (m.group(1) or m.group(2)).upper()
        if name in ("RAWPARAMS",):
            continue
        d = (m.group(3) or "").strip().strip("'\"") or None
        if name not in out or (out[name] is None and d is not None):
            out[name] = d
    return [{"name": k, "default": v} for k, v in list(out.items())[:20]]


def _pretty(obj: str) -> str:
    named = {"extruder": "Nozzle", "heater_bed": "Bed", "fan": "Part fan"}
    if obj in named:
        return named[obj]
    if obj.startswith("extruder"):
        return f"Nozzle {obj[8:]}"
    name = obj.split(" ", 1)[1] if " " in obj else obj
    return name.replace("_", " ").title()


class AuthError(Exception):
    pass


class RPCError(Exception):
    pass


def _merge(dst: dict, src: dict):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


class MoonrakerClient:
    def __init__(self, printer_id: int, host: str, port: int, api_key: str | None,
                 on_change: Callable[["MoonrakerClient"], None]):
        self.id, self.host, self.port, self.api_key = printer_id, host, port, api_key
        self._on_change = on_change
        self.on_gcode: Callable[["MoonrakerClient", str], None] | None = None
        self.connection = "connecting"
        self.error: str | None = None
        self.klippy_state: str | None = None
        self.klippy_message: str | None = None
        self.moonraker_warnings: list[str] = []
        self.failed_components: list[str] = []
        self.status: dict = {}
        self.webcams: list[dict] = []
        self.extras: list[str] = []
        self.macros: list[dict] = []
        self.pwm_pins: set[str] = set()
        self.native_camera = None
        self._thumbs: dict[str, tuple[str, bytes] | None] = {}
        self._qmeta: dict[str, tuple[float, dict]] = {}
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._ws: WebSocket | None = None
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run(), name=f"moonraker-{self.id}")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self):
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                await self._session()
                self._set(connection="offline", error="connection closed")
            except asyncio.CancelledError:
                raise
            except AuthError as e:
                self._set(connection="auth_error", error=str(e))
                backoff = 60.0
            except Exception as e:
                self._set(connection="offline", error=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
            if time.monotonic() - started > 60:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _session(self):
        url = f"ws://{self.host}:{self.port}/websocket"
        if self.api_key:
            status, data = await get_json(f"http://{self.host}:{self.port}/access/oneshot_token",
                                          {"X-Api-Key": self.api_key})
            if status in (401, 403):
                raise AuthError("API key rejected")
            if status == 200 and isinstance(data, dict):
                url += "?token=" + str(data.get("result", ""))
        try:
            ws = await WebSocket.connect(url)
        except HandshakeError as e:
            if e.status in (401, 403):
                raise AuthError("Moonraker requires an API key") from e
            raise
        self._ws = ws
        reader = asyncio.create_task(self._reader(ws))
        pinger = asyncio.create_task(self._pinger(ws))
        try:
            try:
                await self._call("server.connection.identify", {
                    "client_name": "printer-monitor", "version": VERSION, "type": "other",
                    "url": "https://github.com/",
                })
            except RPCError:
                pass
            info = await self._call("server.info")
            self._set(connection="online", error=None, klippy_state=info.get("klippy_state"),
                      moonraker_warnings=[str(w) for w in info.get("warnings") or []],
                      failed_components=[str(c) for c in info.get("failed_components") or []])
            await self._load_webcams()
            if info.get("klippy_state") == "ready":
                await self._subscribe()
            else:
                await self._load_klippy_message()
            await reader
        finally:
            reader.cancel()
            pinger.cancel()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("disconnected"))
            self._pending.clear()
            await ws.close()
            self._ws = None

    async def _pinger(self, ws: WebSocket):
        while not ws.closed:
            await asyncio.sleep(20)
            if time.monotonic() - ws.last_rx > 65:
                log.info("printer %s: no data for 65s, reconnecting", self.id)
                ws.abort()
                return
            try:
                await ws.ping()
            except Exception:
                ws.abort()
                return

    async def _call(self, method: str, params: dict | None = None, timeout: float = 10.0):
        if not self._ws:
            raise ConnectionError("not connected")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            msg["params"] = params
        try:
            await self._ws.send_text(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def _reader(self, ws: WebSocket):
        while (raw := await ws.recv()) is not None:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending[rid]
                if fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(RPCError(err.get("message", str(err)) if isinstance(err, dict) else str(err)))
                else:
                    fut.set_result(msg.get("result"))
                continue
            self._notification(msg.get("method"), msg.get("params") or [])
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("disconnected"))

    def _notification(self, method: str | None, params: list):
        if method == "notify_status_update" and params and isinstance(params[0], dict):
            _merge(self.status, params[0])
            self._changed()
        elif method == "notify_klippy_ready":
            self._set(klippy_state="ready", klippy_message=None)
            asyncio.create_task(self._safe(self._subscribe()))
            asyncio.create_task(self._safe(self._load_server_notes()))
        elif method == "notify_klippy_shutdown":
            self._set(klippy_state="shutdown")
            asyncio.create_task(self._safe(self._load_klippy_message()))
        elif method == "notify_klippy_disconnected":
            self._set(klippy_state="disconnected", klippy_message=None)
            asyncio.create_task(self._safe(self._load_server_notes()))
        elif method == "notify_gcode_response" and params:
            if self.on_gcode:
                try:
                    self.on_gcode(self, str(params[0]))
                except Exception:
                    log.exception("on_gcode handler failed")
        elif method == "notify_webcams_changed":
            asyncio.create_task(self._safe(self._load_webcams()))

    async def _safe(self, coro):
        try:
            await coro
        except Exception as e:
            log.warning("printer %s: %s", self.id, e)

    async def _load_klippy_message(self):
        try:
            res = await self._call("printer.info")
            self._set(klippy_state=res.get("state", self.klippy_state), klippy_message=res.get("state_message"))
        except RPCError:
            pass   # Klipper not connected: printer.info only works once it is

    async def _load_server_notes(self):
        info = await self._call("server.info")
        self._set(moonraker_warnings=[str(w) for w in info.get("warnings") or []],
                  failed_components=[str(c) for c in info.get("failed_components") or []])

    async def _subscribe(self):
        objects = dict(SUBSCRIBE)
        try:
            names = (await self._call("printer.objects.list") or {}).get("objects", [])
        except (RPCError, asyncio.TimeoutError):
            names = []
        self.extras = [n for n in names
                       if n.startswith(SENSOR_PREFIXES + FAN_PREFIXES) or n == "fan" or EXTRA_HEATER.fullmatch(n)]
        for n in self.extras:
            objects[n] = None
        if "exclude_object" in names:
            objects["exclude_object"] = None
        self.outputs = [n for n in names if n.startswith(OUTPUT_PREFIXES)]
        self.fsensors = [n for n in names if n.startswith(FSENSOR_PREFIXES)]
        self.features = {f: f in names for f in FEATURES}
        self.extruders = sorted(n for n in names if n == "extruder" or (n.startswith("extruder") and n[8:].isdigit()))
        for n in self.outputs + self.fsensors:
            objects[n] = None
        if "mmu" in names:
            objects["mmu"] = MMU_FIELDS
            objects["mmu_machine"] = None
        for n in ("manual_probe", "bed_screws", "screws_tilt_adjust"):
            if n in names:
                objects[n] = None
        self.led_effects = [n for n in names if n.startswith("led_effect ")]
        for n in self.led_effects:
            objects[n] = ["enabled"]
        self.pause_layer = "gcode_macro SET_PAUSE_AT_LAYER" in names or "gcode_macro SET_PAUSE_NEXT_LAYER" in names
        self.tool_macros = sorted((n for n in names if re.fullmatch(r"gcode_macro [Tt]\d+", n)), key=lambda n: int(n.split(" ", 1)[1][1:]))
        for n in self.tool_macros:
            objects[n] = None
        self.tc_tools = sorted(n for n in names if n.startswith("tool "))
        if "toolchanger" in names:
            objects["toolchanger"] = None
        for n in self.tc_tools:
            objects[n] = None
        if "gcode_macro SET_PRINT_STATS_INFO" in names:
            objects["gcode_macro SET_PRINT_STATS_INFO"] = ["pause_next_layer", "pause_at_layer"]
        for n in ("bed_mesh", "firmware_retraction", "quad_gantry_level", "z_tilt"):
            if n in names:
                objects[n] = ["profile_name", "profiles"] if n == "bed_mesh" else None
        await self._load_macros([n[len("gcode_macro "):] for n in names if n.startswith("gcode_macro ")])
        res = await self._call("printer.objects.subscribe", {"objects": objects})
        self.status = (res or {}).get("status", {})
        self._changed()

    async def _load_macros(self, names: list[str]):
        desc = {}
        try:
            res = await self._call("printer.objects.query", {"objects": {"configfile": ["settings"]}})
            settings = ((res or {}).get("status", {}).get("configfile", {}) or {}).get("settings", {}) or {}
            self.config_sections = set(settings)
            self.z_endstop = (settings.get("stepper_z") or {}).get("position_endstop") is not None
            desc = {k[len("gcode_macro "):]: (v or {}).get("description") for k, v in settings.items()
                    if k.startswith("gcode_macro ")}
            params = {k[len("gcode_macro "):]: _macro_params((v or {}).get("gcode") or "") for k, v in settings.items()
                      if k.startswith("gcode_macro ")}
            # a pin without its own config section (a module's virtual switch) is on/off
            self.pwm_pins = {k for k, v in settings.items() if k.startswith("output_pin ") and (v or {}).get("pwm")}
        except (RPCError, asyncio.TimeoutError):
            params = {}
        self.macros = sorted(({"name": n.upper(), "description": desc.get(n.lower()) or desc.get(n) or None,
                               "params": params.get(n.lower()) or params.get(n) or []}
                              for n in names if not n.startswith("_")), key=lambda m: m["name"])

    async def _load_meta(self, fname: str):
        try:
            m = await self._call("server.files.metadata", {"filename": fname}) or {}
            if self._meta_for == fname:
                self._meta = {k: m.get(k) for k in ("estimated_time", "filament_total")}
        except Exception:
            pass

    async def _load_webcams(self):
        try:
            res = await self._call("server.webcams.list")
            self.webcams = [c for c in (res or {}).get("webcams", []) if c.get("enabled", True)]
        except (RPCError, asyncio.TimeoutError):
            self.webcams = []
        self._changed()

    async def command(self, action: str):
        methods = {"pause": "printer.print.pause", "resume": "printer.print.resume",
                   "cancel": "printer.print.cancel", "estop": "printer.emergency_stop"}
        return await self._call(methods[action], timeout=15)

    async def list_files(self) -> list[dict]:
        res = await self._call("server.files.list", {"root": "gcodes"})
        files = [{"path": f.get("path") or f.get("filename"), "size": f.get("size"), "modified": f.get("modified")}
                 for f in (res or []) if isinstance(f, dict)]
        return sorted(files, key=lambda f: f["modified"] or 0, reverse=True)

    async def start_print(self, path: str):
        return await self._call("printer.print.start", {"filename": path}, timeout=30)

    QUEUE = {"status": "server.job_queue.status", "add": "server.job_queue.post_job", "remove": "server.job_queue.delete_job",
             "start": "server.job_queue.start", "pause": "server.job_queue.pause", "jump": "server.job_queue.jump"}

    async def job_queue(self, action: str, params: dict | None = None) -> dict:
        res = await self._call(self.QUEUE[action], params or None, timeout=15) or {}
        if action != "status" and "queued_jobs" not in res:
            res = await self._call(self.QUEUE["status"]) or {}
        jobs = [{"id": j.get("job_id"), "filename": j.get("filename"), "added": j.get("time_added")}
                for j in res.get("queued_jobs") or []]
        names = sorted({j["filename"] for j in jobs if j["filename"]})
        metas = dict(zip(names, await asyncio.gather(*(self.queue_meta(n) for n in names))))
        for j in jobs:
            j.update(metas.get(j["filename"]) or {"time": None, "filament": None, "weight": None})
        return {"jobs": jobs, "state": res.get("queue_state")}

    async def queue_meta(self, filename: str) -> dict:
        hit = self._qmeta.get(filename)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        try:
            m = await self._call("server.files.metadata", {"filename": filename}) or {}
        except RPCError:
            m = {}
        out = {"time": m.get("estimated_time"), "filament": m.get("filament_total"), "weight": m.get("filament_weight_total")}
        self._qmeta[filename] = (time.time(), out)
        return out

    async def delete_file(self, path: str):
        return await self._call("server.files.delete_file", {"path": f"gcodes/{path}"})

    async def upload(self, filename: str, data: bytes, start: bool = False, root: str = "gcodes", folder: str = ""):
        boundary = uuid.uuid4().hex
        parts = [("root", root)] + ([("path", folder)] if folder else []) + ([("print", "true")] if start else [])
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in parts)
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n').encode() + data + f"\r\n--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        status, _, resp = await http_post(f"http://{self.host}:{self.port}/server/files/upload", body, headers)
        if status not in (200, 201):
            raise RuntimeError(f"upload failed (HTTP {status}): {resp[:200].decode(errors='replace')}")

    async def history(self, start: int = 0, limit: int = 30) -> dict:
        res = await self._call("server.history.list", {"start": start, "limit": limit, "order": "desc"})
        jobs = [{"id": j.get("job_id"), "filename": j.get("filename"), "status": j.get("status"),
                 "start_time": j.get("start_time"), "end_time": j.get("end_time"),
                 "print_duration": j.get("print_duration"), "filament_used": j.get("filament_used"),
                 "exists": j.get("exists", False)}
                for j in (res or {}).get("jobs", [])]
        return {"count": (res or {}).get("count", len(jobs)), "jobs": jobs}

    async def history_totals(self) -> dict:
        res = await self._call("server.history.totals")
        return (res or {}).get("job_totals", {})

    async def temperature_history(self) -> dict:
        res = await self._call("server.temperature_store", {"include_monitors": False}, timeout=15) or {}
        keep = {"extruder", "heater_bed", *self.extras}
        return {k: {f: v[f] for f in ("temperatures", "targets", "powers", "speeds") if f in v}
                for k, v in res.items() if k in keep and isinstance(v, dict)}

    async def thumbnail(self, filename: str) -> tuple[str, bytes] | None:
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
        folder = filename.rsplit("/", 1)[0] + "/" if "/" in filename else ""
        from urllib.parse import quote
        url = f"http://{self.host}:{self.port}/server/files/gcodes/{quote(folder + pick['relative_path'])}"
        status, headers, data = await http_get(url, {"X-Api-Key": self.api_key} if self.api_key else {}, timeout=10,
                                               max_bytes=2_000_000)
        if status != 200 or not data:
            return None
        ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "image/png")
        if len(self._thumbs) > 100:
            self._thumbs.clear()
        self._thumbs[filename] = (ctype, data)
        return self._thumbs[filename]

    async def gcode(self, script: str):
        return await self._call("printer.gcode.script", {"script": script}, timeout=120)

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self._changed()

    def _changed(self):
        try:
            self._on_change(self)
        except Exception:
            log.exception("on_change handler failed")

    def _snap_cams(self) -> list[dict]:
        return [cam for cam in self.webcams if cam.get("snapshot_url")]

    def snapshot_url(self, index: int = 0) -> str | None:
        cams = self._snap_cams()
        if not cams:
            return None
        url = cams[min(max(index, 0), len(cams) - 1)]["snapshot_url"]
        if url.startswith(("http://", "https://")):
            return url
        # relative URLs are served by the web frontend (Mainsail/Fluidd nginx), not Moonraker's port
        return f"http://{self.host}{url if url.startswith('/') else '/' + url}"

    def _abs(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        return f"http://{self.host}{url if url.startswith('/') else '/' + url}"

    @staticmethod
    def _is_mjpeg(cam: dict) -> bool:
        service = (cam.get("service") or "").lower()
        url = (cam.get("stream_url") or "").lower()
        if not url or any(x in service for x in ("webrtc", "hls", "iframe")):
            return False
        return service.startswith(("mjpegstreamer", "uv4l-mjpeg", "ipstream")) or "action=stream" in url or service == ""

    def stream_url(self, index: int = 0) -> str | None:
        cams = self._snap_cams()
        if not cams:
            return None
        cam = cams[min(max(index, 0), len(cams) - 1)]
        return self._abs(cam["stream_url"]) if self._is_mjpeg(cam) else None

    def cams_info(self) -> list[dict]:
        out = []
        for i, cam in enumerate(self._snap_cams()):
            try:
                rotation = int(cam.get("rotation") or 0)
            except (TypeError, ValueError):
                rotation = 0
            out.append({"name": cam.get("name") or f"Camera {i + 1}", "flip_h": bool(cam.get("flip_horizontal")),
                        "flip_v": bool(cam.get("flip_vertical")), "rotation": rotation, "target_fps": cam.get("target_fps"),
                        "live": self._is_mjpeg(cam)})
        return out

    def view(self) -> dict:
        s = self.status
        ps = s.get("print_stats", {})
        ds = s.get("display_status", {})
        vs = s.get("virtual_sdcard", {})
        live = self.connection == "online" and self.klippy_state == "ready"
        state = ps.get("state") if live else None
        progress = ds.get("progress") or vs.get("progress") or 0.0
        duration = ps.get("print_duration") or 0.0
        eta = None
        fname = ps.get("filename") or None
        if fname and getattr(self, "_meta_for", None) != fname:
            self._meta_for, self._meta = fname, {}
            asyncio.ensure_future(self._load_meta(fname))
        meta = getattr(self, "_meta", {}) or {}
        if state == "printing" and duration > 0:
            est, ftot, fused = meta.get("estimated_time"), meta.get("filament_total"), ps.get("filament_used") or 0
            if ETA_MODE == "slicer" and est:
                eta = max(0.0, est - duration)
            elif ETA_MODE == "filament" and ftot and fused > 0:
                eta = duration / min(1.0, fused / ftot) - duration
            elif progress > 0.01:
                eta = duration / progress - duration
        return {
            "connection": self.connection,
            "error": self.error,
            "klippy_state": self.klippy_state,
            "klippy_message": self.klippy_message if self.klippy_state != "ready" else None,
            "klipper_warnings": [str(w.get("message") or w) for w in s.get("configfile", {}).get("warnings") or []] if live else [],
            "moonraker_warnings": self.moonraker_warnings,
            "save_pending": bool(live and s.get("configfile", {}).get("save_config_pending")),
            "save_pending_items": (s.get("configfile", {}).get("save_config_pending_items") or {}) if live else {},
            "failed_components": self.failed_components,
            "state": state,
            "filename": ps.get("filename") or None,
            "message": ps.get("message") or ds.get("message") or None,
            "progress": progress,
            "print_duration": duration,
            "eta": eta,
            "nozzle": s.get("extruder", {}).get("temperature"),
            "nozzle_target": s.get("extruder", {}).get("target"),
            "bed": s.get("heater_bed", {}).get("temperature"),
            "bed_target": s.get("heater_bed", {}).get("target"),
            "has_webcam": self.snapshot_url() is not None,
            "layer": (ps.get("info") or {}).get("current_layer"),
            "total_layers": (ps.get("info") or {}).get("total_layer"),
            "cams": self.cams_info(),
            "speed_factor": s.get("gcode_move", {}).get("speed_factor"),
            "extrude_factor": s.get("gcode_move", {}).get("extrude_factor"),
            "homed_axes": s.get("toolhead", {}).get("homed_axes"),
            "position": (s.get("toolhead", {}).get("position") or [None] * 4)[:4],
            "sensors": self.sensors() if live else [],
            "macro_count": len(self.macros),
            "objects": self.print_objects() if live else None,
            "file_position": vs.get("file_position"),
            "z_offset": ((s.get("gcode_move", {}).get("homing_origin") or [0, 0, 0])[2:3] or [None])[0],
            "pa": {k: s.get("extruder", {}).get(k) for k in ("pressure_advance", "smooth_time")},
            "can_extrude": s.get("extruder", {}).get("can_extrude"),
            "active_extruder": s.get("toolhead", {}).get("extruder"),
            "extruders": getattr(self, "extruders", []),
            "limits": {k: s.get("toolhead", {}).get(k) for k in
                       ("max_velocity", "max_accel", "square_corner_velocity", "minimum_cruise_ratio", "max_accel_to_decel")},
            "outputs": self.output_view() if live else [],
            "fsensors": [{"id": n, "name": _pretty(n), "enabled": s.get(n, {}).get("enabled"),
                          "detected": s.get(n, {}).get("filament_detected")} for n in getattr(self, "fsensors", [])] if live else [],
            "features": getattr(self, "features", {}),
            "manual_probe": {"active": bool(s.get("manual_probe", {}).get("is_active")), "z": s.get("manual_probe", {}).get("z_position"),
                             "lo": s.get("manual_probe", {}).get("z_position_lower"), "hi": s.get("manual_probe", {}).get("z_position_upper")} if live else None,
            "bed_screws": {"active": bool(s.get("bed_screws", {}).get("is_active")), "state": s.get("bed_screws", {}).get("state"),
                           "screw": s.get("bed_screws", {}).get("current_screw"), "accepted": s.get("bed_screws", {}).get("accepted_screws")} if live else None,
            "led_effects": [{"name": n[len("led_effect "):], "enabled": bool(s.get(n, {}).get("enabled"))}
                            for n in getattr(self, "led_effects", [])] if live else [],
            "pause_layer": {"next": s.get("gcode_macro SET_PRINT_STATS_INFO", {}).get("pause_next_layer") or {},
                            "at": s.get("gcode_macro SET_PRINT_STATS_INFO", {}).get("pause_at_layer") or {}}
                           if live and getattr(self, "pause_layer", False) else None,
            "screws_tilt": {"error": s.get("screws_tilt_adjust", {}).get("error"),
                            "results": s.get("screws_tilt_adjust", {}).get("results") or {}} if live and "screws_tilt_adjust" in s else None,
            "mesh": {"profile": s.get("bed_mesh", {}).get("profile_name"),
                     "profiles": sorted((s.get("bed_mesh", {}).get("profiles") or {}).keys())} if "bed_mesh" in s else None,
            "retraction": s.get("firmware_retraction"),
            "tools": self.tools_view() if live else None,
            "tool_changes": self.tool_changes_view() if live else None,
            "toolchanger": self.toolchanger_view() if live else None,
            "leveled": {k: s.get(k, {}).get("applied") for k in ("quad_gantry_level", "z_tilt") if k in s},
            "bed_area": self.bed_area(),
            "mmu": self.mmu_view() if live else None,
        }

    def mmu_view(self) -> dict | None:
        m = self.status.get("mmu")
        if not m:
            return None
        mm = self.status.get("mmu_machine") or {}

        def at(key, i):
            v = m.get(key) or []
            return v[i] if i < len(v) else None

        def color(c):
            c = str(c or "").strip().lstrip("#")
            if re.fullmatch(r"[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", c):
                return "#" + c[:6].lower()
            return c or None

        count = len(m.get("gate_status") or []) or int(m.get("num_gates") or 0)
        gates = [{"status": at("gate_status", i), "material": at("gate_material", i) or "", "color": color(at("gate_color", i)),
                  "name": at("gate_filament_name", i) or "", "temp": at("gate_temperature", i),
                  "remaining": r if isinstance(r := at("gate_remaining", i), (int, float)) and r >= 0 else None}
                 for i in range(count)]
        units = []
        for u in range(int(mm.get("num_units") or 1)):
            info = mm.get(f"unit_{u}") or {}
            env = self.status.get(info.get("environment_sensor") or "", {})
            units.append({"name": info.get("name") or f"Unit {u + 1}", "first": int(info.get("first_gate") or 0),
                          "count": int(info.get("num_gates") or count), "temp": env.get("temperature"),
                          "humidity": env.get("humidity")})
        return {"enabled": m.get("enabled", True), "gate": m.get("gate"), "tool": m.get("tool"),
                "loaded": m.get("filament") == "Loaded", "action": m.get("action"), "bypass": bool(m.get("has_bypass")),
                "units": units, "gates": gates}

    def output_view(self) -> list[dict]:
        out = []
        for n in getattr(self, "outputs", []):
            st = self.status.get(n, {})
            if n.startswith("output_pin "):
                out.append({"id": n, "name": _pretty(n), "kind": "pin", "value": st.get("value"),
                            "onoff": n.lower() not in self.pwm_pins})
            else:
                c = (st.get("color_data") or [[0, 0, 0, 0]])[0]
                out.append({"id": n, "name": _pretty(n), "kind": "led", "color": list(c)})
        return out

    def toolchanger_view(self) -> dict | None:
        tc = self.status.get("toolchanger")
        if not isinstance(tc, dict):
            return None
        tools = []
        for n in getattr(self, "tc_tools", []):
            t = self.status.get(n) or {}
            heater = t.get("heater") or t.get("extruder")
            h = self.status.get(heater) or {}
            tools.append({"name": t.get("name") or n.split(" ", 1)[1], "number": t.get("tool_number", -1), "active": bool(t.get("active")),
                          "detect": t.get("detect_state"), "heater": heater, "temp": h.get("temperature"), "target": h.get("target")})
        tools.sort(key=lambda t: (not isinstance(t["number"], int) or t["number"] < 0, t["number"] if isinstance(t["number"], int) else 0, t["name"]))
        return {"status": tc.get("status"), "tool": tc.get("tool"), "number": tc.get("tool_number", -1),
                "detected": tc.get("detected_tool"), "tools": tools}

    def tool_heater_labels(self) -> dict[str, str]:
        out = {}
        for n in getattr(self, "tc_tools", []):
            t = self.status.get(n) or {}
            heater = t.get("heater") or t.get("extruder")
            if heater:
                num = t.get("tool_number", -1)
                out[heater] = f"T{num}" if isinstance(num, int) and num >= 0 else (t.get("name") or n.split(" ", 1)[1])
        return out

    def tool_changes_view(self) -> list[dict] | None:
        desc = {m["name"]: m["description"] for m in self.macros}
        out = []
        for n in getattr(self, "tool_macros", []):
            name = n.split(" ", 1)[1].upper()
            v = self.status.get(n, {})
            color = str(v.get("color") or "").lstrip("#")
            out.append({"name": name, "description": desc.get(name) or f"Select tool {name[1:]}",
                        "color": f"#{color}" if re.fullmatch(r"[0-9a-fA-F]{6}", color) else None, "active": bool(v.get("active"))})
        return out or None

    def tools_view(self) -> dict | None:
        c = getattr(self, "config_sections", None)
        if not c:
            return None
        names = {m["name"] for m in self.macros}
        helpers = {k: next((n for n in opts if n in names), None) for k, opts in HELPER_MACROS.items()}
        probe = bool(c & {"probe", "bltouch", "smart_effector", "cartographer"}) or any(s.startswith("probe_eddy_current ") for s in c)
        calibrate = [name for name, ok in (
            ("AXIS_TWIST_COMPENSATION_CALIBRATE", "axis_twist_compensation" in c),
            ("BEACON_AUTO_CALIBRATE", "beacon" in c),
            ("BED_SCREWS_ADJUST", "bed_screws" in c),
            ("BED_TILT_CALIBRATE", "bed_tilt" in c),
            ("CARTOGRAPHER_SCAN_CALIBRATE", "cartographer" in c),
            ("CARTOGRAPHER_TOUCH_CALIBRATE", "cartographer" in c),
            ("CARTOGRAPHER_AXIS_TWIST_COMPENSATION", "cartographer" in c and "axis_twist_compensation" in c),
            ("DELTA_CALIBRATE", "delta_calibrate" in c),
            ("MANUAL_PROBE", True),
            ("PROBE_ACCURACY", probe),
            ("PROBE_CALIBRATE", probe),
            ("QUAD_GANTRY_LEVEL", "quad_gantry_level" in c),
            ("SCREWS_TILT_CALCULATE", "screws_tilt_adjust" in c),
            ("Z_ENDSTOP_CALIBRATE", getattr(self, "z_endstop", False)),
            ("Z_TILT_ADJUST", "z_tilt" in c or "z_tilt_ng" in c),
        ) if ok]
        return {"helpers": {k: v for k, v in helpers.items() if v}, "calibrate": calibrate}

    async def rpc(self, method: str, params: dict | None = None, timeout: float = 15.0):
        return await self._call(method, params, timeout=timeout)

    async def read_file(self, root: str, path: str):
        from urllib.parse import quote
        url = f"http://{self.host}:{self.port}/server/files/{root}/{quote(path)}"
        return await http_stream(url, {"X-Api-Key": self.api_key} if self.api_key else {}, timeout=15)

    def print_objects(self) -> dict | None:
        eo = self.status.get("exclude_object")
        if not isinstance(eo, dict) or not eo.get("objects"):
            return None
        return {"list": [{"name": o.get("name"), "center": o.get("center"), "polygon": o.get("polygon")}
                         for o in eo["objects"] if o.get("name")],
                "excluded": eo.get("excluded_objects") or [], "current": eo.get("current_object")}

    def bed_area(self) -> list | None:
        th = self.status.get("toolhead", {})
        lo, hi = th.get("axis_minimum"), th.get("axis_maximum")
        return [lo[0], lo[1], hi[0], hi[1]] if lo and hi and len(lo) > 1 and len(hi) > 1 else None

    def sensors(self) -> list[dict]:
        out = []
        label = self.tool_heater_labels()
        for obj in ["extruder", "heater_bed", *self.extras]:
            st = self.status.get(obj)
            if not isinstance(st, dict):
                continue
            is_fan = obj == "fan" or obj.startswith(FAN_PREFIXES)
            kind = ("fan" if is_fan else "heater" if "target" in st and not obj.startswith("temperature_fan ")
                    else "sensor")
            out.append({"id": obj, "name": label.get(obj) or _pretty(obj), "kind": kind,
                        "temp": st.get("temperature"), "target": st.get("target"),
                        "power": st.get("power"), "speed": st.get("speed")})
        return out
