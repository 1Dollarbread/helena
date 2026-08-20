"""Local transcript persistence.

The server can store sessions too, but the harness keeps its own copy under
`.helena/sessions/` so a conversation is recoverable from the project itself —
including the tool calls, which are what you actually want when reconstructing
what an agent did.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class Transcript:
    id: str
    path: Path
    created_at: str
    messages: list[dict[str, Any]]
    model: str = ""
    title: str = ""

    @classmethod
    def new(cls, directory: Path, model: str = "") -> "Transcript":
        directory.mkdir(parents=True, exist_ok=True)
        sid = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        return cls(
            id=sid,
            path=directory / f"{sid}.json",
            created_at=datetime.now().isoformat(timespec="seconds"),
            messages=[],
            model=model,
        )

    def save(self, messages: list[dict[str, Any]], totals: dict[str, Any] | None = None) -> Path:
        self.messages = messages
        payload = {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "model": self.model,
            "title": self.title or self._derive_title(messages),
            "totals": totals or {},
            # Images are megabytes of base64 that nobody reads back; the
            # transcript keeps a marker instead.
            "messages": [_strip_images(m) for m in messages],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return self.path

    @staticmethod
    def _derive_title(messages: list[dict[str, Any]]) -> str:
        for msg in messages:
            if msg.get("role") == "user" and msg.get("content"):
                text = " ".join(str(msg["content"]).split())
                return text[:70] + ("…" if len(text) > 70 else "")
        return "(empty)"

    @classmethod
    def load(cls, path: Path) -> "Transcript":
        data = json.loads(path.read_text("utf-8"))
        return cls(
            id=data.get("id", path.stem),
            path=path,
            created_at=data.get("created_at", ""),
            messages=data.get("messages", []),
            model=data.get("model", ""),
            title=data.get("title", ""),
        )


def _strip_images(message: dict[str, Any]) -> dict[str, Any]:
    if not message.get("images"):
        return message
    copy = dict(message)
    copy["images"] = [f"<image omitted: {len(img)} base64 chars>" for img in message["images"]]
    return copy


def list_transcripts(directory: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    rows = []
    for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rows.append({
            "id": data.get("id", path.stem),
            "title": data.get("title", ""),
            "messages": len(data.get("messages", [])),
            "updated_at": data.get("updated_at", ""),
            "path": str(path),
            "age": _age(path.stat().st_mtime),
        })
    return rows


def _age(mtime: float) -> str:
    delta = time.time() - mtime
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    if delta < 86400:
        return f"{delta / 3600:.0f}h ago"
    return f"{delta / 86400:.0f}d ago"


def find_transcript(directory: Path, needle: str) -> Path | None:
    """Resolve a session id, a prefix of one, or 'last'."""
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    if needle in ("last", "latest", ""):
        return candidates[0]
    for path in candidates:
        if path.stem == needle or path.stem.startswith(needle):
            return path
    return None
