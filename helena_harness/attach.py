"""Terminal front end that attaches to a running `helena-web` session
instead of starting its own `Agent` — see `webui.LiveHub`'s docstring for
why turns are serialized rather than run concurrently.

This is the "mirror the terminal and the web HUD" feature: run `helena-web`
somewhere (your Mac, wherever Ollama lives), then

    helena --attach

from a terminal to watch and drive the exact same conversation a browser tab
has open. Start a task from your phone, walk over, and it's still going —
and it'll keep streaming into the terminal too.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.panel import Panel
from rich.text import Text

from .config import Config
from .ui import TOOL_ICONS, UI

ANSWER_MAP = {
    "y": "once", "yes": "once", "": "no",
    "a": "always", "always": "always",
    "s": "session", "session": "session",
    "n": "no", "no": "no",
}


class AttachClient:
    """Translates the same JSON event protocol `WebUI` speaks into `rich`
    terminal output, and terminal input back into that protocol — the
    terminal becomes one more sink on the hub's broadcast list, not a
    separate conversation."""

    def __init__(self, ui: UI) -> None:
        self.ui = ui
        self.socket: Any = None

    async def run(self, url: str) -> int:
        import websockets

        async with websockets.connect(url) as socket:
            self.socket = socket
            prompt_session = PromptSession(history=InMemoryHistory())
            recv_task = asyncio.create_task(self._receive_loop(socket))
            try:
                with patch_stdout():
                    while True:
                        try:
                            line = (await prompt_session.prompt_async("you › ")).strip()
                        except (EOFError, KeyboardInterrupt):
                            break
                        if not line:
                            continue
                        if line in ("/exit", "/quit"):
                            break
                        await socket.send(json.dumps({"type": "message", "text": line}))
            finally:
                recv_task.cancel()
        return 0

    async def _receive_loop(self, socket: Any) -> None:
        async for raw in socket:
            await self._handle(json.loads(raw))

    async def _handle(self, msg: dict[str, Any]) -> None:
        ui = self.ui
        mtype = msg.get("type")
        if mtype == "ready":
            peers = ", ".join(p for p in msg.get("peers", []) if p != "terminal") or "none yet"
            ui.notice(
                f"attached — model {msg.get('model')} · {msg.get('workspace')} · "
                f"mode {msg.get('mode')} · other clients: {peers}"
            )
        elif mtype == "history":
            messages = msg.get("messages", [])
            if messages:
                ui.notice(f"caught up on {len(messages)} message(s) already in this session")
        elif mtype == "notice":
            ui.notice(msg.get("text", ""))
        elif mtype == "warn":
            ui.warn(msg.get("text", ""))
        elif mtype == "error":
            ui.error(msg.get("text", ""))
        elif mtype == "assistant_start":
            ui.assistant_prefix(msg.get("label", "HELENA"))
        elif mtype == "token":
            ui.stream_token(msg.get("text", ""))
        elif mtype == "assistant_end":
            ui.end_stream()
        elif mtype == "tool_start":
            ui.tool_start(msg.get("tool", ""), msg.get("preview", ""))
        elif mtype == "tool_result":
            ui.tool_result(msg.get("tool", ""), msg.get("ok", True), msg.get("summary", ""), msg.get("detail", ""))
        elif mtype == "todos":
            ui.render_todos(msg.get("items", []))
        elif mtype == "usage":
            ui.usage(msg.get("stats", {}))
        elif mtype == "turn_owner":
            source = msg.get("source")
            if source and source != "terminal":
                ui.notice(f"({source} sent this one — watching)")
        elif mtype == "queued":
            ui.notice(f"queued behind {msg.get('ahead')} — position {msg.get('position')}")
        elif mtype == "session_loaded":
            ui.notice(f"session switched to {msg.get('title') or msg.get('id')}")
        elif mtype == "permission_request":
            await self._ask_permission(msg)
        # turn_done: usage() already printed the summary line, nothing more to show

    async def _ask_permission(self, msg: dict[str, Any]) -> None:
        ui = self.ui
        icon = TOOL_ICONS.get(msg.get("tool", ""), "•")
        header = Text()
        header.append(f"{icon} {msg.get('tool', '')}", style="bold yellow")
        if msg.get("agent") not in (None, "helena"):
            header.append(f"  (subagent: {msg['agent']})", style="dim")

        ui.console.print()
        ui.console.print(
            Panel(Text(msg.get("preview", ""), style="bold"), title=header, border_style="yellow", expand=False)
        )
        for line in (msg.get("detail") or "").splitlines()[:20]:
            ui.console.print(Text(f"  {line}", style="dim"))
        if msg.get("danger"):
            ui.console.print(f"[bold red]  ⚠ This {msg['danger']}.[/bold red]")
        ui.console.print(
            Text("  [y] yes, once    [a] yes, always allow this    [s] yes, all session    [n] no (default)",
                 style="dim")
        )
        try:
            raw = (await PromptSession().prompt_async("  allow? ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            raw = "n"
        if self.socket is not None:
            await self.socket.send(json.dumps({"type": "permission_answer", "answer": ANSWER_MAP.get(raw, "no")}))


async def run_attach(url: str, config: Config) -> int:
    ui = UI(theme=config.theme)
    ui.print(f"[dim]Attaching to {url} …[/dim]")
    try:
        return await AttachClient(ui).run(url)
    except ModuleNotFoundError:
        ui.error("The `websockets` package is needed for --attach (pip install websockets).")
        return 1
    except OSError as exc:
        ui.error(f"Couldn't reach {url}: {exc}")
        return 1
