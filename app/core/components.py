from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import updater

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "printer-buddy.env"
STATE = ROOT / "data" / "parts"
UPDATER = ROOT / "app" / "core" / "updater.py"
UPDATES_URL = "https://github.com/printer-buddy/printer-buddy/releases/download/updates/updates.txt"
# part -> (env var holding its tier, tiers, file or folder it installs)
PARTS = {
    "ui": ("PB_UI", ("advanced",), ROOT / "ui"),
    "notify": ("PB_NOTIFY", ("basic", "advanced"), ROOT / "app" / "core" / "notify.py"),
    "timelapse": (None, (), ROOT / "app" / "media" / "timelapse.py"),
    "streamer": ("PB_STREAM", ("off", "light", "basic", "advanced"), ROOT / "streamer"),
}
REQUIRED = ("ui", "notify", "timelapse")
AI_KINDS = ("off", "basic", "heavy")
_cache: dict = {}


def ensure_parts():
    missing = [n for n in REQUIRED if not PARTS[n][2].exists()]
    if not missing:
        return
    log.warning("parts missing (%s), downloading them", ", ".join(missing))
    try:
        subprocess.run([sys.executable, str(UPDATER), "parts"], cwd=ROOT, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as ex:
        log.error("couldn't download the parts (%s): run ./install.sh --parts", ex)


def env_get(key: str, default: str = "") -> str:
    v = os.getenv(key)
    if v is not None:
        return v.strip()
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


def env_set(key: str, value: str):
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{key}=")] + [f"{key}={value}"]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.environ[key] = value


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        value = fn()
    except Exception as ex:
        log.info("%s: %s", key, ex)
        value = hit[1] if hit else None
    _cache[key] = (time.time(), value)
    return value


def latest() -> dict:
    """updates.txt: {(part, channel or tier): version, ("needs", tag): [(component, min version)]}."""
    def fetch():
        req = urllib.request.Request(UPDATES_URL, headers={"User-Agent": "printer-buddy", "Cache-Control": "no-cache"})
        text = urllib.request.urlopen(req, timeout=15).read().decode()
        out = {}
        for line in text.splitlines():
            bits = line.split()
            if len(bits) == 4 and bits[0] == "needs":
                out.setdefault(("needs", bits[1]), []).append((bits[2], bits[3]))
            elif len(bits) == 3 and not line.startswith("#"):
                out[(bits[0], bits[1])] = bits[2]
        return out
    return _cached("updates", 600, fetch) or {}


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=20).stdout.strip()


def core() -> dict:
    cur, want = _git("rev-parse", "--abbrev-ref", "HEAD"), updater.core_branch()
    remote = _cached(f"core:{want}", 600, lambda: (_git("ls-remote", "origin", f"refs/heads/{want}").split() or [""])[0][:7])
    have = _git("rev-parse", "--short=7", "HEAD")
    ver = latest().get(("core", "stable")) if want == "main" else None
    return {"installed": f"{cur} {have}", "latest": f"{want} {ver or remote}" if remote else None, "version": ver,
            "update": bool(remote) and (cur != want or remote != have)}


def _newer(a: str, b: str) -> bool:
    """Is version a (e.g. v1.10) newer than b (v1.9)?"""
    def nums(v):
        return [int(x) for x in v.lstrip("v").replace("-", ".").split(".") if x.isdigit()]
    return nums(a) > nums(b)


def _installed() -> dict[str, str]:
    """component -> installed version ("nightly", "v1.2", or "" if unknown). Turned-off parts are left out."""
    have = {c: updater.installed(c) for c in (*PARTS, "core", "ai")}
    return {c: v for c, v in have.items() if v is not None}


def _missing(needs: list, have: dict) -> list[str]:
    return [f"{c} {v}" for c, v in needs if c in have and have[c] != "nightly" and (not have[c] or _newer(v, have[c]))]


def _stamp(name: str) -> tuple[str, str]:
    try:
        tag, _, when = (STATE / name).read_text().strip().partition(" ")
        return tag, when
    except OSError:
        return "", ""


