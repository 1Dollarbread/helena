"""Desktop control: open_app / close_app.

These shell out to macOS-only commands (`open`, `osascript`), so the actual
launch/quit behavior can't be exercised in CI. What's tested here is the
platform gate itself (a clean error on non-macOS, not a crash) and the pure
URL-vs-app-name classification logic, which is where a real bug would most
likely hide.
"""

from __future__ import annotations

import platform

import pytest

from helena_harness.tools.base import ToolError
from helena_harness.tools.desktop import CloseAppTool, OpenAppTool, _looks_like_url, _normalize

pytestmark = pytest.mark.skipif(platform.system() == "Darwin", reason="platform-gate tests only meaningful off macOS")


def test_looks_like_url_recognizes_urls_and_bare_domains():
    assert _looks_like_url("https://github.com")
    assert _looks_like_url("github.com")
    assert _looks_like_url("github.com/anthropics/claude")
    assert not _looks_like_url("Visual Studio Code")
    assert not _looks_like_url("Safari")


def test_normalize_adds_scheme_only_to_bare_domains():
    assert _normalize("github.com") == "https://github.com"
    assert _normalize("https://github.com") == "https://github.com"
    assert _normalize("Spotify") == "Spotify"


async def test_open_app_refuses_cleanly_off_macos(tool_ctx):
    with pytest.raises(ToolError, match="macOS"):
        await OpenAppTool().run({"target": "Safari"}, tool_ctx)


async def test_close_app_refuses_cleanly_off_macos(tool_ctx):
    with pytest.raises(ToolError, match="macOS"):
        await CloseAppTool().run({"app": "Safari"}, tool_ctx)


async def test_open_app_requires_a_target(tool_ctx):
    with pytest.raises(ToolError, match="required"):
        await OpenAppTool().run({}, tool_ctx)


def test_open_app_preview():
    assert OpenAppTool().preview({"target": "Spotify"}) == "Open Spotify"


def test_close_app_preview():
    assert CloseAppTool().preview({"app": "Spotify"}) == "Quit Spotify"
