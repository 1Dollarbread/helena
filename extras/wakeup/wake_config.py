"""Shared tuning knobs for the clap listener.

`clap_listener.py` runs as its own long-lived process (usually a launchd
agent, see `com.helena.wake.plist`) — it isn't part of the same Python
process as the harness, so there's no in-memory object the harness could
just reach into. The file at `~/.helena/wake.json` is the handoff point:
the harness's `/wake-config` command writes it, and the listener polls its
mtime so a running listener picks up new values without a restart.

Kept dependency-free (stdlib only) so importing this module never requires
the `voice` extra that `clap_listener.py` itself needs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

WAKE_CONFIG_PATH = Path(os.path.expanduser("~")) / ".helena" / "wake.json"

DEFAULTS: dict[str, float] = {
    "threshold": 0.35,      # RMS level (0-1 float audio) that counts as "loud"
    "refractory_s": 0.15,   # ignore new spikes for this long after one fires
    "clap_window_s": 1.2,   # both claps must land inside this window
    "cooldown_s": 4.0,      # after a successful wake, ignore audio for this long
}

FIELDS = tuple(DEFAULTS.keys())


def load_wake_config() -> dict[str, float]:
    """Defaults merged with whatever's on disk. Never raises — a broken or
    missing file just means defaults, same policy as Config._merge_file."""
    values = dict(DEFAULTS)
    if WAKE_CONFIG_PATH.is_file():
        try:
            data = json.loads(WAKE_CONFIG_PATH.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return values
        for key in FIELDS:
            if key in data:
                try:
                    values[key] = float(data[key])
                except (TypeError, ValueError):
                    continue
    return values


def save_wake_config(values: dict[str, float]) -> Path:
    WAKE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged = load_wake_config()
    merged.update({k: v for k, v in values.items() if k in FIELDS})
    WAKE_CONFIG_PATH.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return WAKE_CONFIG_PATH


class ReloadingWakeConfig:
    """Live-reloading view of the config for the listener's callback loop.

    Checking the file on every audio block (30ms) would mean constant stat()
    calls from the audio thread; instead this only re-checks mtime every
    `poll_interval` seconds and re-reads the file if it changed.
    """

    def __init__(self, poll_interval: float = 2.0) -> None:
        self.poll_interval = poll_interval
        self._values = load_wake_config()
        self._mtime = self._current_mtime()
        self._last_poll = time.monotonic()

    def _current_mtime(self) -> float:
        try:
            return WAKE_CONFIG_PATH.stat().st_mtime
        except OSError:
            return 0.0

    def get(self) -> dict[str, float]:
        now = time.monotonic()
        if now - self._last_poll >= self.poll_interval:
            self._last_poll = now
            mtime = self._current_mtime()
            if mtime != self._mtime:
                self._mtime = mtime
                old = dict(self._values)
                self._values = load_wake_config()
                if self._values != old:
                    print(f"[wake] config reloaded: {self._values}")
        return self._values


def format_config(values: dict[str, float] | None = None) -> str:
    values = values or load_wake_config()
    return (
        f"threshold={values['threshold']}  "
        f"refractory_s={values['refractory_s']}  "
        f"clap_window_s={values['clap_window_s']}  "
        f"cooldown_s={values['cooldown_s']}"
    )
