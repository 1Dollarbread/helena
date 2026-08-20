"""Text-to-speech via ElevenLabs.

Deliberately the one part of this project that isn't free or local — there's
no local TTS engine that sounds like an actual voice yet, and the user asked
for a real one specifically. Needs an ElevenLabs account and API key (see the
README's Voice section for exact setup steps). Everything else about HELENA
still works with zero cost and zero external calls if you never touch this.

Playback shells out to macOS's `afplay`, consistent with how the desktop
tools already work — no extra audio-playback dependency needed.
"""

from __future__ import annotations

import asyncio
import platform
import tempfile
from pathlib import Path

import httpx

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # "Rachel" — one of ElevenLabs' stock voices


class TTSError(Exception):
    pass


async def synthesize(text: str, api_key: str, voice_id: str | None = None, model_id: str = "eleven_turbo_v2_5") -> bytes:
    """Returns raw MP3 bytes for `text`, spoken in `voice_id`."""
    if not api_key:
        raise TTSError('No ElevenLabs API key configured. Set ELEVENLABS_API_KEY — see "/voice-setup" for the full steps.')
    if not text.strip():
        raise TTSError("Nothing to speak — empty text.")

    voice_id = voice_id or DEFAULT_VOICE_ID
    url = f"{ELEVENLABS_API_BASE}/text-to-speech/{voice_id}"

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            res = await client.post(
                url,
                headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                json={
                    "text": text,
                    "model_id": model_id,
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
            )
        except httpx.HTTPError as exc:
            raise TTSError(f"Could not reach ElevenLabs: {exc}") from exc

    if res.status_code == 401:
        raise TTSError("ElevenLabs rejected the API key (401 unauthorized) — check ELEVENLABS_API_KEY.")
    if res.status_code == 404:
        raise TTSError(f'Voice ID "{voice_id}" wasn\'t found (404) — check ELEVENLABS_VOICE_ID.')
    if res.status_code != 200:
        raise TTSError(f"ElevenLabs request failed ({res.status_code}): {res.text[:200]}")
    return res.content


async def play(audio: bytes) -> None:
    """Plays MP3 bytes through the system's default output device."""
    if platform.system() != "Darwin":
        raise TTSError("Audio playback is only wired up for macOS right now (afplay).")

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(audio)
        path = Path(f.name)
    try:
        proc = await asyncio.create_subprocess_exec(
            "afplay", str(path), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise TTSError(f"afplay failed: {stderr.decode('utf-8', 'replace').strip()}")
    finally:
        path.unlink(missing_ok=True)


async def speak(text: str, api_key: str, voice_id: str | None = None) -> None:
    """Synthesize and play in one call — what /say and auto-speak both use."""
    audio = await synthesize(text, api_key, voice_id)
    await play(audio)
