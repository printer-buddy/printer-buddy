from __future__ import annotations

import re
import time
from collections import deque

MAX_TEXT = 300
MAX_NAME = 24
KEEP = 100
VIEWER_GAP = 2.0
ROOM_PER_MINUTE = 30
_SPACE = re.compile(r"[\x00-\x20\x7f​-‏  ]+")


def _clean(s, limit: int) -> str:
    return _SPACE.sub(" ", str(s or "")).strip()[:limit]


class Chat:
    """One chat room per printer, kept in memory: everyone watching a printer through a share link sees the same messages."""

    def __init__(self):
        self.rooms: dict[int, deque] = {}
        self.next: dict[int, int] = {}
        self.epoch: dict[int, int] = {}
        self.sent: dict[tuple, float] = {}
        self.recent: dict[int, deque] = {}

    def post(self, pid: int, name: str, text: str, cid: str = "", owner: bool = False) -> dict:
        text = _clean(text, MAX_TEXT)
        if not text:
            raise ValueError("Type a message first.")
        now = time.time()
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
               "time": now, "owner": owner}
        self.rooms.setdefault(pid, deque(maxlen=KEEP)).append(msg)
        return msg

    def since(self, pid: int, after: int = 0) -> dict:
        return {"messages": [m for m in self.rooms.get(pid, ()) if m["id"] > after],
                "last": self.next.get(pid, 0), "epoch": self.epoch.get(pid, 0)}

    def clear(self, pid: int):
        self.rooms.pop(pid, None)
        self.epoch[pid] = self.epoch.get(pid, 0) + 1
