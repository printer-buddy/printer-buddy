from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

DEFAULTS = {"ai_threshold": 0.6, "ai_interval": 10, "ai_hold": 3, "ai_cooldown": 10}
CHOICES = {"ai_threshold": (0.4, 0.5, 0.6, 0.7, 0.8), "ai_interval": (5, 10, 20, 30),
           "ai_hold": (1, 2, 3, 5), "ai_cooldown": (5, 10, 20, 30)}
MODES = ("off", "alert", "pause")
SUSPECT_SPEEDUP = 5.   # s taken off the interval while a failure holds, until it alerts
MIN_INTERVAL = 2.
DEFAULT_MODE = "alert"
# only in onnxruntime-gpu builds (updater installs it for heavy when nvidia-smi works)
GPU_PROVIDERS = {"CUDAExecutionProvider": "GPU (CUDA)", "DmlExecutionProvider": "GPU (DirectML)"}
HISTORY_MAX = 4320   # checks kept per print for the score history chart (12h at 10s)


def enabled() -> bool:
    return kind() in ("lite", "basic", "heavy")


def kind() -> str:
    return os.getenv("PB_AI", "off").strip().lower()


class Model:

    def __init__(self, folder: Path):
        self.meta = json.load(open(folder / "model.json"))
        path = folder / self.meta["file"]
        want = self.meta.get("sha256")
        if want and hashlib.sha256(path.read_bytes()).hexdigest() != want:
            raise ValueError(f"{path.name} is damaged (checksum doesn't match model.json)")
        import numpy as np
        self.np = np
        if path.suffix == ".npz":
            from .lite_numpy import LiteModel
            self.lite = LiteModel.load(path)
            self.provider = "CPU (numpy)"
        else:
            import onnxruntime as ort
            self.lite = None
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2
            avail = ort.get_available_providers()
            wanted = [p for p in GPU_PROVIDERS if p in avail] + ["CPUExecutionProvider"]
            self.sess = ort.InferenceSession(str(path), opts, providers=wanted)
            self.provider = GPU_PROVIDERS.get(self.sess.get_providers()[0], "CPU")
        inp = self.meta["input"]
        self.size = int(inp["shape"][2])
        self.mean = np.array(inp["mean"], dtype=np.float32).reshape(3, 1, 1)
        self.std = np.array(inp["std"], dtype=np.float32).reshape(3, 1, 1)
        self.labels = self.meta["labels"]
        self.kinds = self.meta["kinds"]
        self.tag = self.meta.get("tag") or folder.name

    def _input(self, jpeg: bytes, flip_h=False, flip_v=False, rotation=0):
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(jpeg)).convert("RGB")
        if rotation:
            im = im.rotate(-int(rotation), expand=True)
        if flip_h:
            im = ImageOps.mirror(im)
        if flip_v:
            im = ImageOps.flip(im)
        im = im.resize((self.size, self.size), Image.BILINEAR)
        x = self.np.asarray(im, dtype=self.np.float32).transpose(2, 0, 1) / 255.0
        return ((x - self.mean) / self.std)[None].astype(self.np.float32)

    def _run(self, x):
        if self.lite:
            return self.lite.score(x[0])
        return self.sess.run([self.meta["output"]["name"]], {self.meta["input"]["name"]: x})[0][0]

    def score(self, jpeg: bytes, flip_h=False, flip_v=False, rotation=0) -> dict[str, float]:
        out = self._run(self._input(jpeg, flip_h, flip_v, rotation))
        return {l: round(float(s), 4) for l, s in zip(self.labels, out)}

    def locate(self, jpeg: bytes, label: str, flip_h=False, flip_v=False, rotation=0,
               force=False) -> list[float] | None:
        """Where in the camera picture LABEL's score comes from, as [x0, y0, x1, y1] (0-1, of the
        picture as the camera sends it), or None if no one part of the picture explains it.

        The model only scores whole pictures, so this greys out one patch at a time and sees how much
        the score drops (occlusion). 25 extra runs: only call it when a score is already high.
        """
        np = self.np
        x = self._input(jpeg, flip_h, flip_v, rotation)
        li = self.labels.index(label)
        # in log-odds: a score stuck near 100% still moves when part of the picture is hidden
        logit = lambda v: float(np.log(np.clip(v, 1e-6, 1 - 1e-6) / (1 - np.clip(v, 1e-6, 1 - 1e-6))))
        base = logit(self._run(x)[li])
        grid, k = LOCATE_GRID, LOCATE_PATCH
        cell = self.size / grid
        heat, change, cover = np.zeros((grid, grid)), np.zeros((grid, grid)), np.zeros((grid, grid))
        for r in range(grid - k + 1):
            for c in range(grid - k + 1):
                y = x.copy()
                y[..., round(r * cell):round((r + k) * cell), round(c * cell):round((c + k) * cell)] = 0.
                diff = base - logit(self._run(y)[li])
                heat[r:r + k, c:c + k] += max(0., diff)
                change[r:r + k, c:c + k] += abs(diff)
                cover[r:r + k, c:c + k] += 1
        heat /= cover
        if force and heat.max() < 1e-3:
            # nothing raises the score (a good print): where the model looks hardest instead
            heat = change / cover
        best = np.unravel_index(int(heat.argmax()), heat.shape)
        if heat[best] < LOCATE_MIN_DROP and not force:
            return None
        # the cells joined to the hottest one that are at least half as hot
        keep, todo = set(), [best]
        while todo:
            r, c = todo.pop()
            if (r, c) in keep or not (0 <= r < grid and 0 <= c < grid) or heat[r, c] < heat[best] / 2:
                continue
            keep.add((r, c))
            todo += [(r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)]
        rows, cols = [r for r, _ in keep], [c for _, c in keep]
        box = [min(cols) / grid, min(rows) / grid, (max(cols) + 1) / grid, (max(rows) + 1) / grid]
        if (box[2] - box[0]) * (box[3] - box[1]) > LOCATE_MAX_AREA and not force:
            return None
        return _to_camera(box, flip_h, flip_v, rotation)


