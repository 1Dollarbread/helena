"""Command execution: foreground with a timeout, plus background jobs."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from ..permissions import Action, classify_command
from .base import BackgroundJob, Tool, ToolContext, ToolError, ToolResult, resolve_path, truncate

MAX_OUTPUT = 30_000


def _describe(command: str, limit: int = 90) -> str:
    one_line = " ".join(command.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


class RunCommandTool(Tool):
    name = "run_command"
    description = """
    Run a shell command in the workspace and get back its stdout, stderr, and exit code.
    This is real execution — the command actually runs on this machine.

    Use it for builds, tests, linters, git, package managers, and anything else a
    terminal can do. Prefer the dedicated tools where they exist: read_file over cat,
    search_text over grep, find_files over find, edit_file over sed -i.
    Long-running processes (dev servers, watchers) must set background: true, or
    they will hit the timeout and be killed.
    """
    action = Action.EXECUTE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line to run."},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed. Default 120, max 900."},
            "cwd": {"type": "string", "description": "Directory to run in. Defaults to the workspace root."},
            "background": {"type": "boolean", "description": "Start it detached and return immediately; check on it with check_job."},
            "description": {"type": "string", "description": "Short human-readable summary of what this command does and why."},
        },
        "required": ["command"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return (args.get("command") or "").strip()

    def preview(self, args: dict[str, Any]) -> str:
        note = args.get("description")
        bg = " (background)" if args.get("background") else ""
        cmd = _describe(args.get("command", "?"))
        return f"{cmd}{bg}" + (f"  — {note}" if note else "")

    def detail(self, args: dict[str, Any], ctx: ToolContext) -> str:
        command = args.get("command", "")
        _, risky = classify_command(command)
        where = args.get("cwd") or "."
        lines = [f"$ {command}", f"cwd: {where}"]
        if risky:
            lines.append(f"note: this {risky}")
        return "\n".join(lines)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = (args.get("command") or "").strip()
        if not command:
            raise ToolError("`command` is required.")
        cwd = resolve_path(ctx, args.get("cwd") or ".", must_exist=True) if args.get("cwd") else ctx.workspace
        if not Path(cwd).is_dir():
            raise ToolError(f"{cwd} is not a directory.")

        if args.get("background"):
            return await self._run_background(command, cwd, ctx)

        timeout = min(900, max(1, int(args.get("timeout") or ctx.config.command_timeout)))
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                start_new_session=True,   # so we can kill the whole process group
                env={**os.environ, "HELENA": "1", "TERM": "dumb", "NO_COLOR": "1"},
            )
        except OSError as exc:
            raise ToolError(f"Could not start the command: {exc}") from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            self._kill(proc)
            elapsed = time.monotonic() - started
            return ToolResult(
                ok=False,
                content=(
                    f"Command timed out after {elapsed:.0f}s and was killed:\n$ {command}\n"
                    "If it is meant to keep running (a server, a watcher), call it again with "
                    "background: true. Otherwise raise `timeout`."
                ),
                display=f"timed out after {elapsed:.0f}s",
            )
        except asyncio.CancelledError:
            self._kill(proc)
            raise

        elapsed = time.monotonic() - started
        stdout = stdout_b.decode("utf-8", "replace")
        stderr = stderr_b.decode("utf-8", "replace")
        code = proc.returncode or 0

        parts = []
        if stdout.strip():
            parts.append(stdout.rstrip())
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.rstrip()}")
        body = "\n".join(parts) or "(no output)"
        body = truncate(body, min(MAX_OUTPUT, ctx.config.max_tool_output_chars), "command output")

        header = f"$ {command}\nexit {code} · {elapsed:.1f}s"
        return ToolResult(
            ok=code == 0,
            content=f"{header}\n{body}",
            display=f"exit {code} · {elapsed:.1f}s",
            meta={"exit_code": code, "elapsed": elapsed, "stdout": stdout, "stderr": stderr},
        )

    async def _run_background(self, command: str, cwd: Path, ctx: ToolContext) -> ToolResult:
        job_id = uuid.uuid4().hex[:6]
        log_dir = ctx.config.project_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"job-{job_id}.log"
        handle = log_path.open("wb")
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd),
                start_new_session=True,
                env={**os.environ, "HELENA": "1"},
            )
        except OSError as exc:
            handle.close()
            raise ToolError(f"Could not start the background command: {exc}") from exc

        ctx.jobs[job_id] = BackgroundJob(
            id=job_id, command=command, proc=proc, stdout_path=log_path, started_at=time.time()
        )
        return ToolResult(
            ok=True,
            content=(
                f"Started job {job_id} in the background (pid {proc.pid}):\n$ {command}\n"
                f"Output is being written to {log_path}. Read it with check_job(job_id=\"{job_id}\")."
            ),
            display=f"job {job_id} started (pid {proc.pid})",
            meta={"job_id": job_id},
        )

    @staticmethod
    def _kill(proc: Any) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass


class CheckJobTool(Tool):
    name = "check_job"
    description = """
    Check on a background job started by run_command: whether it is still running,
    its exit code if it finished, and its most recent output.
    """
    action = Action.READ
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Omit to list every job."},
            "lines": {"type": "integer", "description": "How many trailing output lines to show. Default 60."},
            "kill": {"type": "boolean", "description": "Stop the job instead of just inspecting it."},
        },
    }

    def preview(self, args: dict[str, Any]) -> str:
        if args.get("kill"):
            return f"Stop background job {args.get('job_id', '?')}"
        return f"Check background job {args.get('job_id') or '(all)'}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not ctx.jobs:
            return ToolResult(ok=True, content="No background jobs have been started.", display="none")

        job_id = args.get("job_id")
        if not job_id:
            rows = []
            for job in ctx.jobs.values():
                state = "running" if job.proc.returncode is None else f"exited {job.proc.returncode}"
                rows.append(f"{job.id}  {state}  ({time.time() - job.started_at:.0f}s)  $ {_describe(job.command, 60)}")
            return ToolResult(ok=True, content="\n".join(rows), display=f"{len(rows)} job(s)")

        job = ctx.jobs.get(job_id)
        if not job:
            raise ToolError(f"No job {job_id!r}. Known jobs: {', '.join(ctx.jobs) or 'none'}.")

        if args.get("kill"):
            if job.proc.returncode is None:
                RunCommandTool._kill(job.proc)
                await asyncio.sleep(0.2)
            return ToolResult(ok=True, content=f"Stopped job {job_id}.", display=f"job {job_id} stopped")

        lines = max(1, min(500, int(args.get("lines") or 60)))
        try:
            output = job.stdout_path.read_text("utf-8", errors="replace")
        except OSError:
            output = ""
        tail = "\n".join(output.splitlines()[-lines:]) or "(no output yet)"
        state = "still running" if job.proc.returncode is None else f"exited with code {job.proc.returncode}"
        return ToolResult(
            ok=True,
            content=f"Job {job_id} ({state}, {time.time() - job.started_at:.0f}s)\n$ {job.command}\n\n{tail}",
            display=state,
        )


_URL_IN_LOG_RE = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\])(?::\d+)?[^\s\"'<>]*",
    re.IGNORECASE,
)

# Checked in priority order once package.json's scripts are known — "dev" over
# "start" because that's the convention nearly every JS framework uses for a
# local server (start is often the production/build-first variant).
_NPM_SCRIPT_PRIORITY = ("dev", "start", "serve", "develop")


def _detect_command(cwd: Path) -> tuple[str, str] | None:
    """Best-effort guess at how to start whatever's in `cwd`.

    Returns (command, framework_label), or None if nothing recognizable is
    there — callers should ask for an explicit `command` in that case rather
    than guess something that's likely wrong.
    """
    package_json = cwd / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        scripts = data.get("scripts") or {}
        for script_name in _NPM_SCRIPT_PRIORITY:
            if script_name in scripts:
                if (cwd / "yarn.lock").exists():
                    return f"yarn {script_name}", "node"
                if (cwd / "pnpm-lock.yaml").exists():
                    return f"pnpm {script_name}", "node"
                return f"npm run {script_name}", "node"

    if (cwd / "manage.py").exists():
        return "python manage.py runserver", "django"

    for candidate in ("app.py", "main.py", "server.py"):
        f = cwd / candidate
        if not f.exists():
            continue
        try:
            text = f.read_text("utf-8", errors="ignore")
        except OSError:
            text = ""
        module = candidate[:-3]
        if "FastAPI(" in text:
            return f"uvicorn {module}:app --reload", "fastapi"
        if "Flask(" in text:
            return f"python {candidate}", "flask"

    if any(cwd.glob("*.html")):
        return "python3 -m http.server", "static"

    return None


class DevServerTool(Tool):
    name = "run_dev_server"
    description = """
    Start a local development server and report the real URL it ends up serving
    on, instead of guessing a port. When `command` is omitted, auto-detects the
    right start command from the project: package.json scripts (dev, then start,
    then serve — using yarn/pnpm if a lockfile says so), manage.py (Django), a
    FastAPI/Flask app.py/main.py/server.py, or falls back to a plain static
    server if there's just HTML. Pass `command` explicitly to override the guess
    or handle a framework this doesn't recognize.

    This is the right tool for "run it" / "start the dev server" / "run this on
    a local port" — prefer it over a bare run_command(background=true) for that,
    since it also watches the output and reports the actual URL instead of
    leaving you to find the port yourself. Runs in the background exactly like
    run_command does; use check_job to see more output later, or check_job with
    kill: true to stop it.
    """
    action = Action.EXECUTE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Override the auto-detected start command."},
            "cwd": {"type": "string", "description": "Project directory. Defaults to the workspace root."},
            "wait_seconds": {"type": "integer", "description": "How long to watch for the server's URL before giving up. Default 15, max 60."},
        },
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return (args.get("command") or "run_dev_server").strip()

    def preview(self, args: dict[str, Any]) -> str:
        if args.get("command"):
            return f"Start dev server: {_describe(args['command'])}"
        return "Start dev server (auto-detect)"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        cwd = resolve_path(ctx, args.get("cwd") or ".", must_exist=True) if args.get("cwd") else ctx.workspace
        if not Path(cwd).is_dir():
            raise ToolError(f"{cwd} is not a directory.")

        command = (args.get("command") or "").strip()
        framework = None
        if not command:
            detected = _detect_command(Path(cwd))
            if detected is None:
                raise ToolError(
                    "Couldn't detect how to start this project — no package.json script, "
                    "manage.py, recognizable app.py/main.py/server.py, or *.html found here. "
                    "Pass `command` explicitly."
                )
            command, framework = detected

        runner = RunCommandTool()
        started = await runner._run_background(command, Path(cwd), ctx)
        job_id = started.meta.get("job_id")
        job = ctx.jobs.get(job_id) if job_id else None
        if job is None:
            return started  # something went wrong starting it; surface that as-is

        wait_seconds = max(1, min(60, int(args.get("wait_seconds") or 15)))
        deadline = time.monotonic() + wait_seconds
        url = None
        while time.monotonic() < deadline:
            if job.proc.returncode is not None:
                break  # exited already — no point continuing to poll for a URL
            try:
                text = job.stdout_path.read_text("utf-8", errors="replace")
            except OSError:
                text = ""
            match = _URL_IN_LOG_RE.search(text)
            if match:
                url = match.group(0)
                break
            await asyncio.sleep(0.5)

        label = f" ({framework})" if framework else ""

        if job.proc.returncode is not None:
            try:
                output = job.stdout_path.read_text("utf-8", errors="replace")
            except OSError:
                output = ""
            tail = "\n".join(output.splitlines()[-20:]) or "(no output)"
            return ToolResult(
                ok=False,
                content=f"Started{label} as job {job_id}, but it exited immediately (code {job.proc.returncode}):\n\n{tail}",
                display=f"job {job_id} exited",
                meta={"job_id": job_id},
            )

        if url:
            return ToolResult(
                ok=True,
                content=f"Dev server{label} is up: {url}\n(job {job_id}, pid {job.proc.pid} — check_job to see more output or stop it)",
                display=url,
                meta={"job_id": job_id, "url": url},
            )

        return ToolResult(
            ok=True,
            content=(
                f"Started{label} as job {job_id} (pid {job.proc.pid}), but didn't see a URL in its output "
                f"within {wait_seconds}s. It's likely still starting up — check_job(job_id=\"{job_id}\") to "
                "see its latest output, or call this again with a longer wait_seconds."
            ),
            display=f"job {job_id} started, no URL yet",
            meta={"job_id": job_id},
        )
