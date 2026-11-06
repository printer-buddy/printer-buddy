"""Elegoo Centauri Carbon printers as Moonraker.

CC1 speaks SDCP over a WebSocket (port 3030), CC2 speaks MQTT (port 1883). Both are turned into the same
Klipper-style objects that printer.py gives for a Bambu printer, so server.py serves either one.
Written from community protocol notes; not tested on a real printer.
"""
import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
import urllib.request
import uuid

import websockets

from .camera import Camera
from .mqtt import MQTT

log = logging.getLogger("elegoo.printer")

CC1_STATES = {0: "standby", 1: "printing", 5: "paused", 6: "paused", 8: "cancelled", 9: "complete", 10: "printing",
              11: "printing", 12: "printing", 13: "printing", 14: "error", 15: "printing", 16: "printing",
              17: "printing", 18: "printing", 19: "printing", 20: "printing", 21: "printing", 22: "printing"}
FANS = {"fan": "ModelFan", "fan_generic Aux": "AuxiliaryFan", "fan_generic Chamber": "BoxFan"}
SPEED_MODES = {0: 50, 1: 100, 2: 130, 3: 160}
UPLOAD_CHUNK = 1024 * 1024


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pair(v) -> tuple[float, float]:
    """(actual, target) from a number or an older [target, actual] pair."""
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return _num(v[1]), _num(v[0])
    return _num(v), 0.0


def _unfraction(v) -> float:
    v = _num(v)
    return round(v if v <= 1 else v / 255, 3)


