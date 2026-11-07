#!/usr/bin/env python3
"""Printer Buddy AI for a Klipper printer (such as a Creality K1): watches the camera during a print.

Needs only Python 3, numpy and Pillow. When the lite model sees a problem it shows a message
in the console and on the screen, and can pause the print. It also draws a status card (an SVG
picture) that Fluidd or Mainsail can show as a camera. Settings: config.json next to this file.
"""
import io
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lite_numpy import LiteModel  # noqa: E402

DEFAULTS = {
    "moonraker": "http://127.0.0.1:7125",
    "camera_url": "",
    "interval": 30,
    "threshold": 0.6,
    "checks_in_a_row": 3,
    "pause": False,
    "card": True,
    "card_dir": "",
}
HISTORY = 240
GREEN, AMBER, RED, GREY = "#4ade80", "#fbbf24", "#f87171", "#9ca3af"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(HERE, "config.json")) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError) as e:
        log("config.json not read, using defaults:", e)
    return cfg


def get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def post(url, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def printing(cfg):
    j = json.loads(get(cfg["moonraker"] + "/printer/objects/query?print_stats"))
    return j["result"]["status"]["print_stats"]["state"] == "printing"


def snapshot_url(cfg):
    if cfg["camera_url"]:
        return cfg["camera_url"]
    cams = json.loads(get(cfg["moonraker"] + "/server/webcams/list"))["result"]["webcams"]
    for c in cams:
        if c.get("enabled", True) and c.get("snapshot_url"):
            u = c["snapshot_url"]
            return u if "://" in u else "http://127.0.0.1" + (u if u.startswith("/") else "/" + u)
    raise RuntimeError("no camera found: set camera_url in config.json")


def tell(cfg, text, pause):
    msg = text.replace('"', "'")
    post(cfg["moonraker"] + "/printer/gcode/script", {"script": 'M117 %s\nRESPOND TYPE=error MSG="%s"' % (msg[:40], msg)})
    if pause:
        post(cfg["moonraker"] + "/printer/print/pause")


def card_dir(cfg):
    if cfg["card_dir"]:
        return cfg["card_dir"]
    for d in (os.path.expanduser("~/printer_data/config"), "/usr/data/printer_data/config"):
        if os.path.isdir(d):
            return os.path.join(d, ".printer-buddy-ai")
    return ""


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def draw_card(status, colour, top, history, last, threshold):
    x0, y0, w, h = 16, 92, 288, 64
    pts = ""
    if len(history) > 1:
        step = w / float(max(len(history) - 1, 1))
        pts = " ".join("%.1f,%.1f" % (x0 + i * step, y0 + h - min(max(v, 0.0), 1.0) * h) for i, v in enumerate(history))
    ty = y0 + h - threshold * h
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">'
        '<rect width="320" height="180" rx="10" fill="#111318"/>'
        '<text x="16" y="28" fill="#e5e7eb" font-family="sans-serif" font-size="13">Printer Buddy AI</text>'
        '<circle cx="16" cy="50" r="5" fill="%s"/>'
        '<text x="28" y="55" fill="%s" font-family="sans-serif" font-size="17" font-weight="bold">%s</text>'
        '<text x="304" y="55" text-anchor="end" fill="#e5e7eb" font-family="sans-serif" font-size="17">%s</text>'
        '<rect x="%d" y="%d" width="%d" height="%d" fill="#1b1e26"/>'
        '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#4b5563" stroke-dasharray="4 4"/>'
        '<polyline points="%s" fill="none" stroke="%s" stroke-width="2"/>'
        '<text x="16" y="174" fill="%s" font-family="sans-serif" font-size="11">Last check %s</text>'
        '</svg>'
    ) % (colour, colour, esc(status), esc(top), x0, y0, w, h, x0, ty, x0 + w, ty, pts, colour, GREY, esc(last))


def write_card(cfg, *args):
    d = card_dir(cfg) if cfg["card"] else ""
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "status.svg.tmp")
        with open(tmp, "w") as f:
            f.write(draw_card(*args))
        os.replace(tmp, os.path.join(d, "status.svg"))
    except OSError as e:
        log("status card not written:", e)


def main():
    cfg = load_config()
    model = LiteModel.load(os.path.join(HERE, "printer-buddy-lite.npz"))
    meta = json.load(open(os.path.join(HERE, "model.json")))
    labels, alert = meta["labels"], set(meta.get("alert", []))
    log("started, checking every", cfg["interval"], "s while printing")
    strikes, alerted, history, idle = 0, False, [], False
    while True:
        try:
            if not printing(cfg):
                strikes, alerted, history = 0, False, []
                if not idle:
                    write_card(cfg, "Not printing", GREY, "", [], "", cfg["threshold"])
                    idle = True
                time.sleep(10)
                continue
            idle = False
            t = time.time()
            scores = dict(zip(labels, model.score_image(get(snapshot_url(cfg)))))
            top = max((v, k) for k, v in scores.items() if k in alert)
            took = time.time() - t
            log("%s %.0f%% (%.1f s)" % (top[1], top[0] * 100, took))
            history = (history + [top[0]])[-HISTORY:]
            strikes = strikes + 1 if top[0] >= cfg["threshold"] else 0
            if strikes >= cfg["checks_in_a_row"] and not alerted:
                alerted = True
                tell(cfg, "Printer Buddy AI: possible %s (%.0f%%)" % (top[1].replace("_", " "), top[0] * 100), cfg["pause"])
                log("alert sent" + (", print paused" if cfg["pause"] else ""))
            elif strikes == 0:
                alerted = False
            if alerted:
                status, colour = ("Paused print", RED) if cfg["pause"] else ("Warning sent", RED)
            else:
                status, colour = "Watching", (AMBER if strikes else GREEN)
            write_card(cfg, status, colour, "%s %.0f%%" % (top[1].replace("_", " "), top[0] * 100), history,
                       time.strftime("%H:%M:%S"), cfg["threshold"])
            time.sleep(max(1.0, cfg["interval"] - took))
        except KeyboardInterrupt:
            return
        except Exception as e:
            log("error:", e)
            time.sleep(15)


if __name__ == "__main__":
    main()
