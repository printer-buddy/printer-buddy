import asyncio
import contextlib
import io
import itertools
import json
import logging
import posixpath
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import deque
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .printer import FANS, Bambu

log = logging.getLogger("bambu.server")
VERSION = "v0.9.3-bambu-convert"
COMPONENTS = ["klippy_connection", "application", "websockets", "internal_transport", "dbus_manager", "database",
              "file_manager", "klippy_apis", "secrets", "template", "shell_command", "machine", "data_store",
              "proc_stats", "job_state", "history", "webcam", "announcements", "extensions"]
PRINT_EXT = (".gcode", ".g", ".gco", ".3mf")
LISTED_DIRS = ("", "cache")


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _png_size(data: bytes) -> tuple[int, int]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


class Client:
    def __init__(self, cid: int, ws: WebSocket | None, host: str):
        self.id, self.ws, self.host = cid, ws, host
        self.subs: dict = {}


class Bridge:
    def __init__(self, cfg: dict, data_dir: Path):
        self.p = Bambu(cfg)
        self.port = int(cfg["port"])
        self.dir = data_dir / self.p.serial
        (self.dir / "thumbs").mkdir(parents=True, exist_ok=True)
        self.db: dict = _load(self.dir / "database.json", {})
        self.history: dict = _load(self.dir / "history.json", {"next": 1, "jobs": []})
        self.meta: dict = _load(self.dir / "metadata.json", {})
        self.gcodes: deque = deque(maxlen=1000)
        self.temps: dict = {}
        self.clients: dict[int, Client] = {}
        self.last: dict = {}
        self._ids = itertools.count(1)
        self._dirty = asyncio.Event()
        self._was_connected = False
        self._listing: tuple[float, list] = (0.0, [])
        self._current_job: str | None = None
        self.p.listeners.append(self._dirty.set)
        self.p.gcode_listeners.append(self._response)
        self._tasks: list[asyncio.Task] = []

    def start(self):
        self._tasks = [asyncio.create_task(self.p.run()), asyncio.create_task(self._loop())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(BaseException):
                await t

    # -------------------------------------------------------------- push loop

    async def _loop(self):
        tick = 0.0
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._dirty.wait(), 1.0)
            self._dirty.clear()
            if self.p.connected != self._was_connected:
                self._was_connected = self.p.connected
                await self._notify("notify_klippy_ready" if self.p.connected else "notify_klippy_disconnected", [])
            self._track_history()
            status = self.p.objects()
            if time.monotonic() - tick >= 1.0:
                tick = time.monotonic()
                self._sample(status)
            diff = {}
            for name, fields in status.items():
                old = self.last.get(name, {})
                changed = {k: v for k, v in fields.items() if old.get(k) != v}
                if changed:
                    diff[name] = changed
            self.last = status
            if diff:
                for c in list(self.clients.values()):
                    part = self._filter(diff, c.subs)
                    if part:
                        await self._send(c, {"jsonrpc": "2.0", "method": "notify_status_update",
                                             "params": [part, time.monotonic()]})

    def _sample(self, status: dict):
        for name in status["heaters"]["available_sensors"]:
            obj = status.get(name) or {}
            t = self.temps.setdefault(name, {"temperatures": deque(maxlen=1200)} if name.startswith("temperature_sensor")
                                      else {"temperatures": deque(maxlen=1200), "targets": deque(maxlen=1200),
                                            "powers": deque(maxlen=1200)})
            t["temperatures"].append(round(obj.get("temperature", 0.0), 2))
            if "targets" in t:
                t["targets"].append(obj.get("target", 0.0))
                t["powers"].append(obj.get("power", 0.0))

    @staticmethod
    def _filter(status: dict, subs: dict) -> dict:
        out = {}
        for name, fields in subs.items():
            if name in status:
                part = status[name] if fields is None else {k: v for k, v in status[name].items() if k in fields}
                if part:
                    out[name] = part
        return out

    async def _send(self, c: Client, msg: dict):
        try:
            await c.ws.send_text(json.dumps(msg))
        except Exception:
            self.clients.pop(c.id, None)

    async def _notify(self, method: str, params: list):
        for c in list(self.clients.values()):
            if c.ws:
                await self._send(c, {"jsonrpc": "2.0", "method": method, "params": params})

    def _response(self, line: str):
        self.gcodes.append({"message": line, "time": time.time(), "type": "response"})
        asyncio.get_running_loop().create_task(self._notify("notify_gcode_response", [line]))

    # -------------------------------------------------------------- history

    def _track_history(self):
        s = self.p.state()
        jobs = self.history["jobs"]
        if s in ("printing", "paused") and not self._current_job:
            last = jobs[-1] if jobs else None
            if last and last["status"] == "in_progress" and last["filename"] == self.p.filename():
                self._current_job = last["job_id"]
            else:
                jid = f"{self.history['next']:06X}"
                self.history["next"] += 1
                job = {"job_id": jid, "exists": True, "end_time": None, "filament_used": 0.0,
                       "filename": self.p.filename(), "metadata": self.meta.get(self.p.filename(), {}).get("meta", {}),
                       "print_duration": 0.0, "status": "in_progress", "start_time": self.p.job_start() or time.time(),
                       "total_duration": 0.0, "user": None, "auxiliary_data": []}
                jobs.append(job)
                del jobs[:-1000]
                self._current_job = jid
                _save(self.dir / "history.json", self.history)
                asyncio.get_running_loop().create_task(self._notify("notify_history_changed", [{"action": "added", "job": job}]))
        ended = self.p.job_ended
        if ended and self._current_job:
            self.p.job_ended = None
            job = next((j for j in jobs if j["job_id"] == self._current_job), None)
            self._current_job = None
            if job:
                _, duration, state = ended
                job.update(end_time=time.time(), print_duration=duration, total_duration=time.time() - job["start_time"],
                           status={"complete": "completed", "cancelled": "cancelled"}.get(state, "error"))
                _save(self.dir / "history.json", self.history)
                asyncio.get_running_loop().create_task(self._notify("notify_history_changed", [{"action": "finished", "job": job}]))

    # -------------------------------------------------------------- files

    async def _ftp(self, fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as e:
            raise RPCError(500, f"printer storage: {e}") from e

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        path = (path or "").strip("/")
        root, _, rest = path.partition("/")
        if root != "gcodes":
            raise RPCError(400, f"root '{root}' is read-only on a Bambu printer" if root else "no root given")
        if ".." in rest.split("/"):
            raise RPCError(400, "bad path")
        return root, rest

    async def list_files(self) -> list[dict]:
        if time.monotonic() - self._listing[0] < 10:
            return self._listing[1]
        out = []
        for d in LISTED_DIRS:
            try:
                _, files = await self._ftp(self.p.files.listdir, "/" + d)
            except RPCError:
                if d:
                    continue
                raise
            for f in files:
                if f["name"].lower().endswith(PRINT_EXT):
                    out.append({"path": posixpath.join(d, f["name"]), "modified": f["modified"], "size": f["size"],
                                "permissions": "rw"})
        self._listing = (time.monotonic(), out)
        return out

    async def metadata(self, filename: str) -> dict:
        filename = filename.strip("/")
        files = {f["path"]: f for f in await self.list_files()}
        f = files.get(filename)
        if not f:
            raise RPCError(404, f"Metadata not available for <{filename}>")
        cached = self.meta.get(filename)
        if cached and cached.get("modified") == f["modified"] and cached.get("size") == f["size"]:
            return cached["meta"]
        meta = {"size": f["size"], "modified": f["modified"], "uuid": None, "slicer": "BambuStudio",
                "slicer_version": None, "print_start_time": None, "job_id": None, "thumbnails": [], "filename": filename}
        if filename.lower().endswith(".3mf"):
            try:
                data = await self._ftp(self.p.files.read, "/" + filename)
                meta.update(self._read_3mf(filename, data))
            except (RPCError, zipfile.BadZipFile, ET.ParseError, KeyError) as e:
                log.info("%s: no metadata for %s (%s)", self.p.name, filename, e)
        self.meta[filename] = {"modified": f["modified"], "size": f["size"], "meta": meta}
        _save(self.dir / "metadata.json", self.meta)
        return meta

    def _read_3mf(self, filename: str, data: bytes) -> dict:
        out: dict = {}
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            plates = sorted(int(m.group(1)) for n in names if (m := re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n)))
            plate_no = plates[0] if plates else 1
            out["bambu_plate"] = plate_no
            if "Metadata/slice_info.config" in names:
                root = ET.fromstring(z.read("Metadata/slice_info.config"))
                plate = next((pl for pl in root.findall("plate")
                              if any(m.get("key") == "index" and m.get("value") == str(plate_no)
                                     for m in pl.findall("metadata"))), root.find("plate"))
                if plate is not None:
                    md = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
                    if md.get("prediction"):
                        out["estimated_time"] = float(md["prediction"])
                    if md.get("weight"):
                        out["filament_weight_total"] = float(md["weight"])
                    used = [float(fl.get("used_m") or 0) for fl in plate.findall("filament")]
                    if used:
                        out["filament_total"] = sum(used) * 1000
                        out["bambu_filaments"] = [
                            {"type": fl.get("type") or "", "color": fl.get("color") or "",
                             "used": float(fl.get("used_g") or fl.get("used_m") or 0) > 0}
                            for fl in plate.findall("filament")]
                    types = [fl.get("type") for fl in plate.findall("filament") if fl.get("type")]
                    if types:
                        out["filament_type"] = ";".join(types)
            thumbs = []
            for src in (f"Metadata/plate_{plate_no}_small.png", f"Metadata/plate_{plate_no}.png"):
                if src in names:
                    png = z.read(src)
                    w, h = _png_size(png)
                    rel = f".thumbs/{re.sub(r'[^A-Za-z0-9._-]', '_', filename)}-{w}x{h}.png"
                    (self.dir / "thumbs" / rel[len(".thumbs/"):]).write_bytes(png)
                    thumbs.append({"width": w, "height": h, "size": len(png), "relative_path": rel})
            out["thumbnails"] = thumbs
        return out

    def _item(self, path: str, size: int = 0) -> dict:
        return {"path": path, "root": "gcodes", "size": size, "modified": time.time(), "permissions": "rw"}

    async def _files_changed(self, action: str, item: dict, source: dict | None = None):
        self._listing = (0.0, [])
        params = {"action": action, "item": item}
        if source:
            params["source_item"] = source
        await self._notify("notify_filelist_changed", [params])

    async def upload(self, root: str, folder: str, name: str, data: bytes, start: bool) -> dict:
        if root != "gcodes":
            raise RPCError(400, f"root '{root}' is read-only on a Bambu printer")
        rel = posixpath.join(folder.strip("/"), name) if folder.strip("/") else name
        await self._ftp(self.p.files.write, "/" + rel, data)
        item = self._item(rel, len(data))
        await self._files_changed("create_file", item)
        if start:
            await self.start_print(rel)
        return {"item": item, "print_started": start, "print_queued": False, "action": "create_file"}

    async def start_print(self, path: str):
        path = path.strip("/")
        meta = {}
        if path.lower().endswith(".3mf"):
            try:
                meta = await self.metadata(path)
            except RPCError as e:
                log.info("%s: starting %s without its metadata (%s)", self.p.name, path, e)
        filaments = meta.get("bambu_filaments")
        await self.p.start(path, int(meta.get("bambu_plate") or 1), filaments if isinstance(filaments, list) else None)

    async def download(self, rel: str) -> bytes:
        if rel.startswith(".thumbs/"):
            p = self.dir / "thumbs" / rel[len(".thumbs/"):]
            if p.is_file():
                return p.read_bytes()
            raise RPCError(404, "not found")
        return await self._ftp(self.p.files.read, "/" + rel)

    # -------------------------------------------------------------- gcode

    async def script(self, script: str):
        for raw in script.splitlines():
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            self.gcodes.append({"message": line, "time": time.time(), "type": "command"})
            words = line.split()
            cmd = words[0].upper()
            args = {}
            for w in words[1:]:
                k, _, v = w.partition("=")
                args[k.upper()] = v
            if cmd in ("PAUSE", "M25"):
                await self.p.pause()
            elif cmd in ("RESUME", "M24"):
                await self.p.resume()
            elif cmd in ("CANCEL_PRINT", "SDCARD_RESET_FILE"):
                await self.p.stop()
            elif cmd in ("FIRMWARE_RESTART", "RESTART", "STATUS", "SAVE_CONFIG"):
                pass
            elif cmd == "SET_PIN" and args.get("PIN", "").lower() == "chamber_light":
                await self.p.light(float(args.get("VALUE") or 0) > 0)
            elif cmd == "SET_FAN_SPEED":
                idx = next((n for name, (_, n) in FANS.items()
                            if name.split(" ")[-1].lower() == args.get("FAN", "").lower() and n), None)
                if idx is None:
                    raise RPCError(400, f"unknown fan {args.get('FAN')}")
                await self.p.gcode(f"M106 P{idx} S{round(float(args.get('SPEED') or 0) * 255)}")
            elif cmd == "SET_HEATER_TEMPERATURE":
                heater = args.get("HEATER", "").lower()
                target = float(args.get("TARGET") or 0)
                if heater == "heater_bed":
                    await self.p.gcode(f"M140 S{target:g}")
                elif re.fullmatch(r"extruder\d*", heater):
                    await self.p.nozzle_temp(int(heater[8:] or 0), target)
                else:
                    raise RPCError(400, f"unknown heater {heater}")
            elif cmd in ("MMU_CHANGE_TOOL", "MMU_LOAD") or re.fullmatch(r"T\d+", cmd):
                if args.get("BYPASS") == "1":
                    raise RPCError(400, "loading the external spool isn't supported on a Bambu printer")
                gate = cmd[1:] if cmd.startswith("T") and cmd[1:].isdigit() else args.get("TOOL") or args.get("GATE")
                try:
                    await self.p.ams_load(int(gate))
                except (TypeError, ValueError) as e:
                    raise RPCError(400, str(e) if gate is not None else f"{cmd} needs TOOL=") from e
            elif cmd in ("MMU_EJECT", "MMU_UNLOAD"):
                try:
                    await self.p.ams_unload()
                except ValueError as e:
                    raise RPCError(400, str(e)) from e
            elif cmd in ("MMU_SELECT", "MMU_CHECK_GATES", "MMU_STATS"):
                pass
            elif cmd == "M220":
                m = re.search(r"S(\d+)", line, re.I)
                await self.p.speed(float(m.group(1)) if m else 100)
            elif cmd in ("M106", "M107") and not re.search(r"\bP\d", line, re.I):
                await self.p.gcode(line.replace(words[0], f"{words[0]} P1", 1) if cmd == "M106" else "M106 P1 S0")
            else:
                await self.p.gcode(line)
        return "ok"

    # -------------------------------------------------------------- rpc

    def webcams(self, host: str) -> list[dict]:
        base = f"http://{host}"
        return [{"name": "Bambu camera", "location": "printer", "service": "mjpegstreamer-adaptive", "enabled": True,
                 "icon": "mdiWebcam", "target_fps": 15, "target_fps_idle": 5,
                 "stream_url": f"{base}/webcam/?action=stream", "snapshot_url": f"{base}/webcam/?action=snapshot",
                 "flip_horizontal": False, "flip_vertical": False, "rotation": 0, "aspect_ratio": "16:9",
                 "extra_data": {}, "source": "config", "uid": f"bambu-{self.p.serial}"}]

    async def call(self, method: str, params: dict, client: Client):
        p = self.p
        if method in ("printer.objects.query", "printer.objects.subscribe"):
            want = params.get("objects") or {}
            status = p.objects()
            objects = {k: (None if v in (None, [], "") else list(v)) for k, v in want.items()}
            if method == "printer.objects.subscribe":
                client.subs = objects
            return {"eventtime": time.monotonic(), "status": self._filter(status, objects)}
        if method == "printer.objects.list":
            return {"objects": list(p.objects())}
        if method == "server.info":
            return {"klippy_connected": p.connected, "klippy_state": "ready" if p.connected else "disconnected",
                    "components": COMPONENTS, "failed_components": [], "registered_directories": ["gcodes", "config"],
                    "warnings": [], "websocket_count": len(self.clients), "moonraker_version": VERSION,
                    "missing_klippy_requirements": [], "api_version": [1, 5, 0], "api_version_string": "1.5.0"}
        if method == "printer.info":
            return {"state": "ready" if p.connected else "startup",
                    "state_message": "Printer is ready" if p.connected else f"Bambu printer not reachable: {p.error}",
                    "hostname": p.name, "software_version": f"Bambu Lab {p.model}", "cpu_info": p.model,
                    "klipper_path": "", "python_path": "", "log_file": "", "config_file": "", "app": "Klipper"}
        if method == "server.connection.identify":
            return {"connection_id": client.id}
        if method == "server.websocket.id":
            return {"websocket_id": client.id}
        if method == "server.config":
            return {"config": {"server": {"host": "0.0.0.0", "port": self.port},
                               "file_manager": {"enable_object_processing": False}}, "orig": {}, "files": []}
        if method == "printer.gcode.script":
            return await self.script(str(params.get("script", "")))
        if method == "printer.gcode.help":
            return {"PAUSE": "Pause the print", "RESUME": "Resume the print", "CANCEL_PRINT": "Cancel the print",
                    "SET_PIN": "PIN=chamber_light VALUE=0|1", "SET_FAN_SPEED": "FAN=Aux|Chamber SPEED=0..1",
                    "M220": "Print speed (silent / standard / sport / ludicrous)"}
        if method == "printer.print.start":
            await self.start_print(str(params.get("filename", "")))
            return "ok"
        if method == "printer.print.pause":
            await p.pause()
            return "ok"
        if method == "printer.print.resume":
            await p.resume()
            return "ok"
        if method in ("printer.print.cancel", "printer.emergency_stop"):
            await p.stop()
            return "ok"
        if method in ("printer.restart", "printer.firmware_restart", "server.restart"):
            return "ok"
        if method == "printer.query_endstops.status":
            return {}
        if method == "server.temperature_store":
            return {k: {kk: list(vv) for kk, vv in v.items()} for k, v in self.temps.items()}
        if method == "server.gcode_store":
            n = int(params.get("count") or 100)
            return {"gcode_store": list(self.gcodes)[-n:]}
        if method == "server.webcams.list":
            return {"webcams": self.webcams(client.host)}
        if method == "server.webcams.get_item":
            return {"webcam": self.webcams(client.host)[0]}
        if method in ("server.webcams.post_item", "server.webcams.delete_item"):
            raise RPCError(400, "the Bambu camera is built in and can't be changed")
        # files
        if method == "server.files.roots":
            return [{"name": "gcodes", "path": "/sdcard", "permissions": "rw"},
                    {"name": "config", "path": "/config", "permissions": "r"}]
        if method == "server.files.list":
            if params.get("root", "gcodes") != "gcodes":
                return []
            try:
                return await self.list_files()
            except RPCError as e:
                log.warning("%s: %s", p.name, e)
                return []
        if method == "server.files.get_directory":
            path = str(params.get("path") or "gcodes").strip("/")
            if path.split("/")[0] != "gcodes":
                return {"dirs": [], "files": [], "disk_usage": {"total": 0, "used": 0, "free": 0},
                        "root_info": {"name": path.split("/")[0], "permissions": "r"}}
            _, rest = self._split(path)
            try:
                dirs, files = await self._ftp(p.files.listdir, "/" + rest)
            except RPCError as e:
                if rest:
                    raise
                log.warning("%s: %s", p.name, e)
                dirs, files = [], []
            sd = p.report.get("sdcard")
            return {"dirs": [{"dirname": d["name"], "modified": d["modified"], "size": 0, "permissions": "rw"}
                             for d in dirs if not d["name"].startswith(".")],
                    "files": [{"filename": f["name"], "modified": f["modified"], "size": f["size"], "permissions": "rw"}
                              for f in files if f["name"].lower().endswith(PRINT_EXT)],
                    "disk_usage": {"total": 0, "used": 0, "free": 0},
                    "root_info": {"name": "gcodes", "permissions": "rw" if sd is not False else "r"}}
        if method in ("server.files.metadata", "server.files.metascan"):
            return await self.metadata(str(params.get("filename", "")))
        if method == "server.files.delete_file":
            _, rest = self._split(str(params.get("path", "")))
            await self._ftp(p.files.delete, "/" + rest)
            item = self._item(rest)
            await self._files_changed("delete_file", item)
            return {"item": item, "action": "delete_file"}
        if method in ("server.files.move", "server.files.copy"):
            _, src = self._split(str(params.get("source", "")))
            _, dst = self._split(str(params.get("dest", "")))
            if method == "server.files.move":
                await self._ftp(p.files.rename, "/" + src, "/" + dst)
            else:
                await self._ftp(p.files.write, "/" + dst, await self._ftp(p.files.read, "/" + src))
            action = "move_file" if method == "server.files.move" else "create_file"
            item, source = self._item(dst), self._item(src)
            await self._files_changed(action, item, source if action == "move_file" else None)
            return {"item": item, "source_item": source, "action": action}
        if method == "server.files.post_directory":
            _, rest = self._split(str(params.get("path", "")))
            await self._ftp(p.files.mkdir, "/" + rest)
            item = self._item(rest)
            await self._files_changed("create_dir", item)
            return {"item": item, "action": "create_dir"}
        if method == "server.files.delete_directory":
            _, rest = self._split(str(params.get("path", "")))
            await self._ftp(p.files.rmdir, "/" + rest)
            item = self._item(rest)
            await self._files_changed("delete_dir", item)
            return {"item": item, "action": "delete_dir"}
        # database
        if method == "server.database.list":
            return {"namespaces": list(self.db), "backups": []}
        if method.startswith("server.database."):
            return self._database(method.rsplit(".", 1)[1], params)
        # history
        if method.startswith("server.history."):
            return self._history(method.rsplit(".", 1)[1], params)
        # stubs for things a Bambu printer doesn't have
        if method == "server.announcements.list":
            return {"entries": [], "feeds": []}
        if method == "server.sensors.list":
            return {"sensors": {}}
        if method == "machine.system_info":
            return {"system_info": {"cpu_info": {"cpu_count": 0, "processor": p.model, "model": p.model},
                                    "sd_info": {}, "distribution": {"name": f"Bambu Lab {p.model}"},
                                    "available_services": [], "instance_ids": {"moonraker": "moonraker", "klipper": "klipper"},
                                    "service_state": {}, "virtualization": {}, "python": {}, "network": {},
                                    "canbus": {}}}
        if method == "machine.proc_stats":
            return {"moonraker_stats": [], "throttled_state": {"bits": 0, "flags": []}, "cpu_temp": None,
                    "network": {}, "system_cpu_usage": {}, "system_memory": {}, "system_uptime": 0,
                    "websocket_connections": len(self.clients)}
        if method in ("machine.update.status", "machine.update.refresh"):
            return {"busy": False, "github_rate_limit": None, "github_requests_remaining": None,
                    "github_limit_reset_time": None, "version_info": {}}
        if method == "machine.device_power.devices":
            return {"devices": []}
        if method.startswith("machine.peripherals."):
            return {"usb_devices": [], "serial_devices": [], "v4l2_devices": [], "libcamera_devices": [], "can_uuids": []}
        if method == "access.info":
            return {"default_source": "moonraker", "available_sources": ["moonraker"], "login_required": False,
                    "trusted": True}
        if method == "access.oneshot_token":
            return "bambu"
        if method == "access.get_user":
            return {"username": "_TRUSTED_USER_", "source": "moonraker", "created_on": 0, "groups": ["admin"]}
        if method == "server.job_queue.status":
            return {"queued_jobs": [], "queue_state": "ready"}
        if method == "access.users.list":
            return {"users": [{"username": "_TRUSTED_USER_", "source": "moonraker", "created_on": 0}]}
        if method == "access.get_api_key":
            return "bambu"
        if method == "server.announcements.dismiss":
            return {"entry_id": params.get("entry_id")}
        if method == "server.logs.rollover":
            return {"rolled_over": [], "failed": {}}
        if method == "machine.device_power.status":
            return {}
        if method in ("machine.services.restart", "machine.services.start", "machine.services.stop"):
            return "ok"
        raise RPCError(404, f"Method not found: {method}")

    def _database(self, action: str, params: dict):
        ns, key = params.get("namespace"), params.get("key")
        if not ns:
            raise RPCError(400, "namespace required")
        parts = [k for k in str(key).split(".") if k] if key not in (None, "") else []
        if action == "get_item":
            if ns not in self.db:
                raise RPCError(404, f"Namespace '{ns}' not found")
            v = self.db[ns]
            for k in parts:
                if not isinstance(v, dict) or k not in v:
                    raise RPCError(404, f"Key '{key}' in namespace '{ns}' not found")
                v = v[k]
            return {"namespace": ns, "key": key, "value": v}
        if action == "post_item":
            value = params.get("value")
            if isinstance(value, str):
                with contextlib.suppress(ValueError):
                    value = json.loads(value)
            if not parts:
                if not isinstance(value, dict):
                    raise RPCError(400, "a whole namespace must be an object")
                self.db[ns] = value
            else:
                d = self.db.setdefault(ns, {})
                for k in parts[:-1]:
                    if not isinstance(d.get(k), dict):
                        d[k] = {}
                    d = d[k]
                d[parts[-1]] = value
            _save(self.dir / "database.json", self.db)
            return {"namespace": ns, "key": key, "value": value}
        if action == "delete_item":
            d = self.db.get(ns)
            if d is None or not parts:
                raise RPCError(404, f"Key '{key}' in namespace '{ns}' not found")
            for k in parts[:-1]:
                d = d.get(k) if isinstance(d, dict) else None
            if not isinstance(d, dict) or parts[-1] not in d:
                raise RPCError(404, f"Key '{key}' in namespace '{ns}' not found")
            value = d.pop(parts[-1])
            _save(self.dir / "database.json", self.db)
            return {"namespace": ns, "key": key, "value": value}
        raise RPCError(404, f"Method not found: server.database.{action}")

    def _history(self, action: str, params: dict):
        jobs = self.history["jobs"]
        if action == "list":
            order = str(params.get("order", "desc"))
            since, before = float(params.get("since") or 0), float(params.get("before") or 0)
            sel = [j for j in jobs if j["start_time"] >= since and (not before or j["start_time"] < before)]
            if order != "asc":
                sel = sel[::-1]
            start, limit = int(params.get("start") or 0), int(params.get("limit") or 50)
            return {"count": len(sel), "jobs": sel[start:start + limit]}
        if action == "get_job":
            job = next((j for j in jobs if j["job_id"] == params.get("uid")), None)
            if not job:
                raise RPCError(404, f"Invalid job uid: {params.get('uid')}")
            return {"job": job}
        if action == "delete_job":
            if params.get("all") in (True, "true", "True"):
                ids = [j["job_id"] for j in jobs]
                jobs.clear()
            else:
                ids = [j["job_id"] for j in jobs if j["job_id"] == params.get("uid")]
                jobs[:] = [j for j in jobs if j["job_id"] not in ids]
            _save(self.dir / "history.json", self.history)
            return {"deleted_jobs": ids}
        if action in ("totals", "reset_totals"):
            done = [j for j in jobs if j["status"] != "in_progress"]
            totals = {"total_jobs": len(done), "total_time": sum(j["total_duration"] for j in done),
                      "total_print_time": sum(j["print_duration"] for j in done),
                      "total_filament_used": sum(j["filament_used"] for j in done),
                      "longest_job": max((j["total_duration"] for j in done), default=0.0),
                      "longest_print": max((j["print_duration"] for j in done), default=0.0)}
            if action == "reset_totals":
                return {"last_totals": totals, "last_auxiliary_totals": []}
            return {"job_totals": totals, "auxiliary_totals": []}
        raise RPCError(404, f"Method not found: server.history.{action}")


