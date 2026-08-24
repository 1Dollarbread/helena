"""Cross-project long-term memory.

`profile.py` keeps a short, explicit list of facts that gets dumped into every
system prompt verbatim — fine for a handful of things, but it doesn't scale
and it has no sense of *which* facts matter to the question just asked. This
is the other half: a growing set of embedded notes about the user (patterns,
preferences, ongoing work, recurring frustrations), searched by cosine
similarity against the current turn so only what's actually relevant gets
recalled — from any project, not just this folder.

Sqlite (via the stdlib) plus whatever embedding model the server already runs
for `/v1/embeddings` — no new dependencies. A connection is opened per call
rather than held open, same tradeoff `profile.py` makes for its JSON file:
this is an interactive CLI, not a server under load, so the simplicity is
worth more than the microseconds saved.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .client import ServerClient, ServerError
from .config import USER_DIR

MEMORY_PATH = USER_DIR / "memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    project     TEXT,
    embedding   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEMORY_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save(text: str, embedding: list[float], project: str | None = None) -> dict[str, Any]:
    """Persist one memory. Returns the stored row (without the embedding)."""
    row = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "project": project,
        "created_at": _now_iso(),
    }
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO memories (id, text, project, embedding, created_at) VALUES (?,?,?,?,?)",
            (row["id"], row["text"], row["project"], json.dumps(embedding), row["created_at"]),
        )
        conn.commit()
    finally:
        conn.close()
    return row


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def search(query_embedding: list[float], top_k: int = 5, min_score: float = 0.35) -> list[dict[str, Any]]:
    """The `top_k` memories most similar to `query_embedding`, above `min_score`."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT id, text, project, created_at, embedding FROM memories").fetchall()
    finally:
        conn.close()
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        try:
            vec = json.loads(row["embedding"])
        except json.JSONDecodeError:
            continue
        score = _cosine(query_embedding, vec)
        if score >= min_score:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "id": r["id"], "text": r["text"], "project": r["project"],
            "created_at": r["created_at"], "score": round(s, 3),
        }
        for s, r in scored[:top_k]
    ]


def all_memories(limit: int = 200) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            # `rowid` (implicit even on a TEXT primary key) breaks ties within
            # the same second — created_at alone doesn't order two memories
            # saved back to back.
            "SELECT id, text, project, created_at FROM memories ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count() -> int:
    conn = _connect()
    try:
        return int(conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"])
    finally:
        conn.close()


def forget(memory_id: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


async def remember(client: ServerClient, embed_model: str, project: str | None, text: str) -> dict[str, Any] | None:
    """Embed `text` and persist it. Best-effort: a server that's down or an
    embedding model that isn't pulled yet should never lose the caller's own
    record of `text` (a profile fact, whatever prompted this) — it just means
    this particular note won't be recallable across projects until it is.
    """
    try:
        vectors = await client.embed([text], model=embed_model or None)
    except ServerError:
        return None
    if not vectors:
        return None
    return save(text, vectors[0], project=project)
