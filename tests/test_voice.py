"""Speech-to-text module. Mostly testing check_available()'s reporting and
the pure logic paths — actual microphone capture and a downloaded whisper
model aren't available (or appropriate to require) in a test environment.

numpy is part of the optional `voice` extra, not the core/dev install, so
this whole module is skipped rather than failing collection when it isn't
installed — that's the expected, common case for CI and for anyone who
hasn't opted into voice input.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from helena_harness.voice import INSTALL_HINT, check_available, transcribe  # noqa: E402


def test_check_available_reports_missing_packages_clearly_or_is_ready():
    # Either this environment has the voice extra installed (ready) or it
    # doesn't (a clear, actionable hint) — never a raw exception either way.
    result = check_available()
    assert result is None or result == INSTALL_HINT


async def test_transcribe_short_circuits_on_empty_audio():
    assert await transcribe(np.zeros(0, dtype="float32")) == ""
