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
import base64
import io
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

LABELS = ("ok", "spaghetti", "warping", "first_layer", "failure", "stringing", "blob")
PRINT_MARKS = (0.10, 0.50, 0.90)
KEEP = 500
MAX_PHOTO = 1_000_000   # bytes; the relay refuses photos over 1 MB
SEND_EVERY = 60


def api_url() -> str:
    custom = os.getenv("PB_CONTRIBUTE_API", "").strip()
    if custom:
        return custom
    domain = os.getenv("PB_SHARE_DOMAIN", "").strip().strip("/") or "printerbuddy.stream"
    return f"https://{domain if domain.startswith('www.') else 'www.' + domain}/api/contribute"


def verdict(item: dict) -> str | None:
    label, meta = item.get("label"), item.get("meta") or {}
    if not label:
        return None
    if item.get("source") in ("alert", "alert_yes", "alert_no"):
        said_problem = True
    elif meta.get("score") is None or meta.get("threshold") is None:
        return None
    else:
        said_problem = float(meta["score"]) >= float(meta["threshold"])
    if label == "ok":
        return "false_alarm" if said_problem else "right"
    return "right" if said_problem else "missed"


class Collector:
    def __init__(self, store, data_dir: str, box_id: Callable[[], str], enabled: Callable[[], bool]):
        self.store = store
        self.root = Path(data_dir) / "ai" / "collect"
        self.box_id = box_id
        self.enabled = enabled
        self.marks: dict[int, set] = {}
        self.job: dict[int, list] = {}
        self._task: asyncio.Task | None = None
        self._pause_until = 0.0
        self._lock: asyncio.Lock | None = None
        self.last_error: str | None = None
        self.rejected: str | None = None   # why the last add() didn't keep its photo (too big)

    def sharing(self) -> bool:
        return bool(self.store.settings().get("ai_contribute", False))

    def set_sharing(self, on: bool):
        self.store.save_settings({"ai_contribute": bool(on)})

    def stats(self) -> dict:
        return self.store.settings().get("ai_stats", {})

    def _judge(self, item: dict):
        old, new = item.get("verdict"), verdict(item)
        item["verdict"] = new
        if old == new:
            return
        stats = self.stats()
        model = (item.get("meta") or {}).get("model") or "unknown"
        m = stats.setdefault(model, {"right": 0, "false_alarm": 0, "missed": 0})
        if old in m:
            m[old] = max(0, m[old] - 1)
        if new in m:
            m[new] += 1
        self.store.save_settings({"ai_stats": stats})

    def _meta_path(self, iid: str) -> Path:
        return self.root / f"{iid}.json"

    def image_path(self, iid: str) -> Path | None:
        if not iid.replace("-", "").isalnum():
            return None
        p = self.root / f"{iid}.jpg"
        return p if p.exists() else None

    def _read(self, iid: str) -> dict | None:
        try:
            return json.loads(self._meta_path(iid).read_text())
        except Exception:
            return None

    def _write(self, item: dict):
        self._meta_path(item["id"]).write_text(json.dumps(item))

    def items(self) -> list[dict]:
        out = []
        for p in self.root.glob("*.json"):
            try:
                out.append(json.loads(p.read_text()))
            except Exception:
                pass
        return sorted(out, key=lambda i: i.get("at", 0), reverse=True)

    def summary(self) -> dict:
        items = self.items()
        count = lambda st: sum(1 for i in items if i.get("status") == st)
        return {"sharing": self.sharing(), "pending": count("pending"), "ready": count("ready"),
                "sent": count("sent"), "error": self.last_error, "stats": self.stats(), "items": items[:200]}

    @staticmethod
    def _clean_jpeg(jpeg: bytes) -> bytes:
        from PIL import Image
        # full size, as the live check sees it (a 640 copy scored spaghetti far lower); re-saving drops the camera's extra data
        im = Image.open(io.BytesIO(jpeg)).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        if buf.tell() > MAX_PHOTO:
            # never queued: the relay would refuse it, holding up every photo after it
            raise ValueError(f"the camera picture is too big ({buf.tell() // 1000} KB, the limit is {MAX_PHOTO // 1000} KB). "
                             "Try turning down the camera's resolution.")
        return buf.getvalue()

    def add(self, pid: int, printer: str, jpeg: bytes, source: str, status: str,
            label: str | None = None, suggested: str | None = None, meta: dict | None = None) -> dict | None:
        self.rejected = None
        if not self.enabled() or not jpeg:
            return None
        if not self.sharing():
            self.rejected = "Turn on sending photos to the Printer Buddy project to save photos for the AI."
            return None
        try:
            data = self._clean_jpeg(jpeg)
        except Exception as e:
            log.info("collect: photo from printer %s not kept: %s", pid, e)
            self.rejected = f"Photo not kept: {e}"
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        iid = f"{int(time.time())}-{secrets.token_hex(3)}"
        (self.root / f"{iid}.jpg").write_bytes(data)
        item = {"id": iid, "pid": pid, "printer": printer, "at": time.time(), "source": source,
                "status": status, "label": label, "suggested": suggested, "meta": meta or {}}
        if label:
            self._judge(item)
        self._write(item)
        self.trim()
        if status == "ready":
            self.kick()
        return item

    def trim(self):
        keep = int(self.store.settings().get("ai_keep", KEEP))
        photos, records = [], []
        for it in self.items():
            (photos if (self.root / f"{it['id']}.jpg").exists() else records).append(it)
        for it in photos[keep:] + records[keep:]:
            self.delete(it["id"])

    def delete(self, iid: str) -> bool:
        ok = False
        for ext in (".jpg", ".json"):
            p = self.root / f"{iid}{ext}"
            if p.exists():
                p.unlink()
                ok = True
        return ok

    def set_label(self, iid: str, label: str) -> dict | None:
        item = self._read(iid)
        if item and label == "yes":
            label = item.get("suggested") or "failure"
        if not item or item.get("status") == "sent" or label not in LABELS:
            return None
        if item["source"] in ("alert", "alert_yes", "alert_no"):
            item["source"] = "alert_no" if label == "ok" else "alert_yes"
        item["label"], item["status"] = label, "ready"
        self._judge(item)
        self._write(item)
        self.kick()
        return item

    def set_severity(self, iid: str, severity) -> dict | None:
        item = self._read(iid)
        try:
            n = int(severity)
        except (TypeError, ValueError):
            return None
        if not item or item.get("status") == "sent" or not item.get("label") or item["label"] == "ok" or not 1 <= n <= 10:
            return None
        item["severity"] = n
        self._write(item)
        return item

    def print_started(self, pid: int):
        self.marks[pid] = set()
        self.job[pid] = []

    def wants_print_snapshot(self, pid: int, progress: float) -> float | None:
        if not self.enabled() or not self.sharing():
            return None
        taken = self.marks.setdefault(pid, set())
        for m in PRINT_MARKS:
            if progress >= m and m not in taken:
                taken.add(m)
                return m
        return None

    def add_print_snapshot(self, pid: int, printer: str, jpeg: bytes, meta: dict):
        item = self.add(pid, printer, jpeg, "print", "waiting", meta=meta)
        if item:
            self.job.setdefault(pid, []).append(item["id"])

    def print_finished(self, pid: int, ok: bool):
        for iid in self.job.pop(pid, []):
            item = self._read(iid)
            if not item or item.get("status") != "waiting":
                continue
            if ok:
                item.update(label="ok", status="ready", source="print_ok")
                self._judge(item)
            else:
                item.update(status="pending", source="print_failed")
            self._write(item)
        self.marks.pop(pid, None)
        if ok:
            self.kick()

    def start(self):
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(SEND_EVERY)
                if self.enabled() and self.sharing() and time.time() >= self._pause_until:
                    await self.send_ready()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("collect: upload round failed")

    def kick(self):
        try:
            asyncio.get_running_loop().create_task(self.send_now())
        except RuntimeError:
            pass

    async def send_now(self):
        if not (self.enabled() and self.sharing()) or time.time() < self._pause_until:
            return
        try:
            await self.send_ready()
        except Exception:
            log.exception("collect: sending now failed")

    async def send_ready(self, limit: int = 10):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._send_ready(limit)

    async def _send_ready(self, limit: int):
        ready = [i for i in self.items() if i.get("status") == "ready"][::-1][:limit]
        for item in ready:
            img = self.image_path(item["id"])
            if not img:
                continue
            body = json.dumps({
                "id": self.box_id(), "label": item["label"],
                "source": item["source"] if item["source"] != "alert" else "alert_yes",
                "image": base64.b64encode(img.read_bytes()).decode(),
                "verdict": item.get("verdict"), "severity": item.get("severity"),
                "meta": {**item.get("meta", {}), "printer": item.get("printer")},
            }).encode()
            status, reply = await asyncio.to_thread(self._post, body)
            if status == 201:
                item["status"], item["sent_at"] = "sent", time.time()
                self._write(item)
                img.unlink(missing_ok=True)
                self.trim()
                self.last_error = None
            elif status == 429:
                self._pause_until = time.time() + 3600
                self.last_error = "daily limit reached, sending the rest later"
                return
            else:
                self.last_error = reply or f"HTTP {status}"
                self._pause_until = time.time() + 600
                return

    @staticmethod
    def _post(body: bytes) -> tuple[int, str]:
        req = urllib.request.Request(api_url(), data=body, method="POST",
                                     headers={"content-type": "application/json", "user-agent": "printer-buddy"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read()).get("error", "")
            except Exception:
                return e.code, ""
        except Exception as e:
            return 0, f"couldn't reach the server: {e}"
