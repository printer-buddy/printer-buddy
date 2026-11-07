from __future__ import annotations

import base64
import re
import time
import zlib
import unicodedata
from collections import deque
from pathlib import Path

MAX_TEXT = 300
MAX_NAME = 24
KEEP = 100
VIEWER_GAP = 2.0
ROOM_PER_MINUTE = 30
_SPACE = re.compile(r"[\x00-\x20\x7f​-‏  ]+")


FLAG_GAP = 60.0
BLOCK_WAIT = 10.0
ROOM_FLAGS = 5
BLOCKED = "That message has a word that isn't allowed here."
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "i"})
HEADER = """# Words that aren't allowed in the chat, one per line. Lines starting with # are ignored.
# A message or name with one of these is refused. It's checked without capitals, accents or tricks like
# 1 for i, 0 for o, @ for a, or spaces between the letters (b a d).
#   word        matches that whole word only
#   two words   matches that phrase
#   word*       matches words that start with it (word, words, wording)
#   *word       matches words that end with it
#   *word*      matches anywhere, even inside other words
"""


def _clean(s, limit: int) -> str:
    return _SPACE.sub(" ", str(s or "")).strip()[:limit]


def _tokens(s) -> tuple[list[str], str]:
    """Letters only, after undoing accents and look-alikes: the words, and the whole text run together."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().translate(LEET)
    raw = re.findall(r"[a-z]+", s)
    words, run = [], []
    for t in raw:
        if len(t) == 1:
            run.append(t)
            continue
        if run:
            words.append("".join(run))
            run = []
        words.append(t)
    if run:
        words.append("".join(run))
    return words, "".join(raw)


class Blocklist:
    """The words an owner doesn't want in the chat, kept in a plain text file."""

    def __init__(self, path, builtin=None):
        self.path = Path(path)
        self.builtin = builtin
        self._stamp = None
        self._terms: list[tuple[str, list[str], str]] = []
        self._base: list[tuple[str, list[str], str]] | None = None

    @staticmethod
    def _parse(lines) -> list[tuple[str, list[str], str]]:
        out = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lead, trail = line.startswith("*"), line.endswith("*")
            words, joined = _tokens(line.strip("*"))
            if joined:
                out.append((joined, words, "any" if lead and trail else "start" if trail else "end" if lead else "word"))
        return out

    def _load(self):
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        self._stamp, self._terms = stamp, []
        try:
            self._terms = self._parse(self.path.read_text(encoding="utf-8").splitlines())
        except OSError:
            pass

    def _builtin(self):
        if self._base is None:
            try:
                data = (Path(__file__).with_name("chat-blocklist.dat")).read_bytes()
                self._base = self._parse(zlib.decompress(base64.b64decode(data)).decode("utf-8").splitlines())
            except (OSError, ValueError, zlib.error):
                self._base = []
        return self._base

    def hits(self, text) -> bool:
        self._load()
        terms = self._terms + (self._builtin() if self.builtin is None or self.builtin() else [])
        if not terms:
            return False
        words, joined = _tokens(text)
        for term, tw, mode in terms:
            if mode == "any":
                if term in joined:
                    return True
            elif mode == "start":
                if any(w.startswith(term) for w in words):
                    return True
            elif mode == "end":
                if any(w.endswith(term) for w in words):
                    return True
            elif any(words[i:i + len(tw)] == tw for i in range(len(words) - len(tw) + 1)):
                return True
        return False

    def text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return HEADER

    def save(self, text: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(text)[:20000], encoding="utf-8")
        self._stamp = -1


