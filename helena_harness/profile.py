"""Durable user profile: facts, saved location, and reminders.

Kept from the original HELENA — the parts of it that were genuinely useful are
memory that survives restarts, and reminders that actually fire. Stored as JSON
at ~/.helena/profile.json, separate from settings.json so preferences and
personal data don't share a file.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import USER_DIR

PROFILE_PATH = USER_DIR / "profile.json"

DEFAULT_PROFILE: dict[str, Any] = {
    "version": 2,
    "location": {"label": None, "lat": None, "lon": None, "timezone": None},
    "units": "imperial",
    "facts": [],       # [{id, text, created_at}]
    "reminders": [],   # [{id, text, when_text, when_iso, notified, created_at}]
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_profile() -> dict[str, Any]:
    if not PROFILE_PATH.is_file():
        return json.loads(json.dumps(DEFAULT_PROFILE))
    try:
        data = json.loads(PROFILE_PATH.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT_PROFILE))
    merged = json.loads(json.dumps(DEFAULT_PROFILE))
    merged.update(data)
    # One level deep, so a profile written by an older version doesn't drop
    # keys added since.
    merged["location"] = {**DEFAULT_PROFILE["location"], **(data.get("location") or {})}
    return merged


def save_profile(profile: dict[str, Any]) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")


def add_fact(text: str) -> dict[str, Any]:
    profile = load_profile()
    fact = {"id": uuid.uuid4().hex[:8], "text": text, "created_at": _now_iso()}
    profile["facts"].append(fact)
    save_profile(profile)
    return fact


def add_reminder(text: str, when_text: str, when: datetime | None) -> dict[str, Any]:
    profile = load_profile()
    item = {
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "when_text": when_text,
        "when_iso": when.isoformat(timespec="seconds") if when else None,
        "notified": False,
        "created_at": _now_iso(),
    }
    profile["reminders"].append(item)
    save_profile(profile)
    return item


def pending_reminders() -> list[dict[str, Any]]:
    return [r for r in load_profile()["reminders"] if not r["notified"]]


def due_reminders(now: datetime | None = None) -> list[dict[str, Any]]:
    """Return reminders that have come due, marking them fired immediately.

    Marking before returning (and persisting straight away) is what keeps a
    reminder from firing twice, including across restarts.
    """
    now = now or datetime.now()
    profile = load_profile()
    due = []
    for item in profile["reminders"]:
        if item["notified"] or not item["when_iso"]:
            continue
        try:
            when = datetime.fromisoformat(item["when_iso"])
        except ValueError:
            continue
        if when <= now:
            item["notified"] = True
            due.append(item)
    if due:
        save_profile(profile)
    return due


def set_location(label: str, lat: float, lon: float, timezone: str | None) -> None:
    profile = load_profile()
    profile["location"] = {"label": label, "lat": lat, "lon": lon, "timezone": timezone}
    save_profile(profile)


def context_summary() -> str:
    """A compact block of profile context for the system prompt."""
    profile = load_profile()
    lines: list[str] = []
    if profile["location"].get("label"):
        lines.append(f"User location: {profile['location']['label']}")
    facts = profile.get("facts") or []
    if facts:
        lines.append("Things the user has told you to remember:")
        lines += [f"  - {f['text']}" for f in facts[-20:]]
    pending = [r for r in profile.get("reminders", []) if not r["notified"]]
    if pending:
        lines.append("Pending reminders:")
        lines += [f"  - {r['text']} ({r['when_text']})" for r in pending[:10]]
    return "\n".join(lines)


# --- natural-language time parsing -----------------------------------------


@dataclass
class ParsedTime:
    when: datetime | None
    note: str = ""


def _parse_clock(text: str, base: datetime) -> datetime | None:
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", text.strip(), re.IGNORECASE)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = (m.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """Parse the phrasings people actually use: "3pm", "in 20 minutes",
    "tomorrow at 9am", "2026-08-12 09:00"."""
    if not text:
        return None
    now = now or datetime.now()
    t = text.strip().lower()

    m = re.match(r"^in\s+(\d+)\s*(second|sec|minute|min|hour|hr|day)s?$", t)
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        delta = {
            "second": timedelta(seconds=amount), "sec": timedelta(seconds=amount),
            "minute": timedelta(minutes=amount), "min": timedelta(minutes=amount),
            "hour": timedelta(hours=amount), "hr": timedelta(hours=amount),
            "day": timedelta(days=amount),
        }[unit]
        return now + delta

    m = re.match(r"^tomorrow(?:\s+at\s+(.+))?$", t)
    if m:
        base = now + timedelta(days=1)
        if m.group(1):
            parsed = _parse_clock(m.group(1), base)
            if parsed:
                return parsed
        return base.replace(hour=9, minute=0, second=0, microsecond=0)

    m = re.match(r"^(?:at\s+)?(.+)$", t)
    if m:
        parsed = _parse_clock(m.group(1), now)
        if parsed:
            # A time that already passed today means the next one.
            return parsed + timedelta(days=1) if parsed <= now else parsed

    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        return None
