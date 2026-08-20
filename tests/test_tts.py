"""ElevenLabs TTS: request construction, error mapping, and playback gating.
No real network calls or audio playback — httpx is mocked, afplay is never
actually invoked (platform-gated tests check the guard instead of the sound).
"""

from __future__ import annotations

import platform

import httpx
import pytest

from helena_harness.tts import DEFAULT_VOICE_ID, TTSError, play, synthesize


async def test_synthesize_requires_an_api_key():
    with pytest.raises(TTSError, match="No ElevenLabs API key"):
        await synthesize("hello", api_key="")


async def test_synthesize_requires_nonempty_text():
    with pytest.raises(TTSError, match="Nothing to speak"):
        await synthesize("   ", api_key="sk-test")


async def test_synthesize_sends_the_right_request(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key_header"] = request.headers.get("xi-api-key")
        seen["body"] = request.content
        return httpx.Response(200, content=b"FAKE_MP3_BYTES")

    _patch_httpx(monkeypatch, handler)
    audio = await synthesize("hello there", api_key="sk-test", voice_id="voice123")

    assert audio == b"FAKE_MP3_BYTES"
    assert seen["url"].endswith("/text-to-speech/voice123")
    assert seen["api_key_header"] == "sk-test"
    assert b"hello there" in seen["body"]


async def test_synthesize_defaults_to_stock_voice(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"x")

    _patch_httpx(monkeypatch, handler)
    await synthesize("hi", api_key="sk-test")

    assert seen["url"].endswith(f"/text-to-speech/{DEFAULT_VOICE_ID}")


async def test_synthesize_maps_401_to_a_clear_error(monkeypatch):
    _patch_httpx(monkeypatch, lambda request: httpx.Response(401, text="unauthorized"))
    with pytest.raises(TTSError, match="rejected the API key"):
        await synthesize("hi", api_key="bad-key")


async def test_synthesize_maps_404_to_a_voice_id_hint(monkeypatch):
    _patch_httpx(monkeypatch, lambda request: httpx.Response(404, text="not found"))
    with pytest.raises(TTSError, match="Voice ID"):
        await synthesize("hi", api_key="sk-test", voice_id="nonexistent")


async def test_synthesize_maps_other_errors_generically(monkeypatch):
    _patch_httpx(monkeypatch, lambda request: httpx.Response(500, text="server exploded"))
    with pytest.raises(TTSError, match="500"):
        await synthesize("hi", api_key="sk-test")


@pytest.mark.skipif(platform.system() == "Darwin", reason="platform-gate test only meaningful off macOS")
async def test_play_refuses_cleanly_off_macos():
    with pytest.raises(TTSError, match="macOS"):
        await play(b"not real audio")


def _patch_httpx(monkeypatch, handler):
    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
