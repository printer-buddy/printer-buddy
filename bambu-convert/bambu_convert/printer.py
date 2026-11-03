import asyncio
import json
import logging
import re
import time

from .camera import Camera
from .ftps import Files
from .mqtt import MQTT

log = logging.getLogger("bambu.printer")

# printer_type -> (name, bed x, bed y, max z, nozzles, chamber sensor, aux fan, chamber fan, bed max temp)
# TODO: bed sizes of X2D / A2L / H2C / H2S need checking
MODELS = {
    "BL-P001": ("X1 Carbon", 256, 256, 256, 1, True, True, True, 100),
    "BL-P002": ("X1", 256, 256, 256, 1, True, True, True, 100),
    "C13": ("X1E", 256, 256, 256, 1, True, True, True, 110),
    "C11": ("P1P", 256, 256, 256, 1, False, True, True, 100),
    "C12": ("P1S", 256, 256, 256, 1, False, True, True, 100),
    "N1": ("A1 mini", 180, 180, 180, 1, False, False, False, 80),
    "N2S": ("A1", 256, 256, 256, 1, False, False, False, 100),
    "N6": ("X2D", 256, 256, 256, 2, True, True, True, 100),
    "N7": ("P2S", 256, 256, 256, 1, True, True, True, 100),
    "N9": ("A2L", 256, 256, 256, 1, False, False, False, 80),
    "O1C": ("H2C", 325, 320, 325, 2, True, True, True, 100),
    "O1C2": ("H2C", 325, 320, 325, 2, True, True, True, 100),
    "O1D": ("H2D", 325, 320, 325, 2, True, True, True, 100),
    "O1E": ("H2D Pro", 325, 320, 325, 2, True, True, True, 100),
    "O1S": ("H2S", 340, 320, 340, 1, True, True, True, 100),
}
SERIALS = {"00M": "BL-P001", "00W": "BL-P002", "03W": "C13", "01S": "C11", "01P": "C12", "030": "N1", "039": "N2S",
           "094": "O1D"}
UNKNOWN = ("Bambu Lab", 256, 256, 256, 1, True, True, True, 100)
STATES = {"IDLE": "standby", "PREPARE": "printing", "SLICING": "printing", "RUNNING": "printing",
          "PAUSE": "paused", "FINISH": "complete", "FAILED": "error"}
CANCELLED = 50348044
SPEEDS = {1: 50, 2: 100, 3: 124, 4: 166}
FANS = {"fan": ("cooling_fan_speed", 1), "fan_generic Aux": ("big_fan1_speed", 2),
        "fan_generic Chamber": ("big_fan2_speed", 3), "heater_fan Hotend": ("heatbreak_fan_speed", None)}


