"""Tiny SQLite-backed key/value store (single-user).

For now the only persisted state is the encrypted Telegram session string.
Later milestones (runs, drafts, audit log) will add their own tables here.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_path: str | None = None


def init(path: str) -> None:
    global _path
    _path = path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
            "target_id INTEGER, target_handle TEXT, group_id INTEGER, "
            "group_title TEXT, action TEXT, ok INTEGER, detail TEXT)"
        )
        conn.commit()


def _connect() -> sqlite3.Connection:
    if _path is None:
        raise RuntimeError("db.init() must be called before use")
    return sqlite3.connect(_path)


def get(key: str) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def set(key: str, value: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def delete(key: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        conn.commit()


def audit_add(
    *, target_id: int | None, target_handle: str | None, group_id: int,
    group_title: str, action: str, ok: bool, detail: str | None,
) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO audit (ts, target_id, target_handle, group_id, group_title, "
            "action, ok, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, target_id, target_handle, group_id, group_title, action, int(ok), detail),
        )
        conn.commit()


def audit_list(limit: int = 50) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT ts, target_handle, group_title, action, ok, detail "
            "FROM audit ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"ts": r[0], "target_handle": r[1], "group_title": r[2],
         "action": r[3], "ok": bool(r[4]), "detail": r[5]}
        for r in rows
    ]
