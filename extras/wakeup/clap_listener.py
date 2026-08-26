"""Two-clap wake detector for HELENA.

Fully offline, no wake word, no speech recognition — just amplitude-spike
detection on the raw mic stream, same as a clapper light switch. Requires the
project's `voice` extra (sounddevice + numpy), which most HELENA setups
already have for `/voice`:

    pip install -e ".[voice]"

Tuning knobs live in `~/.helena/wake.json`, not as constants in this file —
run `/wake-config` from inside HELENA to change them (see `wake_config.py`).
That file is polled every couple seconds, so a running listener picks up new
values without a restart. Deleting it, or a key inside it, falls back to the
defaults in `wake_config.py`. Clap detection is inherently a blunt
instrument — a dropped book or a door slam can trigger it. Two claps in a
tight window cuts false positives a lot; if it's still too sensitive, raise
`threshold` first, then tighten the window.

Usage:
    python clap_listener.py                      # listen forever
    python clap_listener.py --workspace ~/code/x  # debrief for a specific project
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

from wake_config import ReloadingWakeConfig, format_config

SAMPLE_RATE = 16_000
BLOCK_MS = 30
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_MS / 1000)

HERE = Path(__file__).parent


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block))))


def listen_for_double_clap(workspace: Path) -> None:
    wake_cfg = ReloadingWakeConfig()
    print(f"[wake] listening for two claps · {format_config(wake_cfg.get())}")
    last_spike = 0.0
    first_clap_at: float | None = None
    cooldown_until = 0.0

    def callback(indata, frames, time_info, status):  # noqa: ANN001 - sounddevice signature
        nonlocal last_spike, first_clap_at, cooldown_until
        cfg = wake_cfg.get()
        now = time.monotonic()
        if now < cooldown_until:
            return
        level = rms(indata[:, 0])
        if level < cfg["threshold"]:
            return
        if now - last_spike < cfg["refractory_s"]:
            return
        last_spike = now

        if first_clap_at is None:
            first_clap_at = now
            print("[wake] clap 1…")
            return

        if now - first_clap_at <= cfg["clap_window_s"]:
            print("[wake] clap 2 — waking HELENA")
            first_clap_at = None
            cooldown_until = now + cfg["cooldown_s"]
            trigger(workspace)
        else:
            # too slow — this spike becomes a fresh "clap 1" instead of being dropped
            first_clap_at = now
            print("[wake] clap 1…")

    with sd.InputStream(
        samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE, channels=1, dtype="float32", callback=callback
    ):
        while True:
            time.sleep(0.5)


def trigger(workspace: Path) -> None:
    """Runs the debrief in its own process so a crash there can't kill the listener."""
    subprocess.Popen(
        [sys.executable, str(HERE / "debrief.py"), "--workspace", str(workspace)],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Listen for two claps, then run HELENA's debrief.")
    parser.add_argument("--workspace", default=str(Path.cwd()), help="Project directory to debrief on wake.")
    args = parser.parse_args()
    try:
        listen_for_double_clap(Path(args.workspace).expanduser())
    except KeyboardInterrupt:
        print("\n[wake] stopped.")


if __name__ == "__main__":
    main()
