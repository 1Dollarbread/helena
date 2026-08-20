"""Permission engine: rule matching, modes, and the safety rails."""

from __future__ import annotations

import pytest

from helena_harness.config import Config
from helena_harness.permissions import (
    Action,
    Decision,
    PermissionEngine,
    PermissionRequest,
    classify_command,
    parse_rule,
)


def engine(mode="ask", allow=None, deny=None, workspace=None, ask=None) -> PermissionEngine:
    config = Config(workspace=workspace or ".", mode=mode, allow=allow or [], deny=deny or [])
    return PermissionEngine(config, ask=ask)


def req(tool="run_command", action=Action.EXECUTE, key="") -> PermissionRequest:
    return PermissionRequest(tool=tool, action=action, key=key, preview=key or tool)


# --- rule parsing -----------------------------------------------------------


def test_parse_rule_forms():
    assert parse_rule("web_search") == parse_rule("WebSearch")     # alias, no pattern
    rule = parse_rule("run_command(git status:*)")
    assert rule.tool == "run_command" and rule.pattern == "git status:*"
    assert parse_rule("Bash(ls)").tool == "run_command"            # alias with pattern


@pytest.mark.parametrize(
    "rule,key,expected",
    [
        ("run_command(git status:*)", "git status --short", True),
        ("run_command(git status:*)", "git push", False),
        ("run_command(*)", "anything at all", True),
        ("write_file(src/**)", "src/deep/a.py", True),
        ("write_file(src/**)", "tests/a.py", False),
        ("write_file(*.md)", "README.md", True),
        ("read_file(exact.txt)", "exact.txt", True),
        ("read_file(exact.txt)", "other.txt", False),
    ],
)
def test_rule_matching(rule, key, expected):
    tool = parse_rule(rule).tool
    assert parse_rule(rule).matches(tool, key) is expected


def test_wildcard_rule_matches_every_tool():
    assert parse_rule("*").matches("run_command", "rm file")
    assert parse_rule("*").matches("write_file", "a.py")


# --- modes ------------------------------------------------------------------


def test_ask_mode_allows_reads_and_asks_for_writes():
    eng = engine("ask")
    assert eng.evaluate(req("read_file", Action.READ, "a.py")).decision is Decision.ALLOW
    assert eng.evaluate(req("write_file", Action.WRITE, "a.py")).decision is Decision.ASK
    assert eng.evaluate(req(key="pytest -q")).decision is Decision.ASK


def test_auto_mode_allows_edits_but_still_asks_for_commands():
    eng = engine("auto")
    assert eng.evaluate(req("write_file", Action.WRITE, "a.py")).decision is Decision.ALLOW
    assert eng.evaluate(req(key="pytest -q")).decision is Decision.ASK


def test_plan_mode_refuses_all_mutation():
    eng = engine("plan")
    assert eng.evaluate(req("read_file", Action.READ, "a.py")).decision is Decision.ALLOW
    write = eng.evaluate(req("write_file", Action.WRITE, "a.py"))
    assert write.decision is Decision.DENY and "read-only" in write.reason.lower()
    assert eng.evaluate(req(key="ls")).decision is Decision.DENY


def test_yolo_allows_ordinary_work_but_not_hard_blocks():
    eng = engine("yolo")
    assert eng.evaluate(req(key="rm -rf ./build")).decision is Decision.ALLOW
    blocked = eng.evaluate(req(key="rm -rf /"))
    assert blocked.decision is Decision.DENY and blocked.danger


# --- precedence -------------------------------------------------------------


def test_deny_beats_allow_and_mode():
    eng = engine("yolo", allow=["*"], deny=["run_command(curl:*)"])
    assert eng.evaluate(req(key="curl example.com")).decision is Decision.DENY


def test_allow_rule_overrides_ask():
    eng = engine("ask", allow=["run_command(pytest:*)"])
    result = eng.evaluate(req(key="pytest -q tests/"))
    assert result.decision is Decision.ALLOW and result.rule == "run_command(pytest:*)"


def test_risky_commands_ask_even_in_auto_mode():
    eng = engine("auto")
    result = eng.evaluate(req(key="git push origin main"))
    assert result.decision is Decision.ASK and "remote" in result.danger


@pytest.mark.parametrize(
    "command",
    ["rm -rf /", "rm -rf ~", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda", ":(){ :|:& };:"],
)
def test_hard_blocked_commands(command):
    blocked, _ = classify_command(command)
    assert blocked, command
    assert engine("yolo").evaluate(req(key=command)).decision is Decision.DENY


@pytest.mark.parametrize("command", ["rm -rf ./node_modules", "git status", "ls -la /tmp", "echo rm -rf /"])
def test_ordinary_commands_are_not_hard_blocked(command):
    blocked, _ = classify_command(command)
    assert blocked is None, command


# --- interactive path -------------------------------------------------------


async def test_check_denies_when_nobody_can_answer():
    result = await engine("ask").check(req(key="pytest"))
    assert result.decision is Decision.DENY
    assert "no one is at the keyboard" in result.reason


async def test_always_answer_persists_a_rule(tmp_path):
    async def always(_request, _result):
        return "always"

    eng = engine("ask", workspace=tmp_path, ask=always)
    result = await eng.check(req(key="pytest -q tests/"))
    assert result.decision is Decision.ALLOW
    # Grants the verb, not the exact invocation.
    assert result.rule == "run_command(pytest -q:*)"
    assert "run_command(pytest -q:*)" in eng.config.allow
    assert eng.config.project_settings_path.is_file()
    # And a second, differently-worded call now passes without asking.
    assert eng.evaluate(req(key="pytest -q other/")).decision is Decision.ALLOW


async def test_session_answer_does_not_touch_disk(tmp_path):
    async def session(_request, _result):
        return "session"

    eng = engine("ask", workspace=tmp_path, ask=session)
    assert (await eng.check(req(key="ls"))).decision is Decision.ALLOW
    assert eng.evaluate(req(key="ls")).decision is Decision.ALLOW
    assert not eng.config.project_settings_path.exists()


async def test_no_answer_denies_and_is_recorded(tmp_path):
    async def refuse(_request, _result):
        return "no"

    eng = engine("ask", workspace=tmp_path, ask=refuse)
    assert (await eng.check(req(key="rm file"))).decision is Decision.DENY
    assert eng.stats["denied"] == 1
    assert eng.denied_this_session
