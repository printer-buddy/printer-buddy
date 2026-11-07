#!/usr/bin/env python3
"""Printer Buddy updates. Standard library only, so it also runs before the venv exists.

  python3 app/core/updater.py update     core to the channel's branch, then everything below
  python3 app/core/updater.py parts      download the parts (ui, notify, timelapse, streamer)
  python3 app/core/updater.py ai         AI libraries and model
  python3 app/core/updater.py streamer   download and set up the camera streamer
  python3 app/core/updater.py packages   system packages the chosen features need

Root jobs go through the root helper (tools/root.sh), the only thing the app may run with sudo.
"""
from __future__ import annotations

import glob
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "printer-buddy.env"
STATE = ROOT / "data" / "parts"
VENV = ROOT / ".venv" / "bin"
HELPERS = ("/usr/local/lib/printer-buddy/root.sh", "/usr/local/lib/printer-buddy/streamer-root.sh")
RELEASES = "https://api.github.com/repos/printer-buddy/printer-buddy/releases?per_page=100"
HDR = {"User-Agent": "printer-buddy", "Accept": "application/vnd.github+json"}
CHANNEL_BRANCH = {"stable": "main", "nightly": "nightly"}
# setting -> choices (first one is the default)
CHOICES = {"PB_UI": ("advanced",), "PB_NOTIFY": ("basic", "advanced"), "PB_AI": ("off", "lite", "basic", "heavy"),
           "PB_VIDEO": ("snapshots", "live", "timelapse"), "PB_STREAM": ("off", "light", "basic", "advanced"),
           "PB_CHANNEL": ("stable", "nightly")}
_releases: list | None = None


def say(msg: str):
    print(msg, flush=True)


def env(key: str) -> str:
    v = ""
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith(f"{key}="):
                v = line.split("=", 1)[1].strip()
    except OSError:
        pass
    choices = CHOICES.get(key)
    return v if not choices or v in choices else choices[0]


def dev_parts() -> set[str]:
    """Parts set to dev/beta (nightly) builds while the rest follow the channel."""
    return {x.strip() for x in env("PB_NIGHTLY").split(",") if x.strip()}


def channel(name: str) -> str:
    return "nightly" if env("PB_CHANNEL") == "nightly" or name in dev_parts() else "stable"


def git(*args) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True).stdout.strip()


def run(*cmd) -> bool:
    return subprocess.run([str(c) for c in cmd], cwd=ROOT).returncode == 0


def core_branch() -> str:
    """The branch core updates follow: the channel's, unless another branch was picked in the installer."""
    cur = git("rev-parse", "--abbrev-ref", "HEAD")
    if cur in ("", "HEAD", "builds", *CHANNEL_BRANCH.values()):
        return CHANNEL_BRANCH[channel("core")]
    return cur