def components(ai_tag: str | None, fresh: bool = False) -> dict:
    if fresh:
        _cache.clear()
    channel = env_get("PB_CHANNEL", "stable") or "stable"
    avail = latest()
    have = _installed()
    dev = updater.dev_parts()

    def need(tag, ch="stable"):
        return _missing(avail.get(("needs", tag), []), have) if ch == "stable" else []
    parts = []
    for name, (var, tiers, path) in PARTS.items():
        tag, when = _stamp(name)
        if var and env_get(var, tiers[0]) == "off":
            parts.append({"name": name, "installed": "off", "latest": None, "update": False, "tier": "off", "tiers": list(tiers),
                          "dev": name in dev})
            continue
        ch = updater.channel(name)
        if ch == "nightly":
            new = avail.get((name, "nightly"))
            update = bool(new) and (tag != f"{name}-nightly" or when[:10] < new)
            show_new = f"nightly {new}" if new else None
            installed = f"nightly {when[:10]}" if tag == f"{name}-nightly" else tag.removeprefix(f"{name}-")
        else:
            new = avail.get((name, "stable"))
            update = bool(new) and (not tag.startswith(f"{name}-v") or _newer(new, tag.removeprefix(f"{name}-")))
            show_new, installed = new, tag.removeprefix(f"{name}-")
        miss = need(f"{name}-{new}", ch) if update else []
        parts.append({"name": name, "installed": installed or ("present" if path.exists() else None), "latest": show_new,
                      "update": update and not miss, "needs": miss, "tier": env_get(var, tiers[0]) if var else None,
                      "tiers": list(tiers), "dev": name in dev})
    kind = env_get("PB_AI", "off") or "off"
    ai_new = avail.get(("ai", kind))
    ai_up = bool(ai_new) and kind != "off" and (not (ai_tag or "").removeprefix("ai-").startswith(f"{kind}-")
                                                 or _newer(ai_new, (ai_tag or "").removeprefix("ai-").removeprefix(f"{kind}-")))
    ai_miss = need(f"ai-{kind}-{ai_new}") if ai_up else []
    c = core()
    c["needs"] = need(f"core-{c['version']}", updater.channel("core")) if c["update"] and c["version"] else []
    c["update"] = c["update"] and not c["needs"]
    c["dev"] = "core" in dev
    return {"channel": channel, "core": c, "parts": parts,
            "ai": {"kind": kind, "kinds": list(AI_KINDS), "installed": ai_tag, "latest": ai_new and f"{kind}-{ai_new}",
                   "update": ai_up and not ai_miss, "needs": ai_miss}}


def run(what: str) -> tuple[bool, str]:
    """core | parts | ai | streamer: do the update, return (ok, output)."""
    jobs = {"core": "update", "parts": "parts", "ai": "ai", "streamer": "streamer"}
    if what not in jobs:
        return False, f"unknown: {what}"
    cmd = [sys.executable, str(UPDATER), jobs[what]]
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return False, str(ex)
    _cache.clear()
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-2000:]


SERVICES = ("printer-buddy", "printer-buddy-streamer")
# (action, service) -> root helper job; Printer Buddy restarting itself is done by the caller
SYSTEM_JOBS = {("restart", "printer-buddy-streamer"): "restart", ("start", "printer-buddy-streamer"): "enable",
               ("stop", "printer-buddy-streamer"): "disable", ("reboot", None): "reboot", ("shutdown", None): "shutdown"}


def system() -> dict:
    """Printer Buddy's own services, as systemd reports them."""
    out = []
    for name in SERVICES:
        try:
            p = subprocess.run(["systemctl", "show", name, "-p", "LoadState,ActiveState,SubState"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        s = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
        if s.get("LoadState") == "loaded":
            out.append({"name": name, "active_state": s.get("ActiveState", ""), "sub_state": s.get("SubState", "")})
    return {"services": out, "helper": bool(updater.helper())}


def system_action(action: str, service: str | None) -> tuple[bool, str]:
    job = SYSTEM_JOBS.get((action, service))
    if not job:
        return False, f"can't {action} {service or 'the host'}"
    helper = updater.helper()
    if not helper:
        return False, "The root helper isn't set up yet: run ./install.sh and pick Update once."
    try:
        p = subprocess.run(["sudo", "-n", helper, job], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return False, str(ex)
    if p.returncode == 2 and job in ("reboot", "shutdown"):
        return False, "This box's root helper is older: run ./install.sh and pick Update once."
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-500:]
