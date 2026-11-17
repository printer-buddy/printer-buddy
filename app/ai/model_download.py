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
import argparse, hashlib, json, os, re, subprocess, sys, tempfile, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def say(msg):
    print(f"  AI model: {msg}", flush=True)


def repo_and_token():
    try:
        url = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        url = ""
    m = re.match(r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None, None
    token = None
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "credential", "fill"],
                             input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True,
                             timeout=10, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}).stdout
        token = next((l.split("=", 1)[1] for l in out.splitlines() if l.startswith("password=")), None)
    except Exception:
        pass
    return f"{m.group(1)}/{m.group(2)}", token


def request(url, token, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "printer-buddy"})
    if token:
        # unredirected: GitHub sends file downloads on to a storage server that must NOT get the token
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="basic")
    ap.add_argument("--dir", default=str(ROOT / "data" / "ai"))
    a = ap.parse_args()

    repo, token = repo_and_token()
    if not repo:
        say("this copy isn't a GitHub clone, so there's nowhere to download from")
        return 1
    prefix = f"ai-{a.kind}-"
    try:
        with request(f"https://api.github.com/repos/{repo}/releases?per_page=100", token) as r:
            releases = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            say("GitHub refused (private repo?). Save a GitHub login first: menu 8) GitHub login")
        else:
            say(f"GitHub error {e.code}")
        return 1
    except Exception as e:
        say(f"couldn't reach GitHub: {e}")
        return 1

    rels = [r for r in releases if r.get("tag_name", "").startswith(prefix) and not r.get("draft")]
    if not rels:
        say(f"no '{prefix}*' release found on {repo}")
        return 1
    rel = max(rels, key=lambda r: r.get("published_at") or "")
    tag = rel["tag_name"]
    assets = {x["name"]: x for x in rel.get("assets", [])}
    if "model.json" not in assets:
        say(f"release {tag} has no model.json")
        return 1

    dest = Path(a.dir) / tag[len("ai-"):]
    try:
        with request(assets["model.json"]["url"], token, "application/octet-stream") as r:
            meta_bytes = r.read()
        meta = json.loads(meta_bytes)
    except Exception as e:
        say(f"couldn't download model.json: {e}")
        return 1
    fname, want = meta.get("file"), meta.get("sha256")
    if not fname or fname not in assets:
        say(f"release {tag} is missing {fname or 'the model file'}")
        return 1

    have = dest / fname
    if have.exists() and want and hashlib.sha256(have.read_bytes()).hexdigest() == want:
        say(f"{tag} already installed, up to date")
        return 0

    say(f"downloading {tag} ({assets[fname].get('size', 0) / 1e6:.1f} MB)...")
    dest.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest, suffix=".part")
    h = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as f, request(assets[fname]["url"], token, "application/octet-stream") as r:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                h.update(chunk)
        if want and h.hexdigest() != want:
            raise ValueError("checksum doesn't match model.json - download damaged")
        os.replace(tmp, have)
        (dest / "model.json").write_bytes(meta_bytes)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        say(f"download failed: {e}")
        return 1
    say(f"{tag} installed in {dest.relative_to(ROOT) if dest.is_relative_to(ROOT) else dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