def update_core() -> bool:
    b = core_branch()
    say(f"Getting the latest {b}...")
    ok = run("git", "-C", ROOT, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*") \
        and run("git", "-C", ROOT, "fetch", "-q", "--prune", "origin") \
        and run("git", "-C", ROOT, "reset", "-q", "--hard") \
        and run("git", "-C", ROOT, "checkout", "-q", "-B", b, f"origin/{b}") \
        and run("git", "-C", ROOT, "branch", "-q", "-u", f"origin/{b}")
    say(f"Now on {b}: {git('log', '-1', '--format=%h %s')}" if ok else "Couldn't update from GitHub. Nothing was changed.")
    return ok


# ------------------------------------------------------------------ parts

def releases() -> list:
    global _releases
    if _releases is None:
        _releases = json.load(urllib.request.urlopen(urllib.request.Request(RELEASES, headers=HDR), timeout=30))
    return _releases


def _nums(v: str) -> list[int]:
    return [int(x) for x in v.lstrip("v").replace("-", ".").split(".") if x.isdigit()]


def installed(c: str) -> str | None:
    """Installed version of a component ("nightly", "v1.2" or "" if unknown); None = off, so it doesn't count."""
    if c == "core":
        if git("rev-parse", "--abbrev-ref", "HEAD") == "nightly":
            return "nightly"
        return git("describe", "--tags", "--abbrev=0", "--match", "core-v*").split("core-", 1)[-1]
    if c == "ai":
        if env("PB_AI") == "off":
            return None
        tags = sorted(json.load(open(m)).get("tag", "") for m in glob.glob(str(ROOT / "data" / "ai" / "*" / "model.json")))
        return tags[-1].rsplit("-", 1)[-1] if tags else None
    if c == "streamer" and env("PB_STREAM") == "off":
        return None
    try:
        t = (STATE / c).read_text().split()[0]
    except (OSError, IndexError):
        return None
    return "nightly" if t.endswith("-nightly") else t.split("-", 1)[-1]


def missing_needs(rel: dict) -> list[str]:
    need = next((l.split(":", 1)[1] for l in (rel.get("body") or "").splitlines() if l.lower().startswith("needs:")), "")
    miss = []
    for item in need.split(","):
        bits = item.split()
        if len(bits) == 2:
            h = installed(bits[0])
            if h is not None and h != "nightly" and (not h or _nums(bits[1]) > _nums(h)):
                miss.append(item.strip())
    return miss


def fetch_part(name: str, asset: str, inner: str, dest: Path) -> bool:
    ch = channel(name)
    try:
        rels = releases()
    except OSError as e:
        say(f"Couldn't get the {name} part ({e}), kept what was there.")
        return False
    want = (lambda t: t == f"{name}-nightly") if ch == "nightly" else (lambda t: t.startswith(f"{name}-v"))
    rel = next((r for r in rels if want(r["tag_name"]) and not r.get("draft")), None)
    a = rel and next((x for x in rel.get("assets", []) if x["name"] == asset), None)
    if not a:
        why = f"{rel['tag_name']} has no {asset}" if rel else f"no {ch} release yet"
        say(f"Couldn't get the {name} part ({why}), kept what was there.")
        return False
    if ch != "nightly" and dest.exists() and (miss := missing_needs(rel)):
        say(f"{name}: {rel['tag_name']} needs {', '.join(miss)} first, kept what was there")
        return True
    stamp = f"{rel['tag_name']} {a['updated_at']} {asset}"
    mark = STATE / name
    if dest.exists() and mark.exists() and mark.read_text() == stamp:
        return True
    try:
        data = urllib.request.urlopen(urllib.request.Request(a["browser_download_url"], headers=HDR), timeout=300).read()
    except OSError as e:
        say(f"Couldn't download the {name} part ({e}), kept what was there.")
        return False
    tmp = Path(tempfile.mkdtemp())
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            t.extractall(tmp, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        src = tmp / inner
        if src.is_dir():
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(src, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    STATE.mkdir(parents=True, exist_ok=True)
    mark.write_text(stamp)
    say(f"{name}: {rel['tag_name']}")
    return True


def parts() -> bool:
    ok = fetch_part("ui", f"print-buddy-ui-{env('PB_UI')}.tar.gz", "print-buddy-ui", ROOT / "ui")
    ok &= fetch_part("notify", f"print-buddy-notify-{env('PB_NOTIFY')}.tar.gz", "print-buddy-notify/pb_notify/__init__.py",
                     ROOT / "app" / "core" / "notify.py")
    ok &= fetch_part("timelapse", "print-buddy-timelapse.tar.gz", "print-buddy-timelapse/pb_timelapse/__init__.py",
                     ROOT / "app" / "media" / "timelapse.py")
    if (tier := env("PB_STREAM")) != "off":
        ok &= fetch_part("streamer", f"print-buddy-streamer-{tier}.tar.gz", "print-buddy-streamer", ROOT / "streamer")
    return ok


# ------------------------------------------------------------------ the rest

def helper() -> str | None:
    return next((h for h in HELPERS if os.path.exists(h)), None)


def root(*args) -> bool:
    h = helper()
    if not h:
        say("The root helper isn't set up yet: run ./install.sh and pick Update once.")
        return False
    return subprocess.run(["sudo", "-n", h, *args]).returncode == 0


def pip() -> bool:
    if not (VENV / "pip").exists():
        return True
    return run(VENV / "pip", "install", "-q", "-r", ROOT / "requirements.txt")


def nvidia_gpu() -> bool:
    exe = shutil.which("nvidia-smi")
    return bool(exe) and subprocess.run([exe], capture_output=True).returncode == 0


def ensure_ai() -> bool:
    kind = env("PB_AI")
    if kind == "off" or not (VENV / "pip").exists():
        return True
    if kind != "lite" and platform.machine() not in ("aarch64", "arm64", "x86_64", "amd64"):
        say(f"AI detection needs a 64-bit system (this one is {platform.machine()}). Skipping AI; everything else works.")
        return True
    gpu = kind == "heavy" and nvidia_gpu()
    say("Installing AI libraries..." + (" (NVIDIA GPU found)" if gpu else ""))
    if gpu:
        # onnxruntime and onnxruntime-gpu can't be installed together
        run(VENV / "pip", "uninstall", "-q", "-y", "onnxruntime")
    reqs = "requirements-ai-lite.txt" if kind == "lite" else "requirements-ai-gpu.txt" if gpu else "requirements-ai.txt"
    if not run(VENV / "pip", "install", "-q", "-r", ROOT / reqs):
        say("Couldn't install the AI libraries. Skipping AI; everything else works.")
        return True
    (ROOT / "data" / "ai").mkdir(parents=True, exist_ok=True)
    if not run(VENV / "python", ROOT / "app" / "ai" / "model_download.py", "--kind", kind, "--dir", ROOT / "data" / "ai"):
        say("No AI model yet. AI stays off until one downloads.")
    return True


def ensure_streamer() -> bool:
    tier = env("PB_STREAM")
    if tier == "off":
        if helper():
            root("disable")
        return True
    if not fetch_part("streamer", f"print-buddy-streamer-{tier}.tar.gz", "print-buddy-streamer", ROOT / "streamer"):
        return False
    conf = ROOT / "data" / "streamer.conf"
    src = conf if conf.exists() else ROOT / "streamer" / "streamer.conf.example"
    text = re.sub(r"(?m)^tier:.*$", f"tier: {tier}", src.read_text())
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(text)
    say(f"Setting up the camera streamer ({tier})...")
    import pwd
    user = pwd.getpwuid(os.getuid()).pw_name
    if not all(root(*a) for a in (("deps", tier), ("unit", str(ROOT), user), ("enable",), ("restart",))):
        say("Couldn't set up the camera streamer.")
        return False
    asvc = Path.home() / "printer_data" / "moonraker.asvc"
    if asvc.exists() and "printer-buddy-streamer" not in asvc.read_text().split():
        with open(asvc, "a") as fh:
            fh.write("printer-buddy-streamer\n")
    return True


def ensure_packages() -> bool:
    ok = True
    if env("PB_VIDEO") == "timelapse" and not shutil.which("ffmpeg"):
        say("Installing ffmpeg (needed for timelapses)...")
        if not root("pkg", "ffmpeg"):
            say("Couldn't install ffmpeg; timelapses will save pictures but no video.")
            ok = False
    return ok


def after_core() -> bool:
    results = [parts(), pip(), ensure_ai(), ensure_streamer(), ensure_packages()]
    return all(results)


def update() -> bool:
    if not update_core():
        return False
    # carry on with the code that was just downloaded
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), "after"])


JOBS = {"update": update, "core": update_core, "after": after_core, "parts": parts, "ai": ensure_ai,
        "streamer": ensure_streamer, "packages": ensure_packages}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in JOBS:
        sys.exit(f"usage: {sys.argv[0]} {'|'.join(JOBS)}")
    sys.exit(0 if JOBS[sys.argv[1]]() else 1)