class MjpegCamera(Camera):
    """The printer's own MJPEG stream, shared by everyone watching and stopped when nobody is."""

    def __init__(self, host: str, url: str):
        super().__init__(host, "")
        self.url = url

    async def _jpeg(self):
        m = re.match(r"http://([^/:]+)(?::(\d+))?(/.*)?$", self.url)
        host, port, path = m.group(1), int(m.group(2) or 80), m.group(3) or "/"
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
        try:
            w.write(f"GET {path} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
            await w.drain()
            buf = b""
            while time.monotonic() - self._wanted < 30:
                chunk = await asyncio.wait_for(r.read(65536), 15)
                if not chunk:
                    raise ConnectionError("camera stream ended")
                buf += chunk
                while (end := buf.find(b"\xff\xd9")) != -1:
                    start = buf.find(b"\xff\xd8")
                    if start != -1 and start < end:
                        await self._publish(buf[start:end + 2])
                    buf = buf[end + 2:]
                if len(buf) > 4_000_000:
                    buf = b""
        finally:
            w.close()


class Files:
    """Printer storage over the printer's own protocol. Folders and renaming aren't available."""

    def __init__(self, printer):
        self.p = printer

    async def listdir(self, path: str):
        if path.strip("/"):
            raise RuntimeError("no such folder")
        return [], await self.p.file_list()

    async def read(self, path: str) -> bytes:
        return await self.p.file_read(path.lstrip("/"))

    async def write(self, path: str, data: bytes):
        await self.p.file_write(path.lstrip("/"), data)

    async def delete(self, path: str):
        await self.p.file_delete(path.lstrip("/"))

    async def rename(self, src: str, dst: str):
        raise RuntimeError("renaming isn't available on an Elegoo printer")

    async def mkdir(self, path: str):
        raise RuntimeError("folders aren't available on an Elegoo printer")

    async def rmdir(self, path: str):
        raise RuntimeError("folders aren't available on an Elegoo printer")


class Elegoo:
    brand = "Elegoo"
    camera_name = "Elegoo camera"
    new_protocol = False

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg.get("name") or "Elegoo"
        self.host, self.serial, self.code = cfg["host"], str(cfg["serial"]).strip(), str(cfg.get("access_code") or "").strip()
        self.model = "Centauri Carbon"
        self.firmware = ""
        self.connected = False
        self.error: str | None = None
        self.n: dict = {}
        self.files = Files(self)
        self.listeners: list = []
        self.gcode_listeners: list = []
        self._job_started: float | None = None
        self._last_state = "standby"
        self._seen_state = "standby"
        self.job_ended: tuple | None = None
        self._ended: str | None = None
        self._duration = 0.0

    # ------------------------------------------------------------ shared state

    def _changed(self):
        for cb in list(self.listeners):
            cb()

    def state(self) -> str:
        s = self.n.get("state", "standby")
        return self._ended if s == "standby" and self._ended else s

    def _track_job(self):
        s, now = self.state(), time.time()
        if s in ("printing", "paused"):
            self._ended = None
            self._duration = _num(self.n.get("elapsed"))
        if s in ("printing", "paused") and self._job_started is None:
            self._job_started = now - self._duration
        if s not in ("printing", "paused") and self._last_state in ("printing", "paused"):
            end = "cancelled" if s == "standby" else s
            self.job_ended = (self.filename(), self._duration, end)
            self._ended = end
            self._job_started = None
        self._last_state = s

    def job_start(self) -> float | None:
        return self._job_started

    def print_duration(self) -> float:
        return self._duration if self._job_started is not None else 0.0

    def total_duration(self) -> float:
        return 0.0 if self._job_started is None else time.time() - self._job_started

    def filename(self) -> str:
        return re.sub(r"^/(local|udisk)/", "", str(self.n.get("filename") or "")).lstrip("/")

    def objects(self) -> dict:
        n, s = self.n, self.state()
        printing = s in ("printing", "paused")
        pct = max(0.0, min(1.0, _num(n.get("progress"))))
        nozzle, nozzle_t = n.get("nozzle", (0.0, 0.0))
        bed, bed_t = n.get("bed", (0.0, 0.0))
        box = n.get("box")
        layer, layers = int(_num(n.get("layer"))), int(_num(n.get("layers")))
        x, y, z = (256.0, 256.0, 256.0)
        message = f"print failed ({n.get('error')})" if s == "error" else ""
        o = {
            "webhooks": {"state": "ready" if self.connected else "shutdown",
                         "state_message": "Printer is ready" if self.connected else f"Elegoo printer not reachable: {self.error}"},
            "print_stats": {"filename": self.filename() if printing or s in ("complete", "error", "cancelled") else "",
                            "state": s, "message": message, "print_duration": self.print_duration(),
                            "total_duration": self.total_duration(), "filament_used": 0.0,
                            "info": {"current_layer": layer or None, "total_layer": layers or None}},
            "virtual_sdcard": {"progress": pct, "is_active": s == "printing", "file_position": 0, "file_size": 0,
                               "file_path": f"/sdcard/{self.filename()}" if printing else None},
            "display_status": {"progress": pct, "message": None},
            "idle_timeout": {"state": "Printing" if s == "printing" else "Ready" if printing else "Idle",
                             "printing_time": self.print_duration()},
            "pause_resume": {"is_paused": s == "paused"},
            "extruder": {"temperature": nozzle, "target": nozzle_t, "power": 1.0 if nozzle_t > nozzle + 2 else 0.0,
                         "can_extrude": nozzle >= 170, "pressure_advance": 0.0, "smooth_time": 0.04},
            "heater_bed": {"temperature": bed, "target": bed_t, "power": 1.0 if bed_t > bed + 2 else 0.0},
            "heaters": {"available_heaters": ["extruder", "heater_bed"],
                        "available_sensors": ["extruder", "heater_bed"] + (["temperature_sensor Chamber"] if box is not None else []),
                        "available_monitors": []},
            "gcode_move": {"speed_factor": _num(n.get("speed"), 100) / 100, "extrude_factor": 1.0,
                           "homing_origin": [0.0, 0.0, 0.0, 0.0], "position": [0.0, 0.0, 0.0, 0.0],
                           "gcode_position": [0.0, 0.0, 0.0, 0.0], "absolute_coordinates": True,
                           "absolute_extrude": True, "speed": 0.0},
            "toolhead": {"homed_axes": "", "position": [0.0, 0.0, 0.0, 0.0], "axis_minimum": [0.0, 0.0, 0.0, 0.0],
                         "axis_maximum": [x, y, z, 0.0], "extruder": "extruder", "max_velocity": 500.0,
                         "max_accel": 20000.0, "square_corner_velocity": 5.0, "minimum_cruise_ratio": 0.5,
                         "max_accel_to_decel": 10000.0, "print_time": 0.0, "estimated_print_time": 0.0, "stalls": 0},
            "motion_report": {"live_position": [0.0, 0.0, 0.0, 0.0], "live_velocity": 0.0, "live_extruder_velocity": 0.0},
            "exclude_object": {"objects": [], "excluded_objects": [], "current_object": None},
            "system_stats": {"sysload": 0.0, "cputime": 0.0, "memavail": 0},
            "mcu": {"mcu_version": self.firmware, "mcu_build_versions": "", "mcu_constants": {}, "last_stats": {}},
            "output_pin Chamber_light": {"value": 1.0 if n.get("light") else 0.0},
            "configfile": {"warnings": [], "save_config_pending": False, "save_config_pending_items": {},
                           "settings": {"extruder": {"max_temp": 300.0, "min_temp": 0.0},
                                        "heater_bed": {"max_temp": 110.0, "min_temp": 0.0}}, "config": {}},
        }
        if box is not None:
            o["temperature_sensor Chamber"] = {"temperature": box, "measured_min_temp": box, "measured_max_temp": box}
        fans = n.get("fans") or {}
        for fan, key in FANS.items():
            if key in fans:
                o[fan] = {"speed": fans[key], "rpm": None}
        return o

    # ------------------------------------------------------------ commands the server expects

    async def pause(self):
        raise NotImplementedError

    async def resume(self):
        raise NotImplementedError

    async def stop(self):
        raise NotImplementedError

    async def light(self, on: bool):
        raise ValueError("the light can't be switched from here on this printer")

    async def speed(self, percent: float):
        raise ValueError("print speed can't be changed from here on this printer")

    async def ams_load(self, gate: int):
        raise ValueError("this printer has no AMS")

    async def ams_unload(self):
        raise ValueError("this printer has no AMS")

    async def nozzle_temp(self, index: int, target: float):
        await self._set_temps(nozzle=target)

    async def gcode(self, line: str):
        words = line.split()
        cmd = words[0].upper() if words else ""
        m = re.search(r"S(-?[\d.]+)", line, re.I)
        value = float(m.group(1)) if m else 0.0
        if cmd in ("M104", "M109"):
            return await self._set_temps(nozzle=value)
        if cmd in ("M140", "M190"):
            return await self._set_temps(bed=value)
        if cmd in ("M106", "M107"):
            p = re.search(r"P(\d)", line, re.I)
            keys = {"1": "ModelFan", "2": "AuxiliaryFan", "3": "BoxFan"}
            return await self._set_fan(keys.get(p.group(1) if p else "1", "ModelFan"), 0.0 if cmd == "M107" else value / 255)
        raise ValueError(f"{cmd or 'that'} isn't available on an Elegoo printer")

    async def start(self, path: str, plate: int = 1, filaments: list[dict] | None = None):
        raise NotImplementedError

    async def _set_temps(self, nozzle: float | None = None, bed: float | None = None):
        raise NotImplementedError

    async def _set_fan(self, fan: str, fraction: float):
        raise ValueError("fan speed can't be changed from here on this printer")

    async def file_list(self) -> list[dict]:
        return []

    async def file_read(self, path: str) -> bytes:
        raise RuntimeError("downloading files isn't available on this printer")

    async def file_write(self, path: str, data: bytes):
        raise NotImplementedError

    async def file_delete(self, path: str):
        raise RuntimeError("deleting files isn't available on this printer")


def _http_upload(url: str, fields: dict, filename: str, data: bytes, headers: dict | None = None) -> dict:
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="File"; filename="{filename}"\r\n'
             f'Content-Type: application/octet-stream\r\n\r\n').encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode(errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return {}


class CC1(Elegoo):
    """Centauri Carbon: SDCP over a WebSocket. Only the commands that work on stock firmware are sent unless
    "extra_commands": true is set in the printer's entry (for OpenCentauri firmware)."""

    camera_name = "Centauri Carbon camera"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.model = "Centauri Carbon"
        self.ws_port = int(cfg.get("ws_port") or 3030)
        self.http_ports = [int(cfg["upload_port"])] if cfg.get("upload_port") else [3030, 80]
        self.extra = bool(cfg.get("extra_commands"))
        self.camera = MjpegCamera(self.host, f"http://{self.host}:{int(cfg.get('camera_port') or 3031)}/video")
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------ connection

    async def run(self):
        backoff = 2
        while True:
            try:
                async with websockets.connect(f"ws://{self.host}:{self.ws_port}/websocket", ping_interval=None,
                                              open_timeout=10, max_size=2 ** 22) as ws:
                    self._ws, self.connected, self.error, backoff = ws, True, None, 2
                    log.info("%s: connected to %s", self.name, self.host)
                    poll = asyncio.create_task(self._poll())
                    try:
                        await self._send(0, wait=False)
                        await self._send(1, wait=False)
                        await self._send(512, {"TimePeriod": 1000}, wait=False)
                        self._changed()
                        async for raw in ws:
                            self._on_message(raw)
                    finally:
                        poll.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = str(e) or type(e).__name__
                log.info("%s: %s", self.name, self.error)
            self._ws, self.connected = None, False
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("printer disconnected"))
            self._pending.clear()
            self._changed()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _poll(self):
        while True:
            await asyncio.sleep(10)
            with contextlib.suppress(Exception):
                await self._send(0, wait=False)

    async def _send(self, cmd: int, data: dict | None = None, wait: bool = True, timeout: float = 10) -> dict:
        if not self._ws:
            raise ConnectionError("printer not connected")
        rid = uuid.uuid4().hex[:16]
        msg = {"Id": self.serial, "Topic": f"sdcp/request/{self.serial}",
               "Data": {"Cmd": cmd, "Data": data or {}, "RequestID": rid, "MainboardID": self.serial,
                        "TimeStamp": int(time.time() * 1000), "From": 1}}
        fut = None
        if wait:
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
        await self._ws.send(json.dumps(msg))
        if not fut:
            return {}
        try:
            reply = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)
        inner = (reply.get("Data") or {}).get("Data") or {}
        if inner.get("Ack") not in (None, 0):
            raise RuntimeError(f"the printer refused it (code {inner.get('Ack')})")
        return inner

    def _on_message(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        topic = str(msg.get("Topic") or "")
        if topic.startswith("sdcp/response/"):
            rid = (msg.get("Data") or {}).get("RequestID")
            fut = self._pending.get(rid)
            if fut and not fut.done():
                fut.set_result(msg)
        elif topic.startswith("sdcp/status/") or "Status" in msg:
            self._apply(msg.get("Status") or {})
        elif topic.startswith("sdcp/attributes/"):
            a = msg.get("Attributes") or {}
            self.firmware = str(a.get("FirmwareVersion") or self.firmware)
            self.model = str(a.get("MachineName") or self.model)
            self._changed()

    def _apply(self, st: dict):
        info = st.get("PrintInfo") or {}
        code = info.get("Status")
        if code is None:
            cur = st.get("CurrentStatus")
            code = (cur[0] if isinstance(cur, list) and cur else cur)
        n = self.n
        try:
            mapped = CC1_STATES.get(int(code))
        except (TypeError, ValueError):
            mapped = None
        if mapped:
            n["state"] = self._seen_state = mapped
        else:
            n["state"] = self._seen_state
        total, current = _num(info.get("TotalTicks")), _num(info.get("CurrentTicks"))
        n["elapsed"], n["remaining"] = current, max(0.0, total - current)
        n["filename"] = info.get("Filename") or n.get("filename") or ""
        n["layer"], n["layers"] = info.get("CurrentLayer"), info.get("TotalLayer")
        if info.get("Progress") is not None:
            n["progress"] = _num(info.get("Progress")) / 100
        elif _num(info.get("TotalLayer")):
            n["progress"] = _num(info.get("CurrentLayer")) / _num(info.get("TotalLayer"))
        if info.get("PrintSpeedPct") is not None:
            n["speed"] = _num(info.get("PrintSpeedPct"), 100)
        if int(_num(info.get("ErrorNumber"))):
            n["error"] = f"error {int(_num(info.get('ErrorNumber')))}"
        n["nozzle"] = (_num(st.get("TempOfNozzle")), _num(st.get("TempTargetNozzle"))) if not isinstance(st.get("TempOfNozzle"), list) else _pair(st.get("TempOfNozzle"))
        n["bed"] = (_num(st.get("TempOfHotbed")), _num(st.get("TempTargetHotbed"))) if not isinstance(st.get("TempOfHotbed"), list) else _pair(st.get("TempOfHotbed"))
        if st.get("TempOfBox") is not None:
            n["box"] = _pair(st.get("TempOfBox"))[0]
        fans = st.get("CurrentFanSpeed")
        if isinstance(fans, dict):
            n["fans"] = {k: round(_num(v) / 100, 2) for k, v in fans.items()}
        light = st.get("LightStatus")
        if isinstance(light, dict) and "SecondLight" in light:
            n["light"] = bool(light["SecondLight"])
        self._track_job()
        self._changed()

    # ------------------------------------------------------------ commands

    async def pause(self):
        await self._send(129)

    async def resume(self):
        await self._send(131)

    async def stop(self):
        await self._send(130)

    def _need_extra(self, what: str):
        if not self.extra:
            raise ValueError(f"{what} isn't sent to stock Centauri Carbon firmware: it can crash the printer's software "
                             'and stop a print. Set "extra_commands": true for this printer if it runs OpenCentauri.')

    async def light(self, on: bool):
        self._need_extra("Changing the light")
        await self._send(403, {"LightStatus": {"SecondLight": 1 if on else 0}})

    async def speed(self, percent: float):
        self._need_extra("Changing the print speed")
        await self._send(403, {"PrintSpeedPct": int(percent)})

    async def _set_temps(self, nozzle: float | None = None, bed: float | None = None):
        self._need_extra("Changing a temperature")
        data = {}
        if nozzle is not None:
            data["TempTargetNozzle"] = int(nozzle)
        if bed is not None:
            data["TempTargetHotbed"] = int(bed)
        await self._send(403, data)

    async def _set_fan(self, fan: str, fraction: float):
        self._need_extra("Changing a fan")
        await self._send(403, {"TargetFanSpeed": {fan: int(round(fraction * 100))}})

    async def start(self, path: str, plate: int = 1, filaments: list[dict] | None = None):
        await self._send(128, {"Filename": f"/local/{path.lstrip('/')}", "StartLayer": 0, "Calibration_switch": 0,
                               "PrintPlatformType": 0, "Tlp_Switch": 0, "slot_map": []})

    # ------------------------------------------------------------ files

    async def file_list(self) -> list[dict]:
        if not self.extra:
            return []
        reply = await self._send(258, {"Url": "/local"})
        out = []
        for f in reply.get("FileList") or []:
            name = str(f.get("name") or "")
            if name and f.get("type", 1) != 0:
                out.append({"name": name.split("/")[-1], "size": int(_num(f.get("FileSize"))),
                            "modified": _num(f.get("CreateTime")) or time.time()})
        return out

    async def file_delete(self, path: str):
        self._need_extra("Deleting a file")
        await self._send(259, {"FileList": [f"/local/{path}"]})

    async def file_read(self, path: str) -> bytes:
        for port in self.http_ports:
            try:
                return await asyncio.to_thread(self._get, f"http://{self.host}:{port}/downloadFile/local/{path}")
            except OSError:
                continue
        raise RuntimeError("couldn't download the file")

    @staticmethod
    def _get(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()

    async def file_write(self, path: str, data: bytes):
        await asyncio.to_thread(self._upload, path, data)

    def _upload(self, path: str, data: bytes):
        md5 = hashlib.md5(data).hexdigest()
        uid, total = uuid.uuid4().hex, len(data)
        last_error = None
        for port in self.http_ports:
            try:
                for off in range(0, max(total, 1), UPLOAD_CHUNK):
                    reply = _http_upload(f"http://{self.host}:{port}/uploadFile/upload",
                                         {"Check": 1, "S-File-MD5": md5, "Offset": off, "Uuid": uid, "TotalSize": total},
                                         path.rsplit("/", 1)[-1], data[off:off + UPLOAD_CHUNK])
                    if reply.get("code") not in (None, "000000"):
                        raise RuntimeError(f"the printer refused the upload ({reply.get('code')})")
                time.sleep(1)
                return
            except OSError as e:
                last_error = e
        raise RuntimeError(f"couldn't upload: {last_error}")


class CC2(Elegoo):
    """Centauri Carbon 2: MQTT on port 1883. The topics and methods come from a community protocol write-up."""

    camera_name = "Centauri Carbon 2 camera"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.model = "Centauri Carbon 2"
        self.mqtt_port = int(cfg.get("mqtt_port") or 1883)
        self.http_port = int(cfg.get("http_port") or 80)
        self.camera = MjpegCamera(self.host, f"http://{self.host}:{int(cfg.get('camera_port') or 8080)}/")
        self.client_id = f"1_PC_{int(time.time()) % 100000}"
        self._mqtt: MQTT | None = None
        self._ids = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self.new_protocol = True

    @property
    def _base(self) -> str:
        return f"elegoo/{self.serial}"

    async def run(self):
        backoff = 2
        while True:
            m = MQTT(self.host, "elegoo", self.code, self.client_id, port=self.mqtt_port, tls=False)
            try:
                await m.connect()
                await m.subscribe(f"{self._base}/#")
                self._mqtt = m
                await m.publish(f"{self._base}/api_register", json.dumps(
                    {"client_id": self.client_id, "request_id": f"{self.client_id}_req"}).encode())
                self.connected, self.error, backoff = True, None, 2
                log.info("%s: connected to %s", self.name, self.host)
                self._changed()
                keep = asyncio.create_task(self._keepalive())
                try:
                    async for topic, payload in m.messages():
                        self._on_message(topic, payload)
                finally:
                    keep.cancel()
            except asyncio.CancelledError:
                await m.close()
                raise
            except Exception as e:
                self.error = str(e) or type(e).__name__
                log.info("%s: %s", self.name, self.error)
            self._mqtt, self.connected = None, False
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("printer disconnected"))
            self._pending.clear()
            await m.close()
            self._changed()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _keepalive(self):
        await asyncio.sleep(1)
        while True:
            with contextlib.suppress(Exception):
                await self._publish({"type": "PING"})
                await self._call(1002, wait=False)
            await asyncio.sleep(10)

    async def _publish(self, body: dict):
        if not self._mqtt:
            raise ConnectionError("printer not connected")
        await self._mqtt.publish(f"{self._base}/{self.client_id}/api_request", json.dumps(body).encode())

    async def _call(self, method: int, params: dict | None = None, wait: bool = True, timeout: float = 10) -> dict:
        async with self._lock:
            gap = 2.0 - (time.monotonic() - self._last_request)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last_request = time.monotonic()
            self._ids += 1
            rid = self._ids
            fut = None
            if wait:
                fut = asyncio.get_running_loop().create_future()
                self._pending[rid] = fut
            await self._publish({"id": rid, "method": method, "params": params or {}})
        if not fut:
            return {}
        try:
            reply = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)
        code = int(_num(reply.get("error_code")))
        if code:
            raise RuntimeError(f"the printer refused it (error {code})")
        return reply.get("result") or {}

    def _on_message(self, topic: str, payload: bytes):
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        tail = topic.rsplit("/", 1)[-1]
        if tail in ("api_response", "api_status"):
            rid = msg.get("id")
            result = msg.get("result")
            fut = self._pending.get(rid) if tail == "api_response" else None
            if fut and not fut.done():
                fut.set_result(msg)
            if isinstance(result, dict) and ("machine_status" in result or "extruder" in result):
                self._apply(result)
            elif msg.get("method") in (6000, 6008) and isinstance(msg.get("params"), dict):
                self._apply(msg["params"])
            elif msg.get("method") == 1001 or (isinstance(result, dict) and "firmware_version" in result):
                self.firmware = str((result or {}).get("firmware_version") or self.firmware)

    def _apply(self, st: dict):
        n = self.n
        ps, ms = st.get("print_status") or {}, st.get("machine_status") or {}
        if ps or ms:
            text = str(ps.get("state") or "").lower()
            if any(w in text for w in ("pause",)):
                n["state"] = "paused"
            elif any(w in text for w in ("complet", "finish", "done")):
                n["state"] = "complete"
            elif any(w in text for w in ("stop", "cancel")):
                n["state"] = "cancelled"
            elif any(w in text for w in ("error", "fail")):
                n["state"] = "error"
            elif text:
                n["state"] = "printing"
            else:
                n["state"] = "standby"
            n["filename"] = ps.get("filename") or n.get("filename") or ""
            n["layer"] = ps.get("current_layer")
            n["layers"] = ps.get("total_layer") or n.get("layers")
            n["elapsed"] = _num(ps.get("print_duration"))
            n["remaining"] = _num(ps.get("remaining_time_sec"))
            if ms.get("progress") is not None:
                n["progress"] = _num(ms.get("progress")) / 100
        ex, bed = st.get("extruder"), st.get("heater_bed")
        if isinstance(ex, dict):
            n["nozzle"] = (_num(ex.get("temperature")), _num(ex.get("target")))
        if isinstance(bed, dict):
            n["bed"] = (_num(bed.get("temperature")), _num(bed.get("target")))
        fans = st.get("fans")
        if isinstance(fans, dict):
            n["fans"] = {"ModelFan": _unfraction((fans.get("fan") or {}).get("speed")),
                         "AuxiliaryFan": _unfraction((fans.get("aux_fan") or {}).get("speed")),
                         "BoxFan": _unfraction((fans.get("box_fan") or {}).get("speed"))}
        led = st.get("led")
        if isinstance(led, dict) and "status" in led:
            n["light"] = bool(led["status"])
        gm = st.get("gcode_move")
        if isinstance(gm, dict) and gm.get("speed_mode") is not None:
            n["speed"] = SPEED_MODES.get(int(_num(gm["speed_mode"])), 100)
        self._track_job()
        self._changed()

    # ------------------------------------------------------------ commands

    async def pause(self):
        await self._call(1021)

    async def resume(self):
        await self._call(1023)

    async def stop(self):
        await self._call(1022)

    async def light(self, on: bool):
        await self._call(1029, {"power": 1 if on else 0})

    async def speed(self, percent: float):
        mode = min(SPEED_MODES, key=lambda k: abs(SPEED_MODES[k] - percent))
        await self._call(1031, {"mode": mode})

    async def _set_temps(self, nozzle: float | None = None, bed: float | None = None):
        params = {}
        if nozzle is not None:
            params["extruder"] = int(nozzle)
        if bed is not None:
            params["heater_bed"] = int(bed)
        await self._call(1028, params)

    async def _set_fan(self, fan: str, fraction: float):
        key = {"ModelFan": "fan", "AuxiliaryFan": "aux_fan", "BoxFan": "box_fan"}[fan]
        await self._call(1030, {key: int(round(fraction * 255))})

    async def start(self, path: str, plate: int = 1, filaments: list[dict] | None = None):
        await self._call(1020, {"filename": path.lstrip("/"), "storage_media": "local"})

    async def file_list(self) -> list[dict]:
        out, offset = [], 0
        while True:
            res = await self._call(1044, {"storage_media": "local", "offset": offset, "limit": 50})
            files = res.get("file_list") or res.get("files") or []
            for f in files:
                name = str(f.get("filename") or f.get("name") or "")
                if name:
                    out.append({"name": name.split("/")[-1], "size": int(_num(f.get("size") or f.get("file_size"))),
                                "modified": _num(f.get("create_time") or f.get("modified")) or time.time()})
            offset += 50
            if len(files) < 50 or offset >= 500:
                return out

    async def file_delete(self, path: str):
        await self._call(1047, {"storage_media": "local", "file_path": [path]})

    async def file_read(self, path: str) -> bytes:
        return await asyncio.to_thread(self._get, f"http://{self.host}:{self.http_port}/download/local/{path}?X-Token={self.code}")

    @staticmethod
    def _get(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()

    async def file_write(self, path: str, data: bytes):
        await asyncio.to_thread(self._upload, path, data)

    def _upload(self, path: str, data: bytes):
        md5, total, name = hashlib.md5(data).hexdigest(), len(data), path.rsplit("/", 1)[-1]
        for off in range(0, max(total, 1), UPLOAD_CHUNK):
            chunk = data[off:off + UPLOAD_CHUNK]
            req = urllib.request.Request(
                f"http://{self.host}:{self.http_port}/upload", data=chunk, method="PUT",
                headers={"Content-Type": "application/octet-stream",
                         "Content-Range": f"bytes {off}-{off + len(chunk) - 1}/{total}",
                         "X-File-Name": name, "X-File-MD5": md5, "X-Token": self.code})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            if body:
                with contextlib.suppress(ValueError):
                    if int(_num(json.loads(body).get("error_code"))):
                        raise RuntimeError("the printer refused the upload")


def make(cfg: dict) -> Elegoo:
    return CC2(cfg) if str(cfg.get("model", "")).lower() in ("cc2", "centauri carbon 2") else CC1(cfg)
