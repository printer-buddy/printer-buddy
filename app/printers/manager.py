from __future__ import annotations

import asyncio
import json
import logging
import os

from .moonraker import MoonrakerClient
from ..ai.detector import draw_box
from ..core.net import http_get
from ..core.notify import Notifier
from ..core.store import Store

log = logging.getLogger(__name__)
OFFLINE_GRACE = float(os.getenv("OFFLINE_GRACE", "60"))


def _event_for(prev: str | None, new: str | None) -> str | None:
    if prev is None or new is None or prev == new:
        return None
    if new == "printing":
        return "resumed" if prev == "paused" else "started"
    return {"paused": "paused", "complete": "finished", "error": "failed", "cancelled": "cancelled"}.get(new)


class Manager:
    def __init__(self, store: Store, notifier: Notifier):
        self.store = store
        self.notifier = notifier
        self.clients: dict[int, MoonrakerClient] = {}
        self.rows: dict[int, dict] = {}
        self._last: dict[int, dict] = {}
        self._offline_timers: dict[int, asyncio.Task] = {}
        self._milestone: dict[int, int] = {}
        self.timelapse = None
        self.ai = None
        self.collect = None
        self._subs: set[asyncio.Queue] = set()

    async def start(self):
        for row in self.store.list():
            self._spawn(row)

    async def stop(self):
        for t in self._offline_timers.values():
            t.cancel()
        await asyncio.gather(*(c.stop() for c in self.clients.values()))

    def _spawn(self, row: dict):
        self.rows[row["id"]] = row
        c = MoonrakerClient(row["id"], row["host"], row["port"], row["api_key"], self._on_change)
        c.on_gcode = self._on_gcode
        self.clients[row["id"]] = c
        c.start()

    def add(self, name: str, host: str, port: int, api_key: str | None) -> dict:
        row = self.store.add(name, host, port, api_key)
        self._spawn(row)
        return self.view(row["id"])

    def rename(self, pid: int, name: str):
        self.store.update(pid, name=name)
        self.rows[pid]["name"] = name
        self._broadcast("printer", self.view(pid))

    def set_group(self, pid: int, group: str | None):
        self.store.update(pid, grp=group)
        self.rows[pid]["grp"] = group
        self._broadcast("printer", self.view(pid))

    @staticmethod
    def _cam_settings(row: dict) -> dict | None:
        if not row.get("cam"):
            return None
        cfg = json.loads(row["cam"])
        if "cams" not in cfg:
            cfg = {"main": 0, "cams": {"0": cfg}}
        return cfg

    def set_camera(self, pid: int, cfg: dict | None):
        value = json.dumps(cfg) if cfg else None
        self.store.update(pid, cam=value)
        self.rows[pid]["cam"] = value
        self._broadcast("printer", self.view(pid))

    def set_notify(self, pid: int, on: bool):
        self.store.update(pid, notify=1 if on else 0)
        self.rows[pid]["notify"] = 1 if on else 0
        self._broadcast("printer", self.view(pid))

    def ai_mode(self, pid: int) -> str:
        from ..ai.detector import DEFAULT_MODE, MODES
        m = (self.rows.get(pid) or {}).get("ai")
        return m if m in MODES else DEFAULT_MODE

    def set_ai_mode(self, pid: int, mode: str):
        self.store.update(pid, ai=mode)
        self.rows[pid]["ai"] = mode
        if self.ai:
            self.ai.refresh(pid, self.view(pid))
        self._broadcast("printer", self.view(pid))

    def set_order(self, ids: list[int]):
        ids = [i for i in ids if i in self.rows]
        self.store.set_order(ids)
        for pos, pid in enumerate(ids):
            self.rows[pid]["position"] = pos
            self._broadcast("printer", self.view(pid))

    async def remove(self, pid: int):
        c = self.clients.pop(pid, None)
        self.rows.pop(pid, None)
        self._last.pop(pid, None)
        if self.timelapse:
            self.timelapse.cancel(pid)
        if self.ai:
            self.ai.cancel(pid)
        if self.collect:
            self.collect.marks.pop(pid, None)
            self.collect.job.pop(pid, None)
        if t := self._offline_timers.pop(pid, None):
            t.cancel()
        self.store.delete(pid)
        if c:
            await c.stop()
        self._broadcast("removed", {"id": pid})

    def view(self, pid: int) -> dict:
        row = self.rows[pid]
        return {
            "id": pid, "name": row["name"], "host": row["host"], "port": row["port"],
            "has_api_key": bool(row["api_key"]), "group": row.get("grp"), "position": row.get("position"),
            "cam_settings": self._cam_settings(row), "notify": row.get("notify") != 0,
            "recording": self.timelapse.recording(pid) if self.timelapse else None,
            "ai": self.ai.status(pid) if self.ai else None,
            **self.clients[pid].view(),
        }

    def views(self) -> list[dict]:
        return [self.view(pid) for pid in self.clients]

    def _on_change(self, client: MoonrakerClient):
        pid = client.id
        if pid not in self.rows:
            return
        v = self.view(pid)
        prev = self._last.get(pid)
        self._last[pid] = v
        if prev:
            self._detect(pid, prev, v)
        self._broadcast("printer", v)

    def _detect(self, pid: int, prev: dict, v: dict):
        if event := _event_for(prev["state"], v["state"]):
            self._emit(pid, event, v)
            if event == "started":
                self._milestone[pid] = 0
        if prev["state"] in ("printing", "paused") and v["state"] in ("complete", "cancelled", "error", "standby"):
            self.store.end_print_shares(pid)
        if self.timelapse:
            self.timelapse.on_change(pid, prev, v)
        if self.ai:
            self.ai.on_change(pid, prev, v)
        if self.collect:
            self._collect_on_change(pid, prev, v)
        step = self.notifier.progress_step()
        if step and v["state"] == "printing" and prev["state"] in ("printing", "paused"):
            reached = int(v["progress"] * 100) // step * step
            last = self._milestone.setdefault(pid, reached)
            if step <= reached < 100 and reached > last:
                self._milestone[pid] = reached
                self._emit(pid, "progress", {**v, "milestone": reached})
        # Klipper shut down / errored (MCU shutdown, thermal runaway, ...): wait a moment for the reason
        if v["connection"] == "online" and prev["klippy_state"] == "ready" and v["klippy_state"] in ("shutdown", "error"):
            asyncio.get_running_loop().call_later(
                2, lambda: self._emit(pid, "shutdown", self.view(pid)) if pid in self.rows else None)
        # lost the printer mid-print: alert only if it stays gone for OFFLINE_GRACE seconds
        if prev["connection"] == "online" and v["connection"] != "online" and prev["state"] in ("printing", "paused"):
            if pid not in self._offline_timers:
                self._offline_timers[pid] = asyncio.create_task(self._offline_check(pid, prev))
        elif v["connection"] == "online" and (t := self._offline_timers.pop(pid, None)):
            t.cancel()

    async def _offline_check(self, pid: int, before: dict):
        try:
            await asyncio.sleep(OFFLINE_GRACE)
            c = self.clients.get(pid)
            if c and c.connection != "online":
                self._emit(pid, "offline", before)
        finally:
            self._offline_timers.pop(pid, None)

    def _emit(self, pid: int, event: str, v: dict):
        self._broadcast("event", {"id": pid, "event": event, "name": v["name"]})
        if self.rows.get(pid, {}).get("notify") == 0:
            return
        if self.notifier.enabled and self.notifier.wants(event):
            asyncio.get_running_loop().create_task(self._notify(pid, event, v))

    def _ai_meta(self, pid: int) -> dict:
        st = (self.ai.state.get(pid) if self.ai else None) or {}
        return {"model": self.ai.model.tag if self.ai and self.ai.model else None,
                "score": st.get("score"), "top": st.get("top"), "scores": st.get("scores"),
                "threshold": self.ai.settings()["ai_threshold"] if self.ai else None}

    def _collect_on_change(self, pid: int, prev: dict, v: dict):
        event = _event_for(prev["state"], v["state"])
        if event == "started":
            self.collect.print_started(pid)
        elif event == "finished":
            self.collect.print_finished(pid, ok=True)
        elif event in ("failed", "cancelled"):
            self.collect.print_finished(pid, ok=False)
            asyncio.get_running_loop().create_task(self._collect_snapshot(pid, "print_failed", "pending"))
        if v["state"] == "printing" and self.collect.wants_print_snapshot(pid, v.get("progress") or 0):
            asyncio.get_running_loop().create_task(self._collect_snapshot(pid, "print", "waiting"))

    async def _collect_snapshot(self, pid: int, source: str, status: str):
        img = await self.snapshot(pid)
        row = self.rows.get(pid)
        if not img or not row:
            return
        if status == "waiting":
            self.collect.add_print_snapshot(pid, row["name"], img, self._ai_meta(pid))
        else:
            self.collect.add(pid, row["name"], img, source, status, meta=self._ai_meta(pid))

    async def save_for_ai(self, pid: int, label: str) -> dict | None:
        img = await self.snapshot(pid)
        row = self.rows.get(pid)
        if not img or not row or not self.collect:
            return None
        return self.collect.add(pid, row["name"], img, "user", "ready", label=label, meta=self._ai_meta(pid))

    def ai_update(self, pid: int):
        if pid in self.rows:
            self._broadcast("printer", self.view(pid))

    async def ai_alert(self, pid: int, label: str, score: float, image: bytes | None, mode: str,
                       box: list | None = None):
        c, row = self.clients.get(pid), self.rows.get(pid)
        if not c or not row:
            return
        paused = False
        if mode == "pause" and c.view().get("state") == "printing":
            try:
                await c.command("pause")
                paused = True
            except Exception as e:
                log.warning("AI: couldn't pause printer %s: %s", pid, e)
        nice = label.replace("_", " ")
        if self.collect and image:
            item = self.collect.add(pid, row["name"], image, "alert", "pending", suggested=label,
                                    meta=self._ai_meta(pid))
            if item and self.ai:
                self.ai.state.setdefault(pid, {})["alert_item"] = item["id"]
        self._broadcast("event", {"id": pid, "event": "ai_failure", "name": row["name"],
                                  "label": label, "score": score, "paused": paused})
        if row.get("notify") == 0 or not self.notifier.enabled:
            return
        v = self.view(pid)
        title = f"{row['name']}: possible {nice} ({score * 100:.0f}%)"
        parts = [v.get("filename") or "unknown file", f"{v['progress'] * 100:.0f}% done",
                 "print paused - check it and resume or cancel" if paused else "check the print"]
        if image and box:
            try:
                image = draw_box(image, box, f"{nice} {score * 100:.0f}%")
            except Exception as e:
                log.info("AI: couldn't draw the box for printer %s: %s", pid, e)
        await self.notifier.send(title, " · ".join(parts), image)

    def _on_gcode(self, client: MoonrakerClient, line: str):
        if client.id in self.rows:
            self._broadcast("gcode", {"id": client.id, "line": line})

    async def _notify(self, pid: int, event: str, v: dict):
        name, file = v["name"], v.get("filename") or "unknown file"
        if event == "offline":
            title, parts = f"{name}: went offline mid-print", [file, f"{v['progress'] * 100:.0f}% done"]
        elif event == "progress":
            title, parts = f"{name}: {v['milestone']}% done", [file]
            if v.get("eta"):
                m = int(v["eta"] // 60)
                parts.append(f"{m // 60}h {m % 60}m left" if m >= 60 else f"{m}m left")
        elif event == "shutdown":
            title, parts = f"{name}: Klipper {v['klippy_state']}", [v.get("klippy_message") or "no reason given"]
        else:
            title, parts = f"{name}: print {event}", [file]
            if event == "failed" and v.get("message"):
                parts.append(v["message"])
            if event in ("paused", "cancelled", "failed"):
                parts.append(f"{v['progress'] * 100:.0f}% done")
            if event == "finished" and v.get("print_duration"):
                m = int(v["print_duration"] // 60)
                parts.append(f"took {m // 60}h {m % 60}m" if m >= 60 else f"took {m}m")
        image = await self.snapshot(pid) if event != "offline" else None
        await self.notifier.send(title, " · ".join(parts), image)

    def cam_transform(self, pid: int) -> tuple[bool, bool, int]:
        row, c = self.rows.get(pid), self.clients.get(pid)
        if not row or not c:
            return False, False, 0
        cs = self._cam_settings(row) or {}
        main = cs.get("main", 0)
        own = (cs.get("cams") or {}).get(str(main), {})
        cams = c.cams_info()
        mr = cams[min(main, len(cams) - 1)] if cams else {}
        pick = lambda k: own[k] if k in own else mr.get(k, 0 if k == "rotation" else False)
        return bool(pick("flip_h")), bool(pick("flip_v")), int(pick("rotation") or 0)

    async def snapshot(self, pid: int) -> bytes | None:
        c, row = self.clients.get(pid), self.rows.get(pid)
        if not c or not row:
            return None
        cam = (self._cam_settings(row) or {}).get("main", 0)
        url = c.snapshot_url(cam)
        if not url or not url.startswith("http://"):
            return None
        try:
            status, _, data = await http_get(url, timeout=6)
            return data if status == 200 and data else None
        except Exception as e:
            log.info("snapshot for printer %s failed: %s", pid, e)
            return None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)

    def _broadcast(self, kind: str, data: dict):
        for q in list(self._subs):
            try:
                q.put_nowait((kind, data))
            except asyncio.QueueFull:
                pass