class Chat:
    """One chat room per printer, kept in memory: everyone watching a printer through a share link sees the same messages."""

    def __init__(self, blocklist: Blocklist | None = None):
        self.block = blocklist
        self.flags: dict[int, deque] = {}
        self.rooms: dict[int, deque] = {}
        self.next: dict[int, int] = {}
        self.epoch: dict[int, int] = {}
        self.sent: dict[tuple, float] = {}
        self.recent: dict[int, deque] = {}
        self.cool: dict[tuple, float] = {}

    def _refuse(self, pid: int, name, text, cid, now: float):
        """A blocked word: the viewer waits 10 seconds, and the owner gets to see what was tried."""
        self.cool[(pid, re.sub(r"[^A-Za-z0-9_-]", "", str(cid))[:40])] = now + BLOCK_WAIT
        if len(self.cool) > 2000:
            self.cool = {k: t for k, t in self.cool.items() if t > now}
        mid = self.next.get(pid, 0) + 1
        self.next[pid] = mid
        self.rooms.setdefault(pid, deque(maxlen=KEEP)).append(
            {"id": mid, "name": _clean(name, MAX_NAME) or "Viewer", "text": _clean(text, MAX_TEXT), "time": now,
             "owner": False, "kind": "blocked"})
        raise ValueError(BLOCKED)

    def _wait(self, pid: int, cid, now: float):
        left = self.cool.get((pid, re.sub(r"[^A-Za-z0-9_-]", "", str(cid))[:40]), 0.0) - now
        if left > 0:
            raise ValueError(f"Wait {int(left) + 1} seconds before trying again.")

    def post(self, pid: int, name: str, text: str, cid: str = "", owner: bool = False) -> dict:
        text = _clean(text, MAX_TEXT)
        if not text:
            raise ValueError("Type a message first.")
        now = time.time()
        if not owner:
            self._wait(pid, cid, now)
            if self.block and (self.block.hits(text) or self.block.hits(name)):
                self._refuse(pid, name, text, cid, now)
        if not owner:
            key = (pid, re.sub(r"[^A-Za-z0-9_-]", "", str(cid))[:40])
            if now - self.sent.get(key, 0.0) < VIEWER_GAP:
                raise ValueError("Slow down a little.")
            room = self.recent.setdefault(pid, deque())
            while room and now - room[0] > 60:
                room.popleft()
            if len(room) >= ROOM_PER_MINUTE:
                raise ValueError("The chat is very busy, try again in a moment.")
            room.append(now)
            self.sent[key] = now
            if len(self.sent) > 2000:
                self.sent = {k: t for k, t in self.sent.items() if now - t < 60}
        mid = self.next.get(pid, 0) + 1
        self.next[pid] = mid
        msg = {"id": mid, "name": "Owner" if owner else (_clean(name, MAX_NAME) or "Viewer"), "text": text,
               "time": now, "owner": owner, "kind": "msg"}
        self.rooms.setdefault(pid, deque(maxlen=KEEP)).append(msg)
        return msg

    def flag(self, pid: int, name: str, note: str = "", cid: str = "") -> dict:
        """A viewer asks the owner to look at the printer. Limited to one a minute each and a few at a time."""
        note = _clean(note, 100)
        now = time.time()
        self._wait(pid, cid, now)
        if self.block and (self.block.hits(note) or self.block.hits(name)):
            self._refuse(pid, name, note, cid, now)
        key = (pid, "flag:" + re.sub(r"[^A-Za-z0-9_-]", "", str(cid))[:40])
        if now - self.sent.get(key, 0.0) < FLAG_GAP:
            raise ValueError("You already flagged this a moment ago.")
        room = self.flags.setdefault(pid, deque())
        while room and now - room[0] > 600:
            room.popleft()
        if len(room) >= ROOM_FLAGS:
            raise ValueError("The owner has been flagged several times already.")
        room.append(now)
        self.sent[key] = now
        mid = self.next.get(pid, 0) + 1
        self.next[pid] = mid
        msg = {"id": mid, "name": _clean(name, MAX_NAME) or "Viewer", "text": note or "wants you to look at the printer",
               "time": now, "owner": False, "kind": "flag"}
        self.rooms.setdefault(pid, deque(maxlen=KEEP)).append(msg)
        return msg

    def since(self, pid: int, after: int = 0, owner: bool = False) -> dict:
        return {"messages": [m for m in self.rooms.get(pid, ()) if m["id"] > after and (owner or m["kind"] != "blocked")],
                "last": self.next.get(pid, 0), "epoch": self.epoch.get(pid, 0)}

    def clear(self, pid: int):
        self.rooms.pop(pid, None)
        self.epoch[pid] = self.epoch.get(pid, 0) + 1