# ------------------------------------------------------------------ app

HTTP_METHODS = {
    ("GET", "server/database/item"): "server.database.get_item",
    ("POST", "server/database/item"): "server.database.post_item",
    ("DELETE", "server/database/item"): "server.database.delete_item",
    ("GET", "server/history/job"): "server.history.get_job",
    ("DELETE", "server/history/job"): "server.history.delete_job",
    ("GET", "server/files/directory"): "server.files.get_directory",
    ("POST", "server/files/directory"): "server.files.post_directory",
    ("DELETE", "server/files/directory"): "server.files.delete_directory",
    ("GET", "server/webcams/item"): "server.webcams.get_item",
    ("POST", "server/history/reset_totals"): "server.history.reset_totals",
}


def build(printers: list[dict], data_dir: Path) -> Starlette:
    bridges = {int(cfg["port"]): Bridge(cfg, data_dir) for cfg in printers}

    def bridge_for(scope) -> Bridge:
        return bridges[int((scope.get("server") or ("", 0))[1])]

    def host_of(scope) -> str:
        for k, v in scope.get("headers", []):
            if k == b"host":
                return v.decode()
        b = bridge_for(scope)
        return f"127.0.0.1:{b.port}"

    async def websocket(ws: WebSocket):
        b = bridge_for(ws.scope)
        await ws.accept()
        client = Client(next(b._ids), ws, host_of(ws.scope))
        b.clients[client.id] = client
        try:
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except ValueError:
                    continue
                asyncio.create_task(handle(b, client, msg))
        except WebSocketDisconnect:
            pass
        finally:
            b.clients.pop(client.id, None)

    async def handle(b: Bridge, client: Client, msg: dict):
        rid = msg.get("id")
        try:
            result = await b.call(str(msg.get("method", "")), msg.get("params") or {}, client)
            reply = {"jsonrpc": "2.0", "result": result, "id": rid}
        except RPCError as e:
            reply = {"jsonrpc": "2.0", "error": {"code": e.code, "message": str(e)}, "id": rid}
        except Exception as e:
            log.exception("%s failed", msg.get("method"))
            reply = {"jsonrpc": "2.0", "error": {"code": 500, "message": str(e)}, "id": rid}
        if rid is not None:
            await b._send(client, reply)

    async def webcam(request: Request):
        b = bridge_for(request.scope)
        if request.query_params.get("action") == "stream":
            async def gen():
                async for jpeg in b.p.camera.frames():
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg) + jpeg + b"\r\n"
            return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")
        jpeg = await b.p.camera.snapshot()
        if not jpeg:
            return Response("camera not available", status_code=503)
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def query_objects(request: Request) -> dict:
        objs = {}
        for k, v in request.query_params.multi_items():
            objs[k] = [f for f in v.split(",") if f] or None
        return {"objects": objs}

    async def http(request: Request):
        b = bridge_for(request.scope)
        path = request.path_params["path"].strip("/")
        verb = request.method
        client = Client(0, None, host_of(request.scope))
        try:
            if path == "server/files/upload" and verb == "POST":
                form = await request.form()
                f = form.get("file")
                if f is None:
                    raise RPCError(400, "no file")
                folder = str(form.get("path") or "")
                result = await b.upload(str(form.get("root") or "gcodes"), folder, f.filename, await f.read(),
                                        str(form.get("print") or "").lower() == "true")
                return JSONResponse({"result": result}, status_code=201)
            if path.startswith("server/files/") and path.split("/")[2] in ("gcodes", "config") \
                    and len(path.split("/")) > 3:
                root, rel = path.split("/", 3)[2:]
                if verb == "DELETE":
                    result = await b.call("server.files.delete_file", {"path": f"{root}/{rel}"}, client)
                    return JSONResponse({"result": result})
                if root != "gcodes":
                    raise RPCError(404, "not found")
                data = await b.download(rel)
                media = "image/png" if rel.endswith(".png") else "application/octet-stream"
                return Response(data, media_type=media)
            method = HTTP_METHODS.get((verb, path)) or path.replace("/", ".")
            if path == "printer/objects/query" or path == "printer/objects/subscribe":
                params = query_objects(request)
            else:
                params = dict(request.query_params)
                if verb in ("POST", "DELETE"):
                    with contextlib.suppress(ValueError):
                        body = await request.json()
                        if isinstance(body, dict):
                            params.update(body)
            result = await b.call(method, params, client)
            return JSONResponse({"result": result})
        except RPCError as e:
            return JSONResponse({"error": {"code": e.code, "message": str(e)}},
                                status_code=e.code if 400 <= e.code < 600 else 500)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        for b in bridges.values():
            b.start()
        yield
        for b in bridges.values():
            await b.stop()

    return Starlette(routes=[
        WebSocketRoute("/websocket", websocket),
        Route("/webcam/", webcam),
        Route("/{path:path}", http, methods=["GET", "POST", "DELETE"]),
    ], middleware=[Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])],
        lifespan=lifespan)
