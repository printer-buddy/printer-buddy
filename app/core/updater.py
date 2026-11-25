#!/usr/bin/env python3
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
"""Printer Buddy updates. Standard library only, so it also runs before the venv exists.

  python3 app/core/updater.py update     core to the channel's branch, then everything below
  python3 app/core/updater.py parts      download the parts (ui, notify, timelapse, streamer)
  python3 app/core/updater.py ai         AI libraries and model
  python3 app/core/updater.py streamer   download and set up the camera streamer
  python3 app/core/updater.py packages   system packages the chosen features need

Nothing here needs root; the camera streamer runs under Printer Buddy (see app/core/streamer.py).
"""
from __future__ import annotations

import glob
import hashlib
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
HELPERS = ("/usr/local/lib/printer-buddy/restart.sh",)
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


def check_sha256(data: bytes, digest: str | None, what: str) -> bytes:
    if not (digest or "").startswith("sha256:"):
        raise ValueError(f"no checksum listed for {what}")
    if hashlib.sha256(data).hexdigest() != digest[7:]:
        raise ValueError(f"{what} doesn't match its checksum")
    return data


def github_asset(repo: str, name: str, tag: str | None = None, timeout: int = 300) -> bytes:
    """Download a release file (latest release when no tag) and check it against the sha256 GitHub lists for it."""
    url = f"https://api.github.com/repos/{repo}/releases/" + (f"tags/{tag}" if tag else "latest")
    rel = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=30))
    a = next((x for x in rel.get("assets", []) if x.get("name") == name), None)
    if not a:
        raise ValueError(f"{rel.get('tag_name', repo)} has no {name}")
    data = urllib.request.urlopen(urllib.request.Request(a["browser_download_url"], headers=HDR), timeout=timeout).read()
    return check_sha256(data, a.get("digest"), name)


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
        check_sha256(data, a.get("digest"), asset)
    except (OSError, ValueError) as e:
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


STREAM_BIN = ROOT / "data" / "streamer-bin"


def _arch(table: dict) -> str | None:
    return table.get(platform.machine())


def _deb_extract(pkg: str, tmp: Path) -> Path | None:
    if subprocess.run(["apt-get", "download", pkg], cwd=tmp, capture_output=True).returncode != 0:
        return None
    debs = sorted(tmp.glob(f"{pkg}_*.deb"))
    out = tmp / pkg
    if not debs or subprocess.run(["dpkg-deb", "-x", str(debs[-1]), str(out)]).returncode != 0:
        return None
    return out


def _installed(pkg: str) -> bool:
    st = subprocess.run(["dpkg-query", "-W", "-f=${Status}", pkg], capture_output=True, text=True).stdout
    return st.strip() == "install ok installed"


def fetch_ustreamer() -> bool:
    """ustreamer has no ready-made download, so take the box's own distro package (apt-get download needs no root),
    unpack its program into data/streamer-bin and any libraries the box doesn't have into data/streamer-bin/lib."""
    dest = STREAM_BIN / "ustreamer"
    if dest.exists():
        return True
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        root = _deb_extract("ustreamer", tmp)
        if not root or not (root / "usr" / "bin" / "ustreamer").exists():
            say("Couldn't download ustreamer.")
            return bool(shutil.which("ustreamer"))
        todo, seen = ["ustreamer"], {"ustreamer"}
        while todo:
            deps = subprocess.run(["apt-cache", "depends", "--no-recommends", "--no-suggests", "--no-enhances",
                                   "--no-breaks", "--no-conflicts", "--no-replaces", todo.pop()],
                                  capture_output=True, text=True).stdout
            for name in re.findall(r"^\s*\|?(?:Pre)?Depends: ([^\s<>]+)$", deps, re.M):
                if name in seen or _installed(name):
                    continue
                seen.add(name)
                lib = _deb_extract(name, tmp)
                if not lib:
                    say(f"Couldn't download {name}, which ustreamer needs.")
                    return bool(shutil.which("ustreamer"))
                (STREAM_BIN / "lib").mkdir(parents=True, exist_ok=True)
                for so in lib.rglob("*.so*"):
                    shutil.copy2(so, STREAM_BIN / "lib" / so.name, follow_symlinks=False)
                todo.append(name)
        STREAM_BIN.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / "usr" / "bin" / "ustreamer", dest)
    return True


def fetch_stream_bins(tier: str) -> bool:
    STREAM_BIN.mkdir(parents=True, exist_ok=True)
    if tier == "light":
        a = _arch({"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "armhf", "armv6l": "armhf"})
        if not a:
            say(f"No mjpg-streamer build for {platform.machine()}.")
            return True
        dest = STREAM_BIN / "mjpg-streamer"
        dest.mkdir(parents=True, exist_ok=True)
        try:
            data = github_asset("printer-buddy/print-buddy-streamer", f"mjpg-streamer-{a}.tar.gz", "mjpg-streamer", 120)
            with tarfile.open(fileobj=io.BytesIO(data)) as t:
                members = []
                for m in t.getmembers():
                    parts = m.name.split("/", 1)
                    if len(parts) == 2 and parts[1]:
                        m.name = parts[1]
                        members.append(m)
                t.extractall(dest, members=members, filter="data") if sys.version_info >= (3, 12) else t.extractall(dest, members=members)
        except Exception as e:
            say(f"Couldn't download mjpg-streamer: {e}")
            return False
        return True
    if not fetch_ustreamer():
        return False
    if tier == "advanced":
        if not (STREAM_BIN / "go2rtc").exists():
            a = _arch({"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm", "armv6l": "armv6"})
            if not a:
                say(f"No go2rtc build for {platform.machine()}.")
                return True
            try:
                data = github_asset("AlexxIT/go2rtc", f"go2rtc_linux_{a}", timeout=120)
                (STREAM_BIN / "go2rtc").write_bytes(data)
                (STREAM_BIN / "go2rtc").chmod(0o755)
            except Exception as e:
                say(f"Couldn't download go2rtc: {e}")
                return False
    return True


def ensure_streamer() -> bool:
    tier = env("PB_STREAM")
    if tier == "off":
        return True
    if not fetch_part("streamer", f"print-buddy-streamer-{tier}.tar.gz", "print-buddy-streamer", ROOT / "streamer"):
        return False
    conf = ROOT / "data" / "streamer.conf"
    src = conf if conf.exists() else ROOT / "streamer" / "streamer.conf.example"
    text = re.sub(r"(?m)^tier:.*$", f"tier: {tier}", src.read_text())
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(text)
    say(f"Setting up the camera streamer ({tier})...")
    if not fetch_stream_bins(tier):
        say("Couldn't set up the camera streamer.")
        return False
    return True


def ensure_packages() -> bool:
    return True


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
