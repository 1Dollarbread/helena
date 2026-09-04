"""Verification and self-critique: the 'verify' half of plan -> act -> verify.

`run_verification` auto-detects the project's test/lint tooling and actually
runs it, instead of leaving "after changing code, verify it" as a line in the
system prompt a small model may or may not act on. `self_review` asks the
model to critique its own diffs against what was actually asked, using the
real diffs recorded this turn (see ToolContext.turn_diffs / agent.py) rather
than trusting the model's own summary of what it changed — the whole point is
a check that isn't just the same model re-asserting it did fine.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ..permissions import Action
from .base import Tool, ToolContext, ToolError, ToolResult, resolve_path, truncate

MAX_CHECK_OUTPUT = 4000


def _detect_checks(root: Path) -> list[tuple[str, str]]:
    """Return [(label, command), ...] for whatever this project looks like."""
    checks: list[tuple[str, str]] = []

    package_json = root / "package.json"
    if package_json.exists():
        try:
            scripts = (json.loads(package_json.read_text("utf-8")) or {}).get("scripts") or {}
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if (root / "yarn.lock").exists():
            runner = "yarn"
        elif (root / "pnpm-lock.yaml").exists():
            runner = "pnpm"
        else:
            runner = "npm run"
        if "test" in scripts:
            checks.append(("npm test", "npm test" if runner == "npm run" else f"{runner} test"))
        if "lint" in scripts:
            checks.append(("lint", f"{runner} lint"))

    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "setup.cfg").exists():
        if (root / "pytest.ini").exists() or (root / "tests").is_dir() or (root / "test").is_dir():
            checks.append(("pytest", "python -m pytest -q"))
        checks.append(("ruff", "python -m ruff check ."))

    if (root / "Cargo.toml").exists():
        checks.append(("cargo test", "cargo test"))
        checks.append(("cargo clippy", "cargo clippy --quiet"))

    if (root / "go.mod").exists():
        checks.append(("go test", "go test ./..."))
        checks.append(("go vet", "go vet ./..."))

    return checks


class RunVerificationTool(Tool):
    name = "run_verification"
    description = """
    Auto-detect this project's tests and linter (package.json scripts,
    pytest/ruff, cargo test/clippy, go test/vet) and actually run them,
    reporting pass/fail for each. This is the concrete "verify" step after a
    round of edits — call it before telling the user a change is done,
    instead of assuming it works because it looks right.

    Pass `checks` to run only specific labels (a call with no arguments lists
    what was detected in its output). If nothing is detected, this says so
    plainly rather than guessing a command — use run_command yourself if you
    know how the project is actually tested.
    """
    action = Action.EXECUTE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: run only these labels instead of everything detected.",
            },
            "cwd": {"type": "string", "description": "Project directory. Defaults to the workspace root."},
        },
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return "run_verification"

    def preview(self, args: dict[str, Any]) -> str:
        checks = args.get("checks")
        return f"Verify: {', '.join(checks)}" if checks else "Verify (auto-detect tests/lint)"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        cwd = resolve_path(ctx, args.get("cwd") or ".", must_exist=True) if args.get("cwd") else ctx.workspace
        detected = _detect_checks(Path(cwd))
        if not detected:
            return ToolResult(
                ok=True,
                content=(
                    "No recognizable test or lint setup found here (checked package.json scripts, "
                    "pytest/ruff, cargo, go). If this project has tests, run them directly with "
                    "run_command instead."
                ),
                display="nothing to verify",
            )

        wanted = set(args.get("checks") or [])
        to_run = [c for c in detected if not wanted or c[0] in wanted]
        if not to_run:
            raise ToolError(
                f"None of {sorted(wanted)} matched what was detected: {[c[0] for c in detected]}."
            )

        lines: list[str] = []
        all_ok = True
        for label, command in to_run:
            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(cwd),
                    start_new_session=True,
                )
                out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=ctx.config.command_timeout)
                code = proc.returncode or 0
            except asyncio.TimeoutError:
                lines.append(f"[{label}] $ {command}\nTIMED OUT after {ctx.config.command_timeout}s")
                all_ok = False
                continue
            except OSError as exc:
                lines.append(f"[{label}] $ {command}\nCould not start: {exc}")
                all_ok = False
                continue
            ok = code == 0
            all_ok = all_ok and ok
            body = truncate(out_b.decode("utf-8", "replace").strip() or "(no output)", MAX_CHECK_OUTPUT, "output")
            lines.append(f"[{label}] $ {command}\n{'PASS' if ok else f'FAIL (exit {code})'}\n{body}")

        header = "All checks passed." if all_ok else "One or more checks FAILED — fix them before calling this done."
        return ToolResult(
            ok=all_ok,
            content=f"{header}\n\n" + "\n\n".join(lines),
            display="all passed" if all_ok else "failures — see output",
            meta={"verified": True, "all_ok": all_ok},
        )


class SelfReviewTool(Tool):
    name = "self_review"
    description = """
    Get a second look at the changes made so far THIS turn, checked against
    what was actually asked — not a summary you write yourself, but a fresh
    critique generated from the real diffs recorded by edit_file/write_file/
    multi_edit/create_project since the turn started. Call this before
    declaring significant changes finished, especially anything you can't
    verify by running (a logic change run_verification's tests don't cover,
    a partial refactor). Costs one extra model call — worth it for anything
    non-trivial, skip it for a one-line fix.
    """
    action = Action.NONE
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "One or two sentences: what was actually asked for, so the critique has something to check against.",
            },
        },
        "required": ["task"],
    }

    def preview(self, args: dict[str, Any]) -> str:
        return "Self-review this turn's changes"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task = (args.get("task") or "").strip()
        if not task:
            raise ToolError("`task` is required — say what was actually asked for.")
        if not ctx.turn_diffs:
            return ToolResult(
                ok=True,
                content="No file changes recorded yet this turn — nothing to review.",
                display="nothing to review",
            )

        diffs = "\n\n".join(ctx.turn_diffs)[-16_000:]
        prompt = (
            "You are reviewing another AI agent's code changes against what was asked. "
            "Be specific and brief — a short list of real problems, or say it looks correct. "
            "Do not restate the diff. Flag: logic that doesn't match the request, an edit that "
            "looks incomplete, an obvious bug, or something the request needed that isn't in the "
            "diff at all. If nothing's wrong, say so in one line.\n\n"
            f"What was asked: {task}\n\nDiff(s) made so far this turn:\n{diffs}"
        )
        try:
            reply = await ctx.client.chat(
                [{"role": "user", "content": prompt}],
                model=ctx.config.subagent_model or ctx.config.model or None,
                options={"temperature": 0.2},
            )
        except Exception as exc:  # a review hiccup shouldn't kill the turn
            return ToolResult(
                ok=False,
                content=f"Self-review couldn't run: {exc}. Proceeding without it.",
                display="review failed",
            )
        critique = (reply.content or "").strip() or "(the reviewer returned nothing)"
        return ToolResult(ok=True, content=f"Self-review:\n{critique}", display="reviewed")
