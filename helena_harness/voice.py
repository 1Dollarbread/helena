"""Speech-to-text input, fully local via faster-whisper — no API key, no
per-request cost, no audio ever leaves the machine.

Optional: needs `pip install -e ".[voice]"`. Those dependencies (a whisper
model, sounddevice for microphone capture) are meaningfully heavier than the
rest of this project, so they're not installed by default — check_available()
gives a clear, actionable message rather than a raw ImportError if they're
missing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys

SAMPLE_RATE = 16000  # what whisper's models expect

_PACKAGE_NAMES = {"sounddevice": "sounddevice", "faster_whisper": "faster-whisper"}

INSTALL_HINT = 'Voice input needs extra packages. Run: pip install -e ".[voice]"'


class VoiceError(Exception):
    pass


def check_available() -> str | None:
    """Returns None if voice input is ready to use, otherwise an explanation
    of what's missing and how to fix it.

    Uses find_spec rather than a real import specifically so this check has
    no side effects — actually importing sounddevice touches the system's
    audio subsystem, which isn't something a mere availability check should
    be doing.

    The #1 reason someone sees this message *after* already running the
    install is that the install went into a different Python than the one
    `helena` is actually running from right now — a very easy trap with venvs
    and multiple terminal tabs. So the message names that Python explicitly
    (`sys.executable`) rather than just repeating the install command, since
    "I already did that" is almost always true — just in the wrong place.
    """
    missing = [name for name in _PACKAGE_NAMES if importlib.util.find_spec(name) is None]
    if not missing:
        return None

    friendly = ", ".join(_PACKAGE_NAMES[name] for name in missing)
    return (
        f"Voice input needs {friendly}, which isn't installed in the Python HELENA is "
        f"running from right now:\n"
        f"    {sys.executable}\n"
        "If you already ran `pip install -e \".[voice]\"` and are still seeing this, that "
        "install almost certainly went into a different Python — check with `which python` "
        "in the terminal where you ran it and compare to the path above. Fix:\n"
        "    cd path/to/this/project\n"
        "    source .venv/bin/activate\n"
        '    pip install -e ".[voice]"\n'
        "Then start a brand-new `helena` — a process that's already running keeps whatever "
        "packages were available when it started, so re-running /voice in this same session "
        "won't pick up a fresh install. `/voice-setup` shows this same diagnosis anytime."
    )


_model_cache: dict[str, object] = {}


def _get_model(model_size: str):
    """Whisper models are slow to load — cache by size so repeated /voice
    calls in one session only pay that cost once."""
    if model_size not in _model_cache:
        from faster_whisper import WhisperModel

        _model_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _model_cache[model_size]


async def record_until_enter(on_start=None):
    """Records from the default microphone until the user presses Enter.

    Recording happens via sounddevice's callback API (its own background
    thread); waiting for the keypress happens in an executor so the asyncio
    event loop stays free the whole time rather than blocking on input().
    Returns a 1-D float32 numpy array of the recorded audio.
    """
    import numpy as np
    import sounddevice as sd

    frames: list[np.ndarray] = []

    def callback(indata, frame_count, time_info, status):
        frames.append(indata.copy())

    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=callback)
    stream.start()
    try:
        if on_start:
            on_start()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, input)
    finally:
        stream.stop()
        stream.close()

    if not frames:
        return np.zeros(0, dtype="float32")
    return np.concatenate(frames, axis=0).flatten()


async def transcribe(audio, model_size: str = "base") -> str:
    """Runs whisper transcription in an executor — CPU-bound and would
    otherwise block the event loop for however long it takes."""
    if len(audio) == 0:
        return ""

    loop = asyncio.get_running_loop()

    def _run() -> str:
        model = _get_model(model_size)
        segments, _ = model.transcribe(audio, language="en")
        return " ".join(seg.text.strip() for seg in segments).strip()

    return await loop.run_in_executor(None, _run)
