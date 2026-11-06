from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS printers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    host       TEXT NOT NULL,
    port       INTEGER NOT NULL DEFAULT 7125,
    api_key    TEXT,
    created_at REAL NOT NULL,
    UNIQUE(host, port)
);
CREATE TABLE IF NOT EXISTS shares (
    code       TEXT PRIMARY KEY,
    printer_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    until_print INTEGER NOT NULL DEFAULT 0,
    chat INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(printers)")}
        with self.db:
            if "grp" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN grp TEXT")
            if "position" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN position INTEGER")
            if "cam" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN cam TEXT")
            if "notify" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN notify INTEGER")
            if "ai" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN ai TEXT")
            if "aibox" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN aibox TEXT")
            if "aiarea" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN aiarea TEXT")
            if "kind" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN kind TEXT NOT NULL DEFAULT 'moonraker'")
            if "extra" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN extra TEXT")
            if "until_print" not in {r["name"] for r in self.db.execute("PRAGMA table_info(shares)")}:
                self.db.execute("ALTER TABLE shares ADD COLUMN until_print INTEGER NOT NULL DEFAULT 0")
            if "chat" not in {r["name"] for r in self.db.execute("PRAGMA table_info(shares)")}:
                self.db.execute("ALTER TABLE shares ADD COLUMN chat INTEGER NOT NULL DEFAULT 1")

    def list(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM printers ORDER BY id")]

    def get(self, pid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM printers WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

    def find(self, host: str, port: int) -> dict | None:
        r = self.db.execute("SELECT * FROM printers WHERE host=? AND port=?", (host, port)).fetchone()
        return dict(r) if r else None

    def add(self, name: str, host: str, port: int, api_key: str | None, kind: str = "moonraker", extra: dict | None = None) -> dict:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO printers(name, host, port, api_key, created_at, kind, extra) VALUES (?,?,?,?,?,?,?)",
                (name, host, port, api_key or None, time.time(), kind, json.dumps(extra) if extra else None))
        return self.get(cur.lastrowid)

    def set_native(self, pid: int, kind: str, host: str, port: int, extra: dict):
        with self.db:
            self.db.execute("UPDATE printers SET kind=?, host=?, port=?, extra=?, api_key=NULL WHERE id=?",
                            (kind, host, port, json.dumps(extra), pid))

    def update(self, pid: int, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("name", "api_key", "grp", "position", "cam", "notify", "ai", "aibox", "aiarea")}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)
        with self.db:
            self.db.execute(f"UPDATE printers SET {sets} WHERE id=?", (*allowed.values(), pid))

    def set_order(self, ids: list[int]):
        with self.db:
            self.db.executemany("UPDATE printers SET position=? WHERE id=?", [(i, pid) for i, pid in enumerate(ids)])

    def settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT key, value FROM settings")}

    def save_settings(self, values: dict):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                                [(k, json.dumps(v)) for k, v in values.items()])

    def add_share(self, code: str, pid: int, expires_at: float | None, until_print: bool = False, chat: bool = True) -> dict:
        with self.db:
            self.db.execute("INSERT INTO shares(code, printer_id, created_at, expires_at, until_print, chat) VALUES (?,?,?,?,?,?)",
                            (code, pid, time.time(), expires_at, 1 if until_print else 0, 1 if chat else 0))
        return self.get_share(code)

    def get_share(self, code: str) -> dict | None:
        r = self.db.execute("SELECT * FROM shares WHERE code=?", (code,)).fetchone()
        return dict(r) if r else None

    def list_shares(self, pid: int) -> list[dict]:
        now = time.time()
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM shares WHERE printer_id=? AND revoked=0 AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at DESC", (pid, now))]

    def end_print_shares(self, pid: int):
        with self.db:
            self.db.execute("UPDATE shares SET revoked=1 WHERE printer_id=? AND until_print=1 AND revoked=0", (pid,))

    def revoke_share(self, code: str) -> bool:
        with self.db:
            return self.db.execute("UPDATE shares SET revoked=1 WHERE code=? AND revoked=0", (code,)).rowcount > 0

    def delete(self, pid: int):
        with self.db:
            self.db.execute("DELETE FROM shares WHERE printer_id=?", (pid,))
            self.db.execute("DELETE FROM printers WHERE id=?", (pid,))