LOCATE_GRID = 6          # cells per side; patches are LOCATE_PATCH x LOCATE_PATCH cells
LOCATE_PATCH = 2
LOCATE_MIN_DROP = 0.5    # log-odds the best patch must take off the score (90% -> 85%, 99.9% -> 99.8%)
LOCATE_MAX_AREA = 0.6
BOX_MODES = ("always", "failure", "off")
DEFAULT_BOX = "failure"
AREA_MARGIN = 0.05


def _to_camera(box, flip_h, flip_v, rotation):
    def back(u, v):
        if flip_v:
            v = 1 - v
        if flip_h:
            u = 1 - u
        r = int(rotation) % 360   # the picture was turned r degrees clockwise
        if r == 90:
            u, v = v, 1 - u
        elif r == 180:
            u, v = 1 - u, 1 - v
        elif r == 270:
            u, v = 1 - v, u
        return u, v
    (ax, ay), (bx, by) = back(box[0], box[1]), back(box[2], box[3])
    return [round(float(v), 4) for v in (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))]


def crop(jpeg: bytes, area: list[float]) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = im.size
    im = im.crop((round(area[0] * w), round(area[1] * h), round(area[2] * w), round(area[3] * h)))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue()


def draw_box(jpeg: bytes, box: list[float], text: str) -> bytes:
    from PIL import Image, ImageDraw
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = box[0] * w, box[1] * h, box[2] * w, box[3] * h
    d.rectangle([x0, y0, x1, y1], outline=(255, 40, 40), width=max(2, round(min(w, h) / 120)))
    tw, th = d.textbbox((0, 0), text)[2:]
    ty = y0 - th - 6 if y0 >= th + 6 else y1 + 2
    d.rectangle([x0, ty, x0 + tw + 8, ty + th + 4], fill=(255, 40, 40))
    d.text((x0 + 4, ty + 2), text, fill=(255, 255, 255))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88)
    return out.getvalue()


