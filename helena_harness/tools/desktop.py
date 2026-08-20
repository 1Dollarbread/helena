"""Desktop control: opening apps and URLs, and quitting apps. macOS-first
(via the `open` command and AppleScript), since that's the only platform
this harness has been tested on so far.

Deliberately narrow: a named app or a URL, nothing else. These use
create_subprocess_exec with argument lists, not a shell string — so unlike
run_command there's no shell-parsing step for anything to inject into. The
app name still gets interpolated into an AppleScript source string for
close_app, so it's quote-escaped there specifically.
"""

from __future__ import annotations

import asyncio
import platform
import re
from typing import Any

from ..permissions import Action
from .base import Tool, ToolContext, ToolError, ToolResult

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^[\w-]+(\.[\w-]+)+(/.*)?$")


def _looks_like_url(s: str) -> bool:
    return bool(_URL_RE.match(s) or _DOMAIN_RE.match(s))


def _normalize(target: str) -> str:
    if _URL_RE.match(target):
        return target
    if _DOMAIN_RE.match(target):
        return f"https://{target}"
    return target


class OpenAppTool(Tool):
    name = "open_app"
    description = """
    Open a native application, or a URL/domain in the default browser. Give it
    an app name ("Visual Studio Code", "Safari", "Spotify") or a URL. macOS only
    right now — pass an app's exact name as it appears in Applications.
    """
    action = Action.EXECUTE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "An app name, or a URL/domain to open in the browser."},
        },
        "required": ["target"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return (args.get("target") or "").strip()

    def preview(self, args: dict[str, Any]) -> str:
        return f"Open {args.get('target', '?')}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = (args.get("target") or "").strip()
        if not target:
            raise ToolError("`target` is required.")
        if platform.system() != "Darwin":
            raise ToolError("open_app is only wired up for macOS right now.")

        is_url = _looks_like_url(target)
        normalized = _normalize(target)
        cmd = ["open", normalized] if is_url else ["open", "-a", normalized]

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            reason = stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
            if not is_url:
                raise ToolError(f'Could not find an app named "{target}" ({reason}). Check the exact name in Applications.')
            raise ToolError(f"Could not open {normalized} ({reason}).")

        kind = "" if is_url else " (app)"
        return ToolResult(ok=True, content=f"Opened {normalized}{kind}.", display=f"opened {normalized}")


class CloseAppTool(Tool):
    name = "close_app"
    description = "Quit a running native application by name (macOS only)."
    action = Action.EXECUTE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {"app": {"type": "string", "description": "Exact app name to quit."}},
        "required": ["app"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return (args.get("app") or "").strip()

    def preview(self, args: dict[str, Any]) -> str:
        return f"Quit {args.get('app', '?')}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        app = (args.get("app") or "").strip()
        if not app:
            raise ToolError("`app` is required.")
        if platform.system() != "Darwin":
            raise ToolError("close_app is only wired up for macOS right now.")

        safe_app = app.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "{safe_app}" to quit'
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            reason = stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
            raise ToolError(f'Could not quit "{app}" — it may not be running, or the name doesn\'t match exactly. ({reason})')
        return ToolResult(ok=True, content=f"Quit {app}.", display=f"quit {app}")
