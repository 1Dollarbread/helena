"""Tool base class, execution context, and shared helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..permissions import Action

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import ServerClient
    from ..config import Config
    from ..permissions import PermissionEngine
    from ..ui import UI


class ToolError(Exception):
    """Raised inside a tool to report a clean, model-readable failure."""


@dataclass
class ToolResult:
    ok: bool
    content: str                       # what goes back to the model
    display: str = ""                  # what the user sees (blank = use content)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def fail(cls, message: str) -> "ToolResult":
        return cls(ok=False, content=f"Error: {message}")


@dataclass
class BackgroundJob:
    id: str
    command: str
    proc: Any
    stdout_path: Path
    started_at: float


@dataclass
class ToolContext:
    """Everything a tool is allowed to reach."""

    workspace: Path
    config: "Config"
    client: "ServerClient"
    permissions: "PermissionEngine"
    ui: "UI"
    agent_name: str = "helena"
    depth: int = 0
    # path -> mtime at last read. edit_file refuses to touch a file the model
    # has never read, and warns when it changed underneath us.
    read_files: dict[str, float] = field(default_factory=dict)
    todos: list[dict[str, Any]] = field(default_factory=list)
    jobs: dict[str, BackgroundJob] = field(default_factory=dict)
    attached_images: list[str] = field(default_factory=list)
    tool_calls_made: int = 0

    def child(self, agent_name: str) -> "ToolContext":
        """A context for a subagent: same world, deeper nesting."""
        return ToolContext(
            workspace=self.workspace,
            config=self.config,
            client=self.client,
            permissions=self.permissions,
            ui=self.ui,
            agent_name=agent_name,
            depth=self.depth + 1,
            read_files=self.read_files,   # shared: a subagent's read counts
            todos=self.todos,
            jobs=self.jobs,
        )


class Tool:
    """Base class. Subclasses declare a schema and implement `run`."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    action: Action = Action.NONE
    # Tools that only observe. Used for plan mode and read-only subagents.
    read_only: bool = True

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip(),
                "parameters": self.parameters,
            },
        }

    def permission_key(self, args: dict[str, Any]) -> str:
        """What permission rules are matched against for this call."""
        return ""

    def preview(self, args: dict[str, Any]) -> str:
        """One line describing the call, shown in the tool card and prompt."""
        return self.name

    def detail(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """Optional extra context for the permission prompt (a diff, a command)."""
        return ""

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError


# --- shared helpers ---------------------------------------------------------


def resolve_path(ctx: ToolContext, raw: str, *, must_exist: bool = False) -> Path:
    """Resolve a user/model-supplied path, keeping it inside the workspace.

    Confinement is a guard against a wandering model (or an injected
    instruction in a file it just read), not a sandbox: anything the harness
    can run through `run_command` can still reach the wider filesystem. Set
    `allow_outside_workspace` to lift it.
    """
    if not raw or not raw.strip():
        raise ToolError("A path is required.")
    expanded = Path(os.path.expanduser(raw.strip()))
    path = expanded if expanded.is_absolute() else (ctx.workspace / expanded)
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise ToolError(f"Bad path {raw!r}: {exc}") from exc

    if not ctx.config.allow_outside_workspace:
        try:
            resolved.relative_to(ctx.workspace.resolve())
        except ValueError:
            raise ToolError(
                f"{resolved} is outside the workspace ({ctx.workspace}). "
                "Ask the user to run /workspace unlock (or /trust, or restart with "
                "--allow-outside-workspace) if that's intended."
            )
    if must_exist and not resolved.exists():
        raise ToolError(f"No such file or directory: {resolved}")
    return resolved


def rel(ctx: ToolContext, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ctx.workspace.resolve()))
    except ValueError:
        return str(path)


def truncate(text: str, limit: int, note: str = "output") -> str:
    """Clip oversized tool output, telling the model what it lost and how to get it."""
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    dropped = len(text) - len(head) - len(tail)
    return (
        f"{head}\n\n… [{dropped:,} characters of {note} omitted — "
        f"narrow the request (offset/limit, a more specific pattern) to see the rest] …\n\n{tail}"
    )


BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".bz2", ".xz", ".7z", ".exe", ".dll", ".so", ".dylib", ".class", ".jar",
    ".pyc", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".avi", ".wav",
    ".sqlite", ".db", ".bin", ".o", ".a",
}

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".nuxt", "target", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".idea", ".vscode", "vendor", ".terraform", "coverage", ".tox", ".gradle",
}


def looks_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(4096)
    except OSError:
        return False


def iter_files(root: Path, *, max_files: int = 20_000):
    """Walk a tree, skipping the usual dependency and build directories."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".cache"))
        for name in sorted(filenames):
            yield Path(dirpath) / name
            count += 1
            if count >= max_files:
                return
