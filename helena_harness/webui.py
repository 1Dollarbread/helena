"""A local, browser-based front end for HELENA.

This drives the exact same `Agent` / `PermissionEngine` machinery as the
terminal `Repl` — but unlike the terminal, every browser tab (and, in
`--attach` mode, a terminal too) that connects to a running `helena-web`
process joins *one shared* `LiveHub`, not a conversation of its own. That's
what makes "start a task on your phone and watch it continue on your
laptop's terminal" possible: whichever front end connects first spins up the
`Agent`; everyone after that is just another view onto it.

Run it with:

    helena-web                      # http://127.0.0.1:8765
    helena-web --port 9000 -C ~/code/some-project

Everything the terminal can do, the browser can do: it sees streamed tokens,
tool calls as they happen, and gets the same yes/always/session/no permission
prompt — just rendered as a HUD panel instead of a `rich` panel. A terminal
can join the same session with `helena --attach` (see `attach.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import uvicorn

from . import barehands_client as bh
from . import push
from .agent import Agent
from .client import ServerClient, ServerError, ensure_server
from .config import VALID_MODES, Config
from .permissions import PermissionEngine, PermissionRequest, PermissionResult
from .session import Transcript, find_transcript, list_transcripts
from .tools import build_tools
from .tools.base import ToolContext
from .ui import UI

WEB_DIR = Path(__file__).parent / "web_static"


class WebUI(UI):
    """Same interface as the terminal `UI`, rendered as JSON events over
    WebSockets instead of `rich` console output. Every terminal method that
    the agent loop calls is overridden here; anything not overridden (there
    isn't much left) silently no-ops via the parent class's `quiet` guard.

    Broadcasts to every socket attached to the hub, not just one — that's
    the mechanism behind session mirroring: a token streamed once reaches
    every attached tab and terminal at once.
    """

    def __init__(self, hub: "LiveHub") -> None:
        super().__init__(theme=hub.config.theme, quiet=True)  # quiet=True: skip rich rendering entirely
        self.hub = hub
        self._pending_permission: asyncio.Future[str] | None = None

    def _send(self, payload: dict[str, Any]) -> None:
        self.hub.broadcast(payload)

    # --- overrides the agent loop actually calls ---------------------------

    def notice(self, message: str) -> None:
        self._send({"type": "notice", "text": message})

    def warn(self, message: str) -> None:
        self._send({"type": "warn", "text": message})

    def error(self, message: str) -> None:
        self._send({"type": "error", "text": message})
        bh.write_ring_state(self.hub.config, "idle")

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

    # --- permission prompt, rendered by every attached front end -----------
    # Only one turn (and so one pending permission) is ever in flight at a
    # time — see LiveHub — so broadcasting the request and letting whichever
    # front end responds first win is safe and simple: no arbitration needed
    # beyond "first answer resolves it."

    async def ask_permission(self, req: PermissionRequest, result: PermissionResult) -> str:
        loop = asyncio.get_event_loop()
        self._pending_permission = loop.create_future()
        self.hub.broadcast({
            "type": "permission_request",
            "tool": req.tool,
            "preview": req.preview,
            "detail": req.detail,
            "danger": result.danger,
            "agent": req.agent,
        })
        try:
            answer = await asyncio.wait_for(self._pending_permission, timeout=600)
        except asyncio.TimeoutError:
            answer = "no"
        self._pending_permission = None
        return answer

    def resolve_permission(self, answer: str) -> None:
        if self._pending_permission and not self._pending_permission.done():
            self._pending_permission.set_result(answer)


class LiveHub:
    """The one shared conversation for a running `helena-web` process: one
    `Agent`, one `ToolContext`, any number of attached sockets (browser tabs
    and/or an attached terminal).

    Turns are serialized through a FIFO queue rather than run concurrently.
    Letting two front ends drive turns at once would mean interleaving tool
    calls, streamed tokens, and permission prompts from two "speakers" into
    one conversation — a much bigger problem than picking whose keystrokes
    win. First-come-first-served, with a "queued behind <source>" notice for
    anyone who has to wait, is a small price for that being simple and safe.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.sockets: list[tuple[WebSocket, str]] = []
        self._hidden: dict[int, bool] = {}
        self.ui = WebUI(self)
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
        self.transcript = Transcript.new(config.transcript_dir, model=config.model)
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._busy_source: str | None = None
        self._worker = asyncio.create_task(self._drain())

    # --- socket bookkeeping -------------------------------------------------

    def join(self, socket: WebSocket, label: str) -> None:
        self.sockets.append((socket, label))
        self._hidden[id(socket)] = False

    def leave(self, socket: WebSocket) -> None:
        self.sockets = [(s, l) for s, l in self.sockets if s is not socket]
        self._hidden.pop(id(socket), None)

    def set_hidden(self, socket: WebSocket, hidden: bool) -> None:
        self._hidden[id(socket)] = hidden

    @property
    def peers(self) -> list[str]:
        return [label for _, label in self.sockets]

    @property
    def all_hidden(self) -> bool:
        """True if every attached front end is backgrounded (or none are
        attached at all) — the signal to send a push notification instead
        of relying on someone watching the screen."""
        return all(self._hidden.values()) if self._hidden else True

    def broadcast(self, payload: dict[str, Any]) -> None:
        for socket, _ in list(self.sockets):
            asyncio.create_task(self._safe_send(socket, payload))

    @staticmethod
    async def _safe_send(socket: WebSocket, payload: dict[str, Any]) -> None:
        try:
            await socket.send_text(json.dumps(payload))
        except Exception:
            pass  # socket's already gone — nothing left to do

    # --- turns ---------------------------------------------------------------

    async def submit(self, text: str, source: str) -> None:
        pending = self._queue.qsize()
        if self._busy_source is not None or pending:
            self.broadcast({
                "type": "queued",
                "position": pending + 1,
                "ahead": self._busy_source or "another message",
            })
        await self._queue.put((text, source))

    async def _drain(self) -> None:
        while True:
            text, source = await self._queue.get()
            self._busy_source = source
            self.broadcast({"type": "turn_owner", "source": source})
            try:
                await self._run_turn(text)
            finally:
                self._busy_source = None
                self._queue.task_done()

    async def _run_turn(self, text: str) -> None:
        bh.write_ring_state(self.config, "thinking")
        result = None
        try:
            result = await self.agent.send(text)
        except ServerError as exc:
            self.ui.error(str(exc))
        except Exception as exc:  # surface it instead of killing every socket
            import traceback
            traceback.print_exc()
            self.ui.error(f"Internal error: {exc}")
        finally:
            bh.write_ring_state(self.config, "idle")

        self.transcript.save(self.agent.messages, self.agent.totals)

        if result is not None:
            self.broadcast({
                "type": "turn_done",
                "stopped": result.stopped,
                "tool_calls": result.tool_calls,
                "seconds": round(result.seconds, 1),
            })
            summary = (result.text or result.error or "").strip()
        else:
            summary = "the turn hit an error — check the HUD for details"

        if self.all_hidden:
            preview = " ".join(summary.split())[:140] or "turn complete"
            await asyncio.to_thread(push.notify_all, f"{self.config.name} finished", preview)

    # --- sessions --------------------------------------------------------------

    def load_session(self, transcript: Transcript) -> None:
        self.agent.messages = [m for m in transcript.messages if not m.get("images")]
        self.transcript = transcript

    def new_session(self) -> None:
        self.agent.messages.clear()
        self.ctx.todos.clear()
        self.transcript = Transcript.new(self.config.transcript_dir, model=self.config.model)

    async def close(self) -> None:
        self._worker.cancel()
        await self.client.aclose()


def build_app(config: Config) -> FastAPI:
    app = FastAPI(title="HELENA web")
    state: dict[str, LiveHub] = {}

    def get_hub() -> LiveHub:
        if "hub" not in state:
            state["hub"] = LiveHub(config)
        return state["hub"]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (WEB_DIR / "index.html").read_text()

    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")

    @app.get("/api/sessions")
    async def api_sessions() -> list[dict[str, Any]]:
        return list_transcripts(config.transcript_dir)

    @app.get("/push/vapid-public-key")
    async def vapid_public_key() -> JSONResponse:
        keys = push.get_vapid_keys()
        if not keys:
            return JSONResponse(
                {"error": "push extra not installed — pip install -e \".[push]\""}, status_code=503
            )
        return JSONResponse({"key": keys["public_key"]})

    @app.post("/push/subscribe")
    async def push_subscribe(request: Request) -> dict[str, bool]:
        push.add_subscription(await request.json())
        return {"ok": True}

    @app.post("/push/unsubscribe")
    async def push_unsubscribe(request: Request) -> dict[str, bool]:
        data = await request.json()
        push.remove_subscription(data.get("endpoint", ""))
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        ok, note = await ensure_server(config.server_url, config.api_token, config.auto_start_server)
        if not ok:
            await socket.send_text(json.dumps({"type": "error", "text": f"No HELENA server: {note}"}))
            await socket.close()
            return

        label = socket.query_params.get("client") or "web"
        is_first = "hub" not in state
        hub = get_hub()
        hub.join(socket, label)

        await socket.send_text(json.dumps({
            "type": "ready",
            "model": config.model or "(server default)",
            "workspace": str(config.workspace),
            "mode": config.mode,
            "peers": hub.peers,
        }))
        if not is_first and hub.agent.messages:
            # Joining a session already in progress — catch this front end
            # up on what happened before it connected.
            await socket.send_text(json.dumps({"type": "history", "messages": hub.agent.messages}))

        try:
            while True:
                raw = await socket.receive_text()
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "message":
                    await hub.submit(msg.get("text", ""), label)
                elif mtype == "permission_answer":
                    hub.ui.resolve_permission(msg.get("answer", "no"))
                elif mtype == "mode":
                    if msg.get("mode") in VALID_MODES:
                        hub.permissions.set_mode(msg["mode"])
                elif mtype == "visibility":
                    hub.set_hidden(socket, bool(msg.get("hidden")))
                elif mtype == "load_session":
                    path = find_transcript(config.transcript_dir, msg.get("id") or "last")
                    if not path:
                        await socket.send_text(json.dumps(
                            {"type": "warn", "text": f"No session matching {msg.get('id')!r}."}
                        ))
                        continue
                    hub.load_session(Transcript.load(path))
                    hub.broadcast({
                        "type": "session_loaded",
                        "id": hub.transcript.id,
                        "title": hub.transcript.title,
                        "messages": hub.agent.messages,
                    })
                elif mtype == "new_session":
                    hub.new_session()
                    hub.broadcast({
                        "type": "session_loaded",
                        "id": hub.transcript.id,
                        "title": "(new session)",
                        "messages": [],
                    })
        except WebSocketDisconnect:
            pass
        finally:
            hub.leave(socket)
            # The hub (and its Agent) stays alive after the last socket
            # leaves — that's what lets you close every tab and terminal,
            # reopen the browser later, and reattach to the same session
            # instead of it vanishing with the last connection.

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
    if not push.push_available():
        print("[push] pywebpush not installed — notifications disabled (pip install -e \".[push]\" to enable)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