class AI:
    def __init__(self, store, data_dir: str,
                 snapshot: Callable[[int], Awaitable[bytes | None]],
                 transform: Callable[[int], tuple[bool, bool, int]],
                 mode_of: Callable[[int], str],
                 on_alert: Callable[[int, str, float, bytes | None, str, list | None], Awaitable[None]],
                 on_update: Callable[[int], None]):
        self.store = store
        self.root = Path(data_dir) / "ai"
        self.snapshot, self.transform, self.mode_of = snapshot, transform, mode_of
        self.on_alert, self.on_update = on_alert, on_update
        self.model: Model | None = None
        self.error: str | None = None
        self.tasks: dict[int, asyncio.Task] = {}
        self.state: dict[int, dict] = {}
        self.box: dict[int, str] = {}
        self.areas: dict[int, list | None] = {}

    def load(self):
        self.model, self.error = None, None
        if not enabled():
            return
        folders = sorted((p.parent for p in self.root.glob(f"{kind()}-*/model.json")),
                         key=lambda p: json.load(open(p / "model.json")).get("created", ""))
        if not folders:
            self.error = f"no {kind()} model installed - run the installer (AI detection)"
            log.warning("AI: %s", self.error)
            return
        try:
            self.model = Model(folders[-1])
            log.info("AI: loaded model %s on %s (%s)", self.model.tag, self.model.provider,
                     ", ".join(self.model.labels))
        except ImportError:
            self.error = "onnxruntime/numpy/Pillow not installed - run the installer (AI detection)"
            log.warning("AI: %s", self.error)
        except Exception as e:
            self.error = f"couldn't load the model: {e}"
            log.warning("AI: %s", self.error)

    def info(self) -> dict:
        m = self.model
        return {"enabled": enabled(), "loaded": bool(m), "error": self.error,
                "tag": m.tag if m else None, "provider": m.provider if m else None,
                "labels": m.labels if m else [],
                "kinds": m.kinds if m else {}, "default_threshold": m.meta.get("default_threshold") if m else None}

    def settings(self) -> dict:
        s = self.store.settings()
        return {k: s.get(k, v) for k, v in DEFAULTS.items()}

    def status(self, pid: int) -> dict | None:
        if not enabled():
            return None
        st = self.state.get(pid, {})
        return {"mode": self.mode_of(pid), "watching": pid in self.tasks,
                "score": st.get("score"), "top": st.get("top"), "scores": st.get("scores"), "box": st.get("box"),
                "at": st.get("at"), "streak": st.get("streak", 0), "alerted_at": st.get("alerted_at"),
                "fast": st.get("streak", 0) > 0 and not st.get("prompted"), "box_mode": self.box_mode(pid),
                "area": self.area(pid),
                "alert_item": st.get("alert_item")}

    def on_change(self, pid: int, prev: dict, v: dict):
        if not enabled() or not self.model:
            return
        if v["state"] == "printing" and v["connection"] == "online" and self.mode_of(pid) != "off":
            if pid not in self.tasks:
                if prev["state"] not in ("printing", "paused"):
                    self.state[pid] = {}
                self.tasks[pid] = asyncio.get_running_loop().create_task(self._watch(pid))
        else:
            self.cancel(pid, keep_scores=True)

    def refresh(self, pid: int, v: dict):
        self.on_change(pid, {"state": v["state"]}, v)

    def cancel(self, pid: int, keep_scores: bool = False):
        if t := self.tasks.pop(pid, None):
            t.cancel()
        if not keep_scores:
            self.state.pop(pid, None)
        elif pid in self.state:
            self.state[pid]["streak"] = 0

    async def stop(self):
        for pid in list(self.tasks):
            self.cancel(pid)

    async def check_now(self, pid: int) -> dict:
        if not enabled():
            raise RuntimeError("AI detection is off (installer: AI detection = lite, basic or heavy)")
        m = self.model
        if not m:
            raise RuntimeError(self.error or "No AI model loaded")
        img = await self.snapshot(pid)
        if not img:
            raise RuntimeError("Couldn't get a picture from this printer's camera")
        img, region = await self._crop(pid, img)
        scores = await asyncio.to_thread(m.score, img, *self.transform(pid))
        alerts = {l: v for l, v in scores.items() if m.kinds.get(l) == "alert"}
        top = max(alerts, key=alerts.get) if alerts else None
        score = alerts.get(top, 0.0)
        box = await self._locate(pid, img, region, top, score)
        return {"scores": scores, "top": top, "score": score, "box": box, "at": time.time()}

    async def _crop(self, pid: int, img: bytes) -> tuple[bytes, list[float] | None]:
        a = self.area(pid)
        if not a:
            return img, None
        region = [max(0., a[0] - AREA_MARGIN), max(0., a[1] - AREA_MARGIN),
                  min(1., a[2] + AREA_MARGIN), min(1., a[3] + AREA_MARGIN)]
        try:
            return await asyncio.to_thread(crop, img, region), region
        except Exception as e:
            log.info("AI: couldn't crop printer %s snapshot: %s", pid, e)
            return img, None

    async def _locate(self, pid: int, img: bytes, region: list[float] | None, top: str | None,
                      score: float) -> list[float] | None:
        mode = self.box_mode(pid)
        force = mode == "always"
        if not top or mode == "off" or (score < float(self.settings()["ai_threshold"]) and not force):
            return None
        try:
            box = await asyncio.to_thread(self.model.locate, img, top, *self.transform(pid), force)
        except Exception as e:
            log.info("AI: couldn't locate %s on printer %s: %s", top, pid, e)
            return None
        if not box or not region:
            return box
        w, h = region[2] - region[0], region[3] - region[1]
        return [round(region[0] + box[0] * w, 4), round(region[1] + box[1] * h, 4),
                round(region[0] + box[2] * w, 4), round(region[1] + box[3] * h, 4)]

    async def _watch(self, pid: int):
        try:
            while True:
                s = self.settings()
                await self._check(pid, s)
                await asyncio.sleep(self.next_interval(pid, s))
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("AI watcher for printer %s stopped", pid)
        finally:
            if self.tasks.get(pid) is asyncio.current_task():
                self.tasks.pop(pid, None)

    def box_mode(self, pid: int) -> str:
        if pid not in self.box:
            m = (self.store.get(pid) or {}).get("aibox")
            self.box[pid] = m if m in BOX_MODES else DEFAULT_BOX
        return self.box[pid]

    def set_box_mode(self, pid: int, mode: str):
        self.store.update(pid, aibox=mode)
        self.box[pid] = mode
        self.on_update(pid)

    def area(self, pid: int) -> list[float] | None:
        if pid not in self.areas:
            a = (self.store.get(pid) or {}).get("aiarea")
            self.areas[pid] = json.loads(a) if a else None
        return self.areas[pid]

    def set_area(self, pid: int, area: list[float] | None):
        self.store.update(pid, aiarea=json.dumps(area) if area else None)
        self.areas[pid] = area
        self.on_update(pid)

    def next_interval(self, pid: int, s: dict) -> float:
        """Seconds to the next check: SUSPECT_SPEEDUP sooner while a failure is building
        towards an alert (the streak has started), so it prompts sooner. Once it has prompted,
        back to the normal pace until a clean check."""
        interval = float(s["ai_interval"])
        st = self.state.get(pid, {})
        if st.get("streak", 0) > 0 and not st.get("prompted"):
            return max(MIN_INTERVAL, interval - SUSPECT_SPEEDUP)
        return interval

    async def _check(self, pid: int, s: dict):
        m = self.model
        img = await self.snapshot(pid)
        if not m or not img:
            return
        crop_img, region = await self._crop(pid, img)
        try:
            scores = await asyncio.to_thread(m.score, crop_img, *self.transform(pid))
        except Exception as e:
            log.info("AI: couldn't score printer %s snapshot: %s", pid, e)
            return
        alerts = {l: v for l, v in scores.items() if m.kinds.get(l) == "alert"}
        top = max(alerts, key=alerts.get) if alerts else None
        score = alerts.get(top, 0.0)
        box = await self._locate(pid, crop_img, region, top, score)
        st = self.state.setdefault(pid, {})
        st.update(scores=scores, top=top, score=score, box=box, at=time.time())
        hist = st.setdefault("history", [])
        hist.append([round(st["at"]), score, top or "", 0])
        if len(hist) > HISTORY_MAX:
            del hist[:len(hist) - HISTORY_MAX]
        st["streak"] = st.get("streak", 0) + 1 if score >= float(s["ai_threshold"]) else 0
        if not st["streak"]:
            st["prompted"] = False
        cooled = time.time() - (st.get("alerted_at") or 0) >= float(s["ai_cooldown"]) * 60
        if st["streak"] >= int(s["ai_hold"]) and cooled:
            st["alerted_at"], st["streak"], st["prompted"] = time.time(), 0, True
            hist[-1][3] = 1
            mode = self.mode_of(pid)
            log.info("AI: printer %s %s %.0f%% for %s checks -> %s", pid, top, score * 100, s["ai_hold"], mode)
            if mode != "off":
                await self.on_alert(pid, top, score, img, mode, box)
        self.on_update(pid)
