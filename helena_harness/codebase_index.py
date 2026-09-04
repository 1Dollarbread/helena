"""A lightweight, per-project semantic index over the workspace's own files.

The same idea as memory.py (embed, store, cosine-search) but scoped to one
project's source instead of cross-project facts about the user, and kept
fresh incrementally by mtime rather than rebuilt from scratch — a full
re-embed of a real codebase on every turn would be far too slow on local
hardware to ever actually get used.

Chunking is deliberately simple (fixed-size line windows, not AST-aware):
good enough for "find the code that does X" recall, and it costs nothing
extra to maintain across languages.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .tools.base import iter_files, looks_binary

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .tools.base import ToolContext

CHUNK_LINES = 60
CHUNK_OVERLAP = 10
MAX_FILE_BYTES = 300_000       # skip anything bigger — not worth indexing, and
                                # slow to embed in chunks on a small local model
MAX_FILES_PER_REFRESH = 400    # bound one incremental pass; the rest catch up
                                # on the next search_codebase call

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    path        TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   TEXT NOT NULL,
    mtime       REAL NOT NULL,
    PRIMARY KEY (path, chunk_index)
);
"""


def _db_path(ctx: "ToolContext") -> Path:
    path = ctx.config.project_dir / "codebase_index.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect(ctx: "ToolContext") -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(ctx)))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _chunk(text: str) -> list[tuple[int, int, str]]:
    lines = text.splitlines()
    if not lines:
        return []
    chunks: list[tuple[int, int, str]] = []
    step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
    for start in range(0, len(lines), step):
        end = min(len(lines), start + CHUNK_LINES)
        body = "\n".join(lines[start:end])
        if body.strip():
            chunks.append((start + 1, end, body))
        if end >= len(lines):
            break
    return chunks


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def ensure_fresh(ctx: "ToolContext") -> tuple[int, int]:
    """Incrementally (re)index changed files. Returns (files_updated, files_removed)."""
    conn = _connect(ctx)
    try:
        known: dict[str, float] = {
            row["path"]: row["mtime"]
            for row in conn.execute("SELECT path, MAX(mtime) as mtime FROM chunks GROUP BY path")
        }
        on_disk: set[str] = set()
        to_update: list[Path] = []
        for path in iter_files(ctx.workspace):
            if looks_binary(path):
                continue
            try:
                size = path.stat().st_size
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if size > MAX_FILE_BYTES or size == 0:
                continue
            rel_path = str(path.relative_to(ctx.workspace))
            on_disk.add(rel_path)
            if known.get(rel_path) is None or mtime > known[rel_path] + 0.001:
                to_update.append(path)
            if len(to_update) >= MAX_FILES_PER_REFRESH:
                break

        removed = [p for p in known if p not in on_disk]
        for rel_path in removed:
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel_path,))

        updated = 0
        for path in to_update:
            rel_path = str(path.relative_to(ctx.workspace))
            try:
                text = path.read_text("utf-8", errors="replace")
            except OSError:
                continue
            chunks = _chunk(text)
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel_path,))
            if not chunks:
                continue
            try:
                vectors = await ctx.client.embed(
                    [c[2] for c in chunks], model=ctx.config.embed_model or None
                )
            except Exception:
                continue  # best-effort — a server hiccup skips this file, not the whole refresh
            if not vectors:
                continue
            mtime = path.stat().st_mtime
            for i, ((start, end, body), vector) in enumerate(zip(chunks, vectors)):
                conn.execute(
                    "INSERT INTO chunks (path, chunk_index, start_line, end_line, text, embedding, mtime) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (rel_path, i, start, end, body, json.dumps(vector), mtime),
                )
            updated += 1
        conn.commit()
        return updated, len(removed)
    finally:
        conn.close()


def search(ctx: "ToolContext", query_embedding: list[float], top_k: int = 8) -> list[dict[str, Any]]:
    conn = _connect(ctx)
    try:
        rows = conn.execute("SELECT path, start_line, end_line, text, embedding FROM chunks").fetchall()
    finally:
        conn.close()
    scored = []
    for row in rows:
        try:
            vec = json.loads(row["embedding"])
        except json.JSONDecodeError:
            continue
        score = _cosine(query_embedding, vec)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "path": r["path"], "start_line": r["start_line"], "end_line": r["end_line"],
            "text": r["text"], "score": round(s, 3),
        }
        for s, r in scored[:top_k]
    ]
