"""Sqlite-backed conversation store.

Sessions live server-side so a conversation survives restarts of the terminal
harness (and can be shared by more than one client). Sqlite via the stdlib
keeps the dependency surface small; calls are pushed to a worker thread so a
slow disk never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    model       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    payload     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = asyncio.Lock()

    def close(self) -> None:
        self._conn.close()

    # --- sync core (always called through _run) ----------------------------

    def _create(self, title: str | None, model: str | None, messages: list[dict[str, Any]]) -> str:
        sid = uuid.uuid4().hex[:16]
        ts = _now()
        self._conn.execute(
            "INSERT INTO sessions (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (sid, title, model, ts, ts),
        )
        self._conn.commit()
        if messages:
            self._append(sid, messages)
        return sid

    def _next_seq(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS s FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["s"]) + 1

    def _append(self, session_id: str, messages: list[dict[str, Any]]) -> int:
        exists = self._conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not exists:
            raise KeyError(session_id)
        seq = self._next_seq(session_id)
        ts = _now()
        rows = []
        for msg in messages:
            extra = {
                k: v for k, v in msg.items()
                if k not in ("role", "content") and v is not None
            }
            rows.append((
                session_id, seq, msg.get("role", "user"), msg.get("content") or "",
                json.dumps(extra) if extra else None, ts,
            ))
            seq += 1
        self._conn.executemany(
            "INSERT INTO messages (session_id, seq, role, content, payload, created_at)"
            " VALUES (?,?,?,?,?,?)",
            rows,
        )
        self._conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (ts, session_id))
        self._conn.commit()
        return len(rows)

    def _messages(self, session_id: str) -> list[dict[str, Any]]:
        if not self._conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
            raise KeyError(session_id)
        rows = self._conn.execute(
            "SELECT role, content, payload FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        out = []
        for row in rows:
            msg: dict[str, Any] = {"role": row["role"], "content": row["content"]}
            if row["payload"]:
                msg.update(json.loads(row["payload"]))
            out.append(msg)
        return out

    def _summary_row(self, row: sqlite3.Row) -> dict[str, Any]:
        count = self._conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (row["id"],)
        ).fetchone()["c"]
        return {
            "id": row["id"],
            "title": row["title"],
            "model": row["model"],
            "message_count": count,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _list(self, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._summary_row(r) for r in rows]

    def _get(self, session_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise KeyError(session_id)
        detail = self._summary_row(row)
        detail["messages"] = self._messages(session_id)
        return detail

    def _delete(self, session_id: str) -> None:
        cur = self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self._conn.commit()
        if cur.rowcount == 0:
            raise KeyError(session_id)

    def _set_title(self, session_id: str, title: str) -> None:
        cur = self._conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now(), session_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise KeyError(session_id)

    # --- async wrappers ----------------------------------------------------

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    async def create(self, title=None, model=None, messages=None) -> str:
        return await self._run(self._create, title, model, messages or [])

    async def append(self, session_id: str, messages: list[dict[str, Any]]) -> int:
        return await self._run(self._append, session_id, messages)

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return await self._run(self._messages, session_id)

    async def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._run(self._list, limit)

    async def get(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._get, session_id)

    async def delete(self, session_id: str) -> None:
        await self._run(self._delete, session_id)

    async def set_title(self, session_id: str, title: str) -> None:
        await self._run(self._set_title, session_id, title)
