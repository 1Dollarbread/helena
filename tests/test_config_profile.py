"""Config layering, profile storage, time parsing, and transcripts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from helena_harness.config import Config
from helena_harness.session import Transcript, find_transcript, list_transcripts


# --- config -----------------------------------------------------------------


def test_project_settings_override_defaults(tmp_path):
    (tmp_path / ".helena").mkdir()
    (tmp_path / ".helena/settings.json").write_text(json.dumps({"model": "llama3.1", "mode": "auto"}))
    config = Config.load(tmp_path)
    assert config.model == "llama3.1" and config.mode == "auto"


def test_permission_lists_are_merged_not_replaced(tmp_path, monkeypatch):
    user_dir = tmp_path / "home" / ".helena"
    user_dir.mkdir(parents=True)
    (user_dir / "settings.json").write_text(json.dumps({"allow": ["read_file(*)"]}))
    monkeypatch.setattr("helena_harness.config.USER_SETTINGS", user_dir / "settings.json")

    project = tmp_path / "project"
    (project / ".helena").mkdir(parents=True)
    (project / ".helena/settings.json").write_text(json.dumps({"allow": ["run_command(pytest:*)"]}))

    config = Config.load(project)
    assert config.allow == ["read_file(*)", "run_command(pytest:*)"]


def test_environment_beats_files(tmp_path, monkeypatch):
    (tmp_path / ".helena").mkdir()
    (tmp_path / ".helena/settings.json").write_text(json.dumps({"model": "from-file"}))
    monkeypatch.setenv("HELENA_MODEL", "from-env")
    assert Config.load(tmp_path).model == "from-env"


def test_allow_outside_workspace_env_var_is_parsed_as_boolean(tmp_path, monkeypatch):
    monkeypatch.setenv("HELENA_ALLOW_OUTSIDE_WORKSPACE", "true")
    assert Config.load(tmp_path).allow_outside_workspace is True

    monkeypatch.setenv("HELENA_ALLOW_OUTSIDE_WORKSPACE", "0")
    assert Config.load(tmp_path).allow_outside_workspace is False


def test_subagent_model_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("HELENA_SUBAGENT_MODEL", "llama3.2:1b")
    assert Config.load(tmp_path).subagent_model == "llama3.2:1b"


def test_elevenlabs_api_key_falls_back_to_plain_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("HELENA_ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-plain")
    assert Config.load(tmp_path).elevenlabs_api_key == "sk-plain"


def test_helena_prefixed_elevenlabs_key_wins_over_plain(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-plain")
    monkeypatch.setenv("HELENA_ELEVENLABS_API_KEY", "sk-prefixed")
    assert Config.load(tmp_path).elevenlabs_api_key == "sk-prefixed"


def test_speak_replies_env_var_is_parsed_as_boolean(tmp_path, monkeypatch):
    monkeypatch.setenv("HELENA_SPEAK_REPLIES", "true")
    assert Config.load(tmp_path).speak_replies is True


def test_corrupt_settings_file_does_not_break_startup(tmp_path):
    (tmp_path / ".helena").mkdir()
    (tmp_path / ".helena/settings.json").write_text("{not json")
    assert Config.load(tmp_path).mode == "ask"


def test_invalid_mode_falls_back_to_ask(tmp_path):
    (tmp_path / ".helena").mkdir()
    (tmp_path / ".helena/settings.json").write_text(json.dumps({"mode": "chaos"}))
    assert Config.load(tmp_path).mode == "ask"


def test_add_rule_persists_and_is_idempotent(tmp_path):
    config = Config(workspace=tmp_path)
    config.add_rule("allow", "run_command(ls:*)")
    config.add_rule("allow", "run_command(ls:*)")
    saved = json.loads(config.project_settings_path.read_text())
    assert saved["allow"] == ["run_command(ls:*)"]
    assert config.allow == ["run_command(ls:*)"]


def test_memory_falls_back_to_other_agent_files(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# rules\n\nbe careful\n")
    assert "be careful" in Config(workspace=tmp_path).read_memory()


# --- profile ----------------------------------------------------------------


@pytest.fixture
def isolated_profile(tmp_path, monkeypatch):
    from helena_harness import profile as profile_store

    monkeypatch.setattr(profile_store, "PROFILE_PATH", tmp_path / "profile.json")
    return profile_store


def test_facts_persist_and_reach_the_prompt(isolated_profile):
    isolated_profile.add_fact("prefers FastAPI")
    assert "prefers FastAPI" in isolated_profile.context_summary()


def test_reminder_fires_once_then_never_again(isolated_profile):
    past = datetime.now() - timedelta(minutes=1)
    isolated_profile.add_reminder("stretch", "1 minute ago", past)

    assert [r["text"] for r in isolated_profile.due_reminders()] == ["stretch"]
    assert isolated_profile.due_reminders() == []          # already marked
    assert isolated_profile.pending_reminders() == []


def test_future_reminder_is_not_due_yet(isolated_profile):
    isolated_profile.add_reminder("later", "in 1 hour", datetime.now() + timedelta(hours=1))
    assert isolated_profile.due_reminders() == []
    assert len(isolated_profile.pending_reminders()) == 1


def test_unparseable_reminder_is_kept_but_never_fires(isolated_profile):
    isolated_profile.add_reminder("someday", "whenever", None)
    assert isolated_profile.due_reminders() == []
    assert isolated_profile.pending_reminders()[0]["when_iso"] is None


def test_corrupt_profile_is_replaced_with_defaults(isolated_profile):
    isolated_profile.PROFILE_PATH.write_text("{{{")
    assert isolated_profile.load_profile()["facts"] == []


# --- time parsing -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,check",
    [
        ("in 20 minutes", lambda now, when: 19 <= (when - now).total_seconds() / 60 <= 21),
        ("in 2 hours", lambda now, when: 119 <= (when - now).total_seconds() / 60 <= 121),
        ("tomorrow", lambda now, when: when.day != now.day and when.hour == 9),
        ("tomorrow at 6pm", lambda now, when: when.hour == 18 and when.day != now.day),
        ("2026-08-12 09:00", lambda now, when: when.year == 2026 and when.hour == 9),
    ],
)
def test_parse_when_handles_common_phrasings(isolated_profile, text, check):
    now = datetime.now()
    when = isolated_profile.parse_when(text, now)
    assert when is not None, text
    assert check(now, when), f"{text} -> {when}"


def test_bare_clock_time_rolls_to_tomorrow_when_already_past(isolated_profile):
    now = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
    assert isolated_profile.parse_when("9am", now).day == (now + timedelta(days=1)).day
    assert isolated_profile.parse_when("4pm", now).day == now.day


@pytest.mark.parametrize("text", ["", "sometime soon", "whenever you feel like it", "25pm"])
def test_parse_when_rejects_what_it_cannot_read(isolated_profile, text):
    assert isolated_profile.parse_when(text) is None


# --- transcripts ------------------------------------------------------------


def test_transcript_saves_and_lists(tmp_path):
    transcript = Transcript.new(tmp_path, model="qwen2.5")
    transcript.save([
        {"role": "user", "content": "refactor the parser"},
        {"role": "assistant", "content": "done"},
    ])
    rows = list_transcripts(tmp_path)
    assert len(rows) == 1
    assert rows[0]["title"] == "refactor the parser"
    assert rows[0]["messages"] == 2


def test_transcript_strips_image_payloads(tmp_path):
    transcript = Transcript.new(tmp_path)
    path = transcript.save([{"role": "user", "content": "look", "images": ["A" * 5000]}])
    saved = json.loads(path.read_text())
    assert saved["messages"][0]["images"] == ["<image omitted: 5000 base64 chars>"]


def test_find_transcript_by_prefix_and_last(tmp_path):
    first = Transcript.new(tmp_path)
    first.save([{"role": "user", "content": "one"}])
    assert find_transcript(tmp_path, first.id[:8]).name == f"{first.id}.json"
    assert find_transcript(tmp_path, "last") is not None
    assert find_transcript(tmp_path, "nonexistent") is None