def _merge(dst: dict, src: dict):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Bambu:
    def __init__(self, cfg: dict):
        self.name = cfg.get("name") or "Bambu"
        self.host, self.serial, self.code = cfg["host"], cfg["serial"].strip().upper(), str(cfg["access_code"]).strip()
        self.report: dict = {}
        self.connected = False
        self.error: str | None = None
        self.camera = Camera(self.host, self.code)
        if cfg.get("camera") == "rtsp":
            self.camera.rtsp_url = f"rtsps://{self.host}:322/streaming/live/1"
        self._camera_mode = cfg.get("camera", "auto")
        self.files = Files(self.host, self.code)
        self.listeners: list = []
        self.gcode_listeners: list = []
        self._mqtt: MQTT | None = None
        self._seq = 0
        self._job_started: float | None = None
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._last_state = "standby"
        self.job_ended: tuple | None = None
        self._ended: str | None = None

    @property
    def spec(self) -> tuple:
        return MODELS.get(str(self.report.get("printer_type") or SERIALS.get(self.serial[:3], "")), UNKNOWN)

    @property
    def model(self) -> str:
        return self.spec[0]

    @property
    def new_protocol(self) -> bool:
        return bool(((self.report.get("device") or {}).get("extruder") or {}).get("info"))

    def nozzles(self) -> list[tuple[float, float]]:
        """[(temperature, target)] per nozzle; newer printers pack each as target << 16 | current."""
        info = ((self.report.get("device") or {}).get("extruder") or {}).get("info")
        if info:
            out = []
            for n in sorted(info, key=lambda n: n.get("id", 0)):
                t = int(_num(n.get("temp")))
                out.append((float(t & 0xFFFF), float(t >> 16)))
            return out
        return [(_num(self.report.get("nozzle_temper")), _num(self.report.get("nozzle_target_temper")))]

    def active_nozzle(self) -> int:
        state = ((self.report.get("device") or {}).get("extruder") or {}).get("state")
        return (int(_num(state)) >> 4) & 15 if state is not None else 0

    def trays(self) -> list[dict]:
        """AMS slots in unit order; "tray" is the printer's own slot number (unit * 4 + slot, AMS HT: its unit id)."""
        out = []
        for u, unit in enumerate((self.report.get("ams") or {}).get("ams") or []):
            uid = int(_num(unit.get("id"), u))
            for t in unit.get("tray") or []:
                sid = int(_num(t.get("id")))
                color = str(t.get("tray_color") or "")[:6].lower()
                out.append({"unit": u, "unit_id": uid, "slot": sid, "tray": uid * 4 + sid if uid < 128 else uid,
                            "type": str(t.get("tray_type") or ""), "name": str(t.get("tray_sub_brands") or ""),
                            "color": color, "temp": int(_num(t.get("nozzle_temp_max"))),
                            "remain": int(_num(t.get("remain"), -1))})
        return out

    def ams_units(self) -> list[dict]:
        return list((self.report.get("ams") or {}).get("ams") or [])

    def chamber(self) -> float | None:
        ctc = (((self.report.get("device") or {}).get("ctc") or {}).get("info") or {}).get("temp")
        if ctc is not None:
            return float(int(_num(ctc)) & 0xFFFF)
        return _num(self.report.get("chamber_temper")) if self.spec[5] and "chamber_temper" in self.report else None

    # -------------------------------------------------------------- connection

    async def run(self):
        backoff = 2
        while True:
            m = MQTT(self.host, "bblp", self.code, f"printer-buddy-{self.serial[-6:]}-{int(time.time()) % 10000}")
            try:
                await m.connect()
                await m.subscribe(f"device/{self.serial}/report")
                self._mqtt, self.connected, self.error, backoff = m, True, None, 2
                log.info("%s: connected to %s", self.name, self.host)
                await self._send({"pushing": {"command": "pushall"}})
                await self._send({"info": {"command": "get_version"}})
                self._changed()
                pinger = asyncio.create_task(self._pinger(m))
                try:
                    async for topic, payload in m.messages():
                        self._on_message(payload)
                finally:
                    pinger.cancel()
            except asyncio.CancelledError:
                await m.close()
                raise
            except Exception as e:
                self.error = str(e) or type(e).__name__
                log.info("%s: %s", self.name, self.error)
            self._mqtt, self.connected = None, False
            await m.close()
            self._changed()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _pinger(self, m: MQTT):
        n = 0
        while True:
            await asyncio.sleep(30)
            await m.ping()
            n += 1
            # P1 / A1 only send what changed; ask for everything now and then
            if n % 20 == 0:
                await self._send({"pushing": {"command": "pushall"}})

    async def _send(self, msg: dict):
        if not self._mqtt:
            raise ConnectionError("printer not connected")
        self._seq += 1
        for section in msg.values():
            section.setdefault("sequence_id", str(self._seq))
        await self._mqtt.publish(f"device/{self.serial}/request", json.dumps(msg).encode())

    def _on_message(self, payload: bytes):
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        p = msg.get("print")
        if isinstance(p, dict):
            if p.get("result") in ("fail", "failed") and p.get("command") != "push_status":
                self._gcode(f"!! Printer refused {p.get('command')}: {p.get('reason')}" if p.get("reason") else
                            f"!! Printer refused {p.get('command')} (turn on LAN only + Developer mode on the printer)")
            if p.get("command") in (None, "push_status"):
                _merge(self.report, p)
                ipcam = (self.report.get("ipcam") or {}).get("rtsp_url") or ""
                if self._camera_mode == "auto":
                    self.camera.rtsp_url = ipcam if ipcam.startswith("rtsp") else None
                self._track_job()
                self._changed()
        info = msg.get("info")
        if isinstance(info, dict) and info.get("command") == "get_version":
            self.report["_modules"] = info.get("module") or []
            self._changed()

    def _changed(self):
        for cb in list(self.listeners):
            cb()

    def _gcode(self, line: str):
        for cb in list(self.gcode_listeners):
            cb(line)

    # -------------------------------------------------------------- job tracking

    def state(self) -> str:
        s = STATES.get(str(self.report.get("gcode_state", "IDLE")).upper(), "standby")
        if s == "error" and int(_num(self.report.get("print_error"))) == CANCELLED:
            return "cancelled"
        # a stopped print drops straight to IDLE; Klipper keeps showing the end state until the next print
        return self._ended if s == "standby" and self._ended else s

    def _track_job(self):
        s, now = self.state(), time.time()
        if s in ("printing", "paused"):
            self._ended = None
        if s in ("printing", "paused") and self._job_started is None:
            elapsed = 0.0
            remaining = _num(self.report.get("mc_remaining_time")) * 60
            pct = _num(self.report.get("mc_percent")) / 100
            if 0 < pct < 1 and remaining:
                elapsed = remaining * pct / (1 - pct)
            self._job_started, self._paused_total = now - elapsed, 0.0
        if s == "paused" and self._paused_at is None:
            self._paused_at = now
        if s != "paused" and self._paused_at is not None:
            self._paused_total += now - self._paused_at
            self._paused_at = None
        if s not in ("printing", "paused") and self._last_state in ("printing", "paused"):
            end = "cancelled" if s == "standby" else s
            self.job_ended = (self.filename(), self.print_duration(), end)
            self._ended = end
            self._job_started = None
        self._last_state = s

    def job_start(self) -> float | None:
        return self._job_started

    def print_duration(self) -> float:
        if self._job_started is None:
            return 0.0
        end = self._paused_at or time.time()
        return max(0.0, end - self._job_started - self._paused_total)

    def total_duration(self) -> float:
        return 0.0 if self._job_started is None else time.time() - self._job_started

    def filename(self) -> str:
        r = self.report
        name = r.get("gcode_file") or r.get("subtask_name") or ""
        return re.sub(r"^(/sdcard/|/mnt/sdcard/|file:///sdcard/|ftp://)", "", str(name)).lstrip("/")

    # -------------------------------------------------------------- klipper objects

    def objects(self) -> dict:
        r, s = self.report, self.state()
        pct = _num(r.get("mc_percent")) / 100
        printing = s in ("printing", "paused")
        bed, bed_t = _num(r.get("bed_temper")), _num(r.get("bed_target_temper"))
        name, x, y, z, _, _, aux, chamber_fan, _ = self.spec
        nozzles = self.nozzles()
        names = ["extruder"] + [f"extruder{i}" for i in range(1, len(nozzles))]
        chamber = self.chamber()
        speed = SPEEDS.get(int(_num(r.get("spd_lvl"), 2)), 100) / 100
        fw = next((m.get("sw_ver") for m in r.get("_modules", []) if m.get("name") == "ota"), "") or ""
        layer, layers = int(_num(r.get("layer_num"))), int(_num(r.get("total_layer_num")))
        message = ""
        if s == "error":
            message = f"print failed (error {int(_num(r.get('print_error'))):08X})"
        heaters = names + ["heater_bed"]
        sensors = heaters + (["temperature_sensor Chamber"] if chamber is not None else [])
        o = {
            "webhooks": {"state": "ready" if self.connected else "shutdown",
                         "state_message": "Printer is ready" if self.connected else f"Bambu printer not reachable: {self.error}"},
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
            "heater_bed": {"temperature": bed, "target": bed_t, "power": 1.0 if bed_t > bed + 2 else 0.0},
            "heaters": {"available_heaters": heaters, "available_sensors": sensors, "available_monitors": []},
            "gcode_move": {"speed_factor": speed, "extrude_factor": 1.0, "homing_origin": [0.0, 0.0, 0.0, 0.0],
                           "position": [0.0, 0.0, 0.0, 0.0], "gcode_position": [0.0, 0.0, 0.0, 0.0],
                           "absolute_coordinates": True, "absolute_extrude": True, "speed": 0.0},
            "toolhead": {"homed_axes": "", "position": [0.0, 0.0, 0.0, 0.0], "axis_minimum": [0.0, 0.0, 0.0, 0.0],
                         "axis_maximum": [float(x), float(y), float(z), 0.0],
                         "extruder": names[min(self.active_nozzle(), len(names) - 1)],
                         "max_velocity": 500.0, "max_accel": 20000.0, "square_corner_velocity": 5.0,
                         "minimum_cruise_ratio": 0.5, "max_accel_to_decel": 10000.0, "print_time": 0.0,
                         "estimated_print_time": 0.0, "stalls": 0},
            "motion_report": {"live_position": [0.0, 0.0, 0.0, 0.0], "live_velocity": 0.0, "live_extruder_velocity": 0.0},
            "exclude_object": {"objects": [], "excluded_objects": [], "current_object": None},
            "system_stats": {"sysload": 0.0, "cputime": 0.0, "memavail": 0},
            "mcu": {"mcu_version": fw, "mcu_build_versions": "", "mcu_constants": {}, "last_stats": {}},
            "output_pin Chamber_light": {"value": 1.0 if any(
                l.get("node") == "chamber_light" and l.get("mode") == "on" for l in r.get("lights_report") or []) else 0.0},
            "configfile": {"warnings": [], "save_config_pending": False, "save_config_pending_items": {},
                           "settings": self._settings(), "config": {}},
        }
        for n, (temp, target) in zip(names, nozzles):
            o[n] = {"temperature": temp, "target": target, "power": 1.0 if target > temp + 2 else 0.0,
                    "can_extrude": temp >= 170, "pressure_advance": 0.0, "smooth_time": 0.04}
        if chamber is not None:
            o["temperature_sensor Chamber"] = {"temperature": chamber, "measured_min_temp": chamber,
                                               "measured_max_temp": chamber}
        gear = int(_num(r.get("fan_gear"), -1))
        o.update(self._mmu(s))
        for fan, (field, idx) in FANS.items():
            if (idx == 2 and not aux) or (idx == 3 and not chamber_fan):
                continue
            if gear >= 0 and idx:
                o[fan] = {"speed": round(((gear >> (8 * (idx - 1))) & 255) / 255, 2), "rpm": None}
            elif field in r:
                o[fan] = {"speed": round(_num(r.get(field)) / 15, 2), "rpm": None}
        return o

    def _mmu(self, state: str) -> dict:
        """The AMS as a Happy Hare MMU, which Fluidd and Mainsail already show."""
        trays, units = self.trays(), self.ams_units()
        if not trays:
            return {}
        out = {}
        now = int(_num((self.report.get("ams") or {}).get("tray_now"), 255))
        gate = next((g for g, t in enumerate(trays) if t["tray"] == now), -2 if now == 254 else -1)
        lite = self.model.startswith(("A1", "A2"))
        machine = {"num_units": len(units)}
        first = 0
        for u, unit in enumerate(units):
            uid = int(_num(unit.get("id"), u))
            count = sum(1 for t in trays if t["unit"] == u)
            sensor = None
            if unit.get("humidity_raw") is not None or unit.get("temp") is not None:
                sensor = f"temperature_sensor AMS_{u + 1}"
                out[sensor] = {"temperature": _num(unit.get("temp")),
                               "humidity": _num(unit.get("humidity_raw"), _num(unit.get("humidity")))}
            machine[f"unit_{u}"] = {
                "name": "AMS HT" if uid >= 128 else "AMS lite" if lite else f"AMS {u + 1}",
                "vendor": "HappyHare", "version": "1.0", "num_gates": count, "first_gate": first,
                "selector_type": "VirtualSelector", "variable_rotation_distances": False,
                "variable_bowden_lengths": False, "require_bowden_move": False, "filament_always_gripped": True,
                "can_crossload": False, "has_bypass": False, "multi_gear": True,
                **({"environment_sensor": sensor} if sensor else {})}
            first += count
        n = len(trays)
        print_state = {"printing": "printing", "paused": "paused", "complete": "complete",
                       "cancelled": "cancelled", "error": "error"}.get(state, "ready")
        loaded = gate >= 0
        out["mmu_machine"] = machine
        out["mmu"] = {
            "enabled": True, "num_gates": n, "is_homed": True, "is_locked": False, "is_paused": state == "paused",
            "is_in_print": state in ("printing", "paused"), "print_state": print_state,
            "unit": trays[gate]["unit"] if loaded else -1, "tool": gate, "gate": gate,
            "active_filament": {"filament_name": (trays[gate]["name"] or trays[gate]["type"]) if loaded else ""},
            "num_toolchanges": 0, "last_tool": -1, "next_tool": -1, "toolchange_purge_volume": 0,
            "last_toolchange": "", "filament": "Loaded" if gate != -1 else "Unloaded", "filament_position": 0,
            "filament_pos": 10 if gate != -1 else 0, "filament_direction": 0, "action": "Idle",
            "has_bypass": True, "sync_drive": False, "servo": "Unknown", "grip": "Unknown",
            "spoolman_support": "off", "sensors": {}, "endless_spool_enabled": 0, "clog_detection_enabled": 0,
            "gate_status": [1 if t["type"] else 0 for t in trays],
            "gate_filament_name": [t["name"] or t["type"] for t in trays],
            "gate_material": [t["type"] for t in trays],
            "gate_color": [t["color"] if t["type"] else "" for t in trays],
            "gate_color_rgb": [[int(t["color"][i:i + 2] or "0", 16) / 255 for i in (0, 2, 4)]
                               if len(t["color"]) == 6 else [0.0, 0.0, 0.0] for t in trays],
            "gate_temperature": [t["temp"] for t in trays], "gate_spool_id": [-1] * n,
            "gate_speed_override": [100] * n, "gate_remaining": [t["remain"] for t in trays],
            "tool_extrusion_multipliers": [1.0] * n, "tool_speed_multipliers": [1.0] * n,
            "ttg_map": list(range(n)), "endless_spool_groups": list(range(n)),
            "slicer_tool_map": {"tools": {}, "referenced_tools": [], "initial_tool": None, "purge_volumes": [],
                                "total_toolchanges": None, "skip_automap": False},
        }
        return out

    def ams_mapping(self, filaments: list[dict]) -> list[int]:
        """For each filament in the 3mf (slicer order) the AMS slot to use: same material, closest colour, -1 if unused."""
        trays = [t for t in self.trays() if t["type"]]
        if not trays:
            return []

        def rgb(c: str) -> tuple:
            c = (c or "").lstrip("#")[:6]
            try:
                return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return (128, 128, 128)

        mapping, taken = [], set()
        for f in filaments:
            if not f.get("used", True):
                mapping.append(-1)
                continue
            want, kind = rgb(f.get("color")), str(f.get("type") or "").upper()
            pool = [t for t in trays if t["type"].upper() == kind] or trays
            pool = [t for t in pool if t["tray"] not in taken] or pool
            best = min(pool, key=lambda t: sum((a - b) ** 2 for a, b in zip(rgb(t["color"]), want)))
            taken.add(best["tray"])
            mapping.append(best["tray"])
        return mapping

    def _settings(self) -> dict:
        name, x, y, z, count, _, _, _, bed_max = self.spec
        nozzle = {"min_temp": 0, "max_temp": 300, "min_extrude_temp": 170, "nozzle_diameter": 0.4,
                  "filament_diameter": 1.75}
        out = {
            "printer": {"kinematics": "cartesian" if name.startswith(("A1", "A2")) else "corexy",
                        "max_velocity": 500, "max_accel": 20000},
            "extruder": dict(nozzle),
            "heater_bed": {"min_temp": 0, "max_temp": bed_max},
            "stepper_x": {"position_min": 0, "position_max": x}, "stepper_y": {"position_min": 0, "position_max": y},
            "stepper_z": {"position_min": 0, "position_max": z},
            "virtual_sdcard": {"path": "/sdcard"}, "pause_resume": {}, "display_status": {},
            "output_pin chamber_light": {"pwm": False},
            "fan": {}, "fan_generic aux": {}, "fan_generic chamber": {}, "heater_fan hotend": {},
        }
        for i in range(1, max(count, len(self.nozzles()))):
            out[f"extruder{i}"] = dict(nozzle)
        return out

    # -------------------------------------------------------------- commands

    async def pause(self):
        await self._send({"print": {"command": "pause"}})

    async def resume(self):
        await self._send({"print": {"command": "resume"}})

    async def stop(self):
        await self._send({"print": {"command": "stop"}})

    async def light(self, on: bool):
        await self._send({"system": {"command": "ledctrl", "led_node": "chamber_light", "led_mode": "on" if on else "off",
                                     "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}})

    async def speed(self, percent: float):
        level = min(SPEEDS, key=lambda k: abs(SPEEDS[k] - percent))
        await self._send({"print": {"command": "print_speed", "param": str(level)}})

    async def ams_load(self, gate: int):
        trays = self.trays()
        if not 0 <= gate < len(trays):
            raise ValueError(f"no AMS slot {gate}")
        t = trays[gate]
        if not t["type"]:
            raise ValueError(f"AMS slot {gate} is empty")
        temp = t["temp"] or 220
        await self._send({"print": {"command": "ams_change_filament", "ams_id": t["unit_id"], "slot_id": t["slot"],
                                    "target": t["tray"], "curr_temp": temp, "tar_temp": temp}})

    async def ams_unload(self):
        trays = self.trays()
        if not trays:
            raise ValueError("no AMS")
        now = int(_num((self.report.get("ams") or {}).get("tray_now"), 255))
        t = next((t for t in trays if t["tray"] == now), trays[0])
        temp = t["temp"] or 220
        await self._send({"print": {"command": "ams_change_filament", "ams_id": t["unit_id"], "slot_id": 255,
                                    "target": 255, "curr_temp": temp, "tar_temp": temp}})

    async def nozzle_temp(self, index: int, target: float):
        if self.new_protocol:
            await self._send({"print": {"command": "set_nozzle_temp", "extruder_index": index,
                                        "target_temp": int(target)}})
        else:
            await self.gcode(f"M104 S{target:g}")

    async def gcode(self, line: str):
        await self._send({"print": {"command": "gcode_line", "param": line.rstrip("\n") + "\n"}})

    async def start(self, path: str, plate: int = 1, filaments: list[dict] | None = None):
        path = path.lstrip("/")
        if path.lower().endswith(".3mf"):
            # H2 printers take ftp:///, the rest file:///sdcard/ (same as Bambu Studio / ha-bambulab)
            h2 = str(self.report.get("printer_type", "")).startswith("O1") or self.serial.startswith("094")
            mapping = self.ams_mapping(filaments or [{}])
            await self._send({"print": {
                "command": "project_file", "param": f"Metadata/plate_{plate}.gcode", "project_id": "0",
                "profile_id": "0", "task_id": "0", "subtask_id": "0", "subtask_name": path.rsplit("/", 1)[-1],
                "url": f"ftp:///{path}" if h2 else f"file:///sdcard/{path}", "timelapse": False,
                "bed_type": "auto", "bed_leveling": True, "flow_cali": True, "vibration_cali": True,
                "layer_inspect": True, "use_ams": bool(mapping), "ams_mapping": mapping or [0]}})
        else:
            await self._send({"print": {"command": "gcode_file", "param": f"/sdcard/{path}"}})
