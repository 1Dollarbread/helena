"""A local, browser-based front end for HELENA.

This is NOT a replacement for `helena_harness.repl.Repl` — it's a second way
to drive the exact same `Agent` / `PermissionEngine` machinery. It creates its
own `ToolContext`, so it is a separate conversation from anything running in
a terminal at the same time (same workspace, same permission *rules* file,
independent session state). That's a deliberate simplification: sharing one
live conversation between two front ends would mean arbitrating whose
keystrokes win, which is a much bigger feature than "give me a UI".

Run it with:

    helena-web                      # http://127.0.0.1:8765
    helena-web --port 9000 -C ~/code/some-project

Everything the terminal can do, the browser can do: it sees streamed tokens,
tool calls as they happen, and gets the same yes/always/session/no permission
prompt — just rendered as a HUD panel instead of a `rich` panel.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

from . import barehands_client as bh
from .agent import Agent
from .client import ServerClient, ServerError, ensure_server
from .config import VALID_MODES, Config
from .permissions import PermissionEngine, PermissionRequest, PermissionResult
from .tools import build_tools
from .tools.base import ToolContext
from .ui import UI

WEB_DIR = Path(__file__).parent / "web_static"


class WebUI(UI):
    """Same interface as the terminal `UI`, rendered as JSON events over a
    WebSocket instead of `rich` console output. Every terminal method that
    the agent loop calls is overridden here; anything not overridden (there
    isn't much left) silently no-ops via the parent class's `quiet` guard.
    """

    def __init__(self, socket: WebSocket, config: Config) -> None:
        super().__init__(theme=config.theme, quiet=True)  # quiet=True: skip rich rendering entirely
        self.socket = socket
        self.config = config
        self._pending_permission: asyncio.Future[str] | None = None

    def _send(self, payload: dict[str, Any]) -> None:
        # Fire-and-forget from sync call sites in the agent loop; the actual
        # socket write happens on the loop via create_task. Swallow failures
        # here — if the socket's already gone, there's nothing left to do,
        # and letting the exception surface just spams the terminal.
        async def _write() -> None:
            try:
                await self.socket.send_text(json.dumps(payload))
            except Exception:
                pass
        asyncio.create_task(_write())

    # --- overrides the agent loop actually calls ---------------------------

    def notice(self, message: str) -> None:
        self._send({"type": "notice", "text": message})

    def warn(self, message: str) -> None:
        self._send({"type": "warn", "text": message})

    def error(self, message: str) -> None:
        self._send({"type": "error", "text": message})
        bh.write_ring_state(self.config, "idle")

    def assistant_prefix(self, label: str = "HELENA") -> None:
        self._send({"type": "assistant_start", "label": label})

    def stream_token(self, text: str) -> None:
        self._send({"type": "token", "text": text})

    def end_stream(self) -> None:
        self._send({"type": "assistant_end"})

    def tool_start(self, tool: str, preview: str) -> None:
        self._send({"type": "tool_start", "tool": tool, "preview": preview})

    def tool_result(self, tool: str, ok: bool, summary: str, detail: str = "") -> None:
        self._send({"type": "tool_result", "tool": tool, "ok": ok, "summary": summary, "detail": detail})

    def render_todos(self, todos) -> None:
        self._send({"type": "todos", "items": list(todos)})

    def usage(self, stats: dict[str, Any]) -> None:
        self._send({"type": "usage", "stats": stats})

    # --- permission prompt, rendered in the browser -------------------------

    async def ask_permission(self, req: PermissionRequest, result: PermissionResult) -> str:
        loop = asyncio.get_event_loop()
        self._pending_permission = loop.create_future()
        await self.socket.send_text(json.dumps({
            "type": "permission_request",
            "tool": req.tool,
            "preview": req.preview,
            "detail": req.detail,
            "danger": result.danger,
            "agent": req.agent,
        }))
        try:
            answer = await asyncio.wait_for(self._pending_permission, timeout=600)
        except asyncio.TimeoutError:
            answer = "no"
        self._pending_permission = None
        return answer

    def resolve_permission(self, answer: str) -> None:
        if self._pending_permission and not self._pending_permission.done():
            self._pending_permission.set_result(answer)


class WebSession:
    """One browser tab's conversation: its own Agent, its own ToolContext."""

    def __init__(self, socket: WebSocket, config: Config) -> None:
        self.config = config
        self.ui = WebUI(socket, config)
        self.client = ServerClient(config.server_url, config.api_token)
        self.permissions = PermissionEngine(config, ask=self.ui.ask_permission)
        self.ctx = ToolContext(
            workspace=config.workspace,
            config=config,
            client=self.client,
            permissions=self.permissions,
            ui=self.ui,
        )
        self.agent = Agent(ctx=self.ctx, tools=build_tools(self.ctx), label=config.name)

    async def turn(self, text: str) -> None:
        bh.write_ring_state(self.config, "thinking")
        try:
            result = await self.agent.send(text)
        except ServerError as exc:
            self.ui.error(str(exc))
            return
        except Exception as exc:  # surface it instead of killing the socket
            import traceback
            traceback.print_exc()
            self.ui.error(f"Internal error: {exc}")
            return
        finally:
            bh.write_ring_state(self.config, "idle")
        self.ui._send({
            "type": "turn_done",
            "stopped": result.stopped,
            "tool_calls": result.tool_calls,
            "seconds": round(result.seconds, 1),
        })

    async def close(self) -> None:
        await self.client.aclose()


def build_app(config: Config) -> FastAPI:
    app = FastAPI(title="HELENA web")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (WEB_DIR / "index.html").read_text()

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        ok, note = await ensure_server(config.server_url, config.api_token, config.auto_start_server)
        if not ok:
            await socket.send_text(json.dumps({"type": "error", "text": f"No HELENA server: {note}"}))
            await socket.close()
            return

        session = WebSession(socket, config)
        await socket.send_text(json.dumps({
            "type": "ready",
            "model": config.model or "(server default)",
            "workspace": str(config.workspace),
            "mode": config.mode,
        }))
        try:
            while True:
                raw = await socket.receive_text()
                msg = json.loads(raw)
                if msg.get("type") == "message":
                    await session.turn(msg.get("text", ""))
                elif msg.get("type") == "permission_answer":
                    session.ui.resolve_permission(msg.get("answer", "no"))
                elif msg.get("type") == "mode":
                    if msg.get("mode") in VALID_MODES:
                        session.permissions.set_mode(msg["mode"])
        except WebSocketDisconnect:
            pass
        finally:
            await session.close()

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="helena-web", description="HELENA's browser-based HUD client.")
    parser.add_argument("-C", "--workspace", default=None, help="Directory to work in.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default=None)
    parser.add_argument("--server", default=None, help="HELENA server URL.")
    parser.add_argument("--mode", choices=VALID_MODES, default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    if not workspace.is_dir():
        print(f"helena-web: not a directory: {workspace}", file=sys.stderr)
        raise SystemExit(2)

    config = Config.load(workspace)
    if args.model:
        config.model = args.model
    if args.server:
        config.server_url = args.server
    if args.mode:
        config.mode = args.mode

    app = build_app(config)
    print(f"HELENA web HUD → http://{args.host}:{args.port}  (workspace: {workspace})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
