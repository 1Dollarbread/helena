"""Thin client for the barehands hand-tracked board (github.com/jaredrhod/barehands).

barehands is a separate, standalone project HELENA talks to over localhost —
nothing here ever reaches outside 127.0.0.1, the same trust boundary as the
Ollama server HELENA already talks to via client.py. Two things live here:

  - the board protocol (stage cards/images/models, read what's on the glass),
    used by tools/board.py
  - the ring-state writer, which makes the board's on-screen ring mirror
    HELENA's own live state — the same file-based mechanism barehands.md
    documents for wiring in Claude Code hooks, just driven from inside the
    REPL loop instead of an external hook script.

See README.md's "Bare hands" section for the full setup story, and
barehands.md (inside a barehands checkout) for the protocol this mirrors.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from .config import Config

# Must match server.py's _ALLOWED tuple exactly — the server is the real
# enforcement point, but validating here too means a bad action name fails
# fast with a clear message instead of a bare HTTP 400.
ALLOWED_ACTIONS = (
    "add_img", "add_card", "clear", "reset", "hand", "give",
    "yank", "hover", "scroll_note", "widget", "explode", "assemble", "present",
)

# Extensions the media airlock in server.py will actually stage.
MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".webm", ".glb", ".gltf"}

RING_STATES = ("idle", "listening", "thinking", "speaking")


class BarehandsError(Exception):
    pass


def repo_path(config: Config) -> Path | None:
    if not config.barehands_path:
        return None
    return Path(config.barehands_path).expanduser()


def is_configured(config: Config) -> bool:
    path = repo_path(config)
    return path is not None and (path / "server.py").is_file()


def not_configured_message() -> str:
    return (
        "barehands isn't set up yet. Run /barehands-setup to clone it, start its server, "
        "and point HELENA at it — or if it's already cloned somewhere, set barehands_path "
        "in .helena/settings.json (or the HELENA_BAREHANDS_PATH environment variable) to "
        "that folder."
    )


def media_root(config: Config) -> Path:
    path = repo_path(config)
    if path is None:
        raise BarehandsError(not_configured_message())
    return (path / "media").resolve()


def state_dir(config: Config) -> Path:
    path = repo_path(config)
    if path is None:
        raise BarehandsError(not_configured_message())
    return path / "state"


def write_ring_state(config: Config, state: str) -> None:
    """Make the board's ring reflect HELENA's live state.

    Best-effort and silent on failure — a missing or misconfigured board
    should never break the actual conversation, same spirit as the TTS
    best-effort call around tts.speak() in repl.py. Called automatically by
    the REPL around each turn, /voice recording, and speaking a reply; never
    something the model itself needs to call.
    """
    if state not in RING_STATES or not is_configured(config):
        return
    try:
        s_dir = state_dir(config)
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / "state").write_text(state, encoding="utf-8")
    except OSError:
        pass


def write_wave(config: Config, samples: list[float]) -> None:
    """Optional: feeds the ring's waveform display while HELENA is speaking.
    Silently skipped if barehands isn't configured — same best-effort spirit
    as write_ring_state."""
    if not is_configured(config):
        return
    try:
        s_dir = state_dir(config)
        s_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"samples": list(samples)[:64], "ts": time.time()})
        (s_dir / "wave.json").write_text(payload, encoding="utf-8")
    except OSError:
        pass


async def post_command(config: Config, command: dict[str, Any]) -> tuple[int, str]:
    """POST one board command to /cmd. Returns (http_status, response_body)."""
    if not is_configured(config):
        raise BarehandsError(not_configured_message())
    action = command.get("a")
    if action not in ALLOWED_ACTIONS:
        raise BarehandsError(
            f"{action!r} isn't a real barehands action. Allowed: {', '.join(ALLOWED_ACTIONS)}."
        )
    url = f"{config.barehands_url}/cmd"
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=command, timeout=5.0)
        except httpx.HTTPError as exc:
            raise BarehandsError(
                f"Couldn't reach the barehands server at {url} ({exc}). Is it running? "
                "/barehands-setup checks and (re)starts it if not."
            ) from exc
    return res.status_code, res.text


async def get_state(config: Config) -> dict[str, Any] | None:
    """The tracker's last scene heartbeat — what's actually on the board right
    now, straight from the board's own truth rather than memory. None means
    the server is up but no tracker page has connected, or the response
    wasn't valid JSON."""
    if not is_configured(config):
        raise BarehandsError(not_configured_message())
    url = f"{config.barehands_url}/state"
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, timeout=5.0)
        except httpx.HTTPError as exc:
            raise BarehandsError(
                f"Couldn't reach the barehands server at {url} ({exc}). Is it running?"
            ) from exc
    try:
        return res.json() or None
    except ValueError:
        return None


async def server_alive(config: Config) -> bool:
    """Cheap liveness check used by /barehands-setup to decide whether to
    start a new server or leave an existing one alone."""
    if not is_configured(config):
        return False
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(f"{config.barehands_url}/config", timeout=2.0)
            return res.status_code == 200
    except httpx.HTTPError:
        return False


def describe_state(state: dict[str, Any] | None) -> str:
    """Mirrors bin/board-state.sh's own rendering, so HELENA's eyes on the
    board (the board_state tool) describe it exactly the way the shell
    script barehands.md teaches an assistant to use would."""
    if not state:
        return "The board is empty, or no tracker page has connected yet."
    items = state.get("items") or []
    if not items:
        return "The board is EMPTY (as of the tracker's last heartbeat)."

    def zone(x: float, y: float) -> str:
        h = "left" if x < 0.33 else ("center" if x < 0.67 else "right")
        v = "top" if y < 0.33 else ("middle" if y < 0.67 else "bottom")
        return "center" if (h == "center" and v == "middle") else f"{v}-{h}"

    lines = [f"ON THE BOARD — {len(items)} item(s), last tracker heartbeat:"]
    for item in items:
        t = item.get("type", "?")
        title = item.get("title") or ""
        src = Path(item.get("src") or "").name
        if t == "card":
            body = (item.get("body") or "").replace("\n", " ")[:70]
            desc = f'card "{title}"' + (f" — {body}" if body else "")
        elif t == "img":
            desc = (
                f"image {src}"
                + (" (fx, frameless)" if item.get("fxf") else "")
                + (" (video)" if item.get("vd") else "")
            )
        elif t == "model":
            mode = "hologram wireframe" if item.get("mm") == "holo" else "solid"
            desc = f"3D model {src} ({mode})"
            ex = item.get("ex") or 0
            if ex > 0.02:
                desc += f", EXPLODED {round(ex * 100)}%"
        elif t == "panel":
            desc = f'open note "{title}"'
        elif t == "browser":
            desc = f'file browser "{title}"'
        elif t == "widget":
            desc = "the assistant ring"
        elif t == "orb":
            desc = f'orb "{title}"'
        else:
            desc = f'{t} "{title or src}"'
        flags = []
        if item.get("g"):
            flags.append("IN THE USER'S HAND")
        sc = item.get("scale") or 1
        if sc >= 1.6:
            flags.append("blown up large")
        elif sc <= 0.55:
            flags.append("shrunk small")
        op = item.get("op", 1)
        if op is not None and op < 0.5:
            flags.append("faded out")
        pos = zone(item.get("x") or 0.5, item.get("y") or 0.5)
        line = f"  - {desc} @ {pos}"
        if flags:
            line += "  [" + ", ".join(flags) + "]"
        lines.append(line)
    return "\n".join(lines)
