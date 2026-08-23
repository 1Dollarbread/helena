"""The interactive terminal loop: prompt, slash commands, reminders, shutdown."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import shlex
import signal
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from . import barehands_client as bh
from . import profile as profile_store
from . import tts, voice
from .agent import Agent
from .client import ServerClient, ServerError, ensure_server
from .config import USER_DIR, VALID_MODES, Config
from .permissions import PermissionEngine
from .session import Transcript, find_transcript, list_transcripts
from .subagents import AGENT_SPECS
from .tools import build_tools
from .tools.base import IMAGE_SUFFIXES, ToolContext
from .ui import UI

REMINDER_INTERVAL = 15  # seconds

COMMANDS: dict[str, str] = {
    "/help": "Show this list",
    "/model": "Show or switch the chat model — /model qwen2.5:7b-instruct",
    "/models": "List models available on the server",
    "/pull": "Download a model through the server — /pull llama3.1",
    "/mode": "Show or set permission mode: ask | auto | plan | yolo",
    "/workspace": "Show, lock, or unlock file access outside the workspace folder",
    "/trust": "Full access, right now: yolo mode + unlocked workspace — see /help trust",
    "/permissions": "Inspect and edit rules — /permissions allow run_command(git:*)",
    "/tools": "List the tools this agent can use",
    "/agents": "List subagent types",
    "/agent": "Run a subagent directly — /agent explorer where is auth handled",
    "/image": "Attach an image — /image ./shot.png what's the error here?",
    "/read": "Load a file into the conversation — /read src/app.py",
    "/search": "Web search without going through the model — /search python 3.13 release",
    "/session": "list | new | save | load <id> — local transcripts",
    "/compact": "Drop tool chatter from history, keep the thread",
    "/clear": "Start a fresh conversation",
    "/cost": "Token and timing totals for this session",
    "/jobs": "Show background commands started by run_command",
    "/memory": "Show HELENA.md, or append a line to it — /memory always use pnpm",
    "/remember": "Save a durable fact about you — /remember I prefer FastAPI",
    "/reminders": "List pending reminders",
    "/init": "Have HELENA write a HELENA.md for this project",
    "/cd": "Change the workspace directory",
    "/doctor": "Check the server, Ollama, and the selected models",
    "/stream": "on | off — stream replies token by token",
    "/voice": "Speak a message instead of typing it — records until you press Enter",
    "/say": "Speak text out loud right now — /say testing one two three",
    "/speak": "on | off — auto-speak every reply (needs /voice-setup done first)",
    "/voice-setup": "Show what's needed to enable voice input and HELENA's spoken voice",
    "/barehands-setup": "Clone, start, and connect the barehands hand-tracked board",
    "/board": "Send a raw board command — /board present src=models/car.glb title=\"My Car\"",
    "/board-state": "Show what's actually on the barehands board right now",
    "/exit": "Quit (Ctrl-D also works)",
}

PATH_COMMANDS = {"/read", "/image", "/cd"}


class HelenaCompleter(Completer):
    """Completes slash commands, then falls back to path completion."""

    def __init__(self) -> None:
        self.paths = PathCompleter(expanduser=True)

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for name, help_text in COMMANDS.items():
                if name.startswith(text):
                    yield Completion(name, start_position=-len(text), display_meta=help_text)
            return
        first = text.split(" ", 1)[0]
        word = document.get_word_before_cursor(WORD=True)
        if first in PATH_COMMANDS or "/" in word or word.startswith("~"):
            sub = Document(word, len(word))
            for completion in self.paths.get_completions(sub, complete_event):
                yield completion


class Repl:
    def __init__(self, config: Config, ui: UI | None = None) -> None:
        self.config = config
        self.ui = ui or UI(theme=config.theme)
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
        self.session: PromptSession | None = None
        self.busy = False
        self._current_task: asyncio.Task | None = None
        self._reminder_task: asyncio.Task | None = None
        self._pending_images: list[str] = []

    # --- lifecycle ---------------------------------------------------------

    async def start(self, first_message: str | None = None) -> int:
        ok, note = await ensure_server(
            self.config.server_url, self.config.api_token, self.config.auto_start_server
        )
        if not ok:
            self.ui.error(f"No HELENA server: {note}.")
            self.ui.notice(
                "Start one with `helena-server` (or `python -m helena_server`), "
                f"or point at another with HELENA_SERVER_URL. Trying {self.config.server_url}."
            )
            return 1
        if note.startswith("started"):
            self.ui.notice(f"Started a model server for this session ({note}).")

        health = await self.client.health()
        model_label = self.config.model or health.get("default_model", "?")
        if not health.get("ollama_reachable"):
            self.ui.warn(health.get("detail") or "Ollama is not reachable — the model layer is down.")

        self.ui.banner(model_label, str(self.config.workspace), self.config.mode, self.config.server_url)
        await self._check_tool_calling_support(model_label)
        # If barehands is configured, make sure its ring starts from a known
        # state rather than whatever was left over from a previous session
        # that ended uncleanly (crash, kill -9) with the ring stuck mid-"thinking".
        bh.write_ring_state(self.config, "idle")

        USER_DIR.mkdir(parents=True, exist_ok=True)
        self.session = PromptSession(
            history=FileHistory(str(USER_DIR / "history")),
            completer=HelenaCompleter(),
            complete_while_typing=False,
        )
        self.ui.prompt_session = self.session
        self._reminder_task = asyncio.create_task(self._reminder_loop())

        if first_message:
            await self._guarded_turn(first_message)

        try:
            await self._loop()
        finally:
            await self.shutdown()
        return 0

    async def _check_tool_calling_support(self, model_label: str) -> None:
        """Warn up front if the selected model isn't recognized as tool-capable.

        Without real tool calling, the agent loop falls back to recovering
        tool calls from plain text, which only partly papers over the gap —
        the model can (and does, in practice) narrate actions it never
        actually took instead of calling a real tool. Better to say so
        before that happens than let it look like a silent, confusing bug.
        """
        try:
            data = await self.client.models()
        except ServerError:
            return  # /doctor covers this more thoroughly if the user wants it
        info = next((m for m in data.get("models", []) if m.get("name") == model_label), None)
        if info is not None and not info.get("supports_tools"):
            self.ui.warn(
                f'"{model_label}" isn\'t recognized as tool-calling capable. Agentic work will still '
                "run, but the model may narrate actions instead of actually taking them — watch for "
                "replies that describe doing something without a real tool line above them. "
                "`qwen2.5:7b-instruct`, `llama3.1`, and `mistral-nemo` are reliable choices; "
                "run /doctor anytime to recheck."
            )

    async def _loop(self) -> None:
        while True:
            try:
                with patch_stdout():
                    line = await self.session.prompt_async(
                        HTML("\n<b><ansicyan>you</ansicyan></b> <ansibrightblack>›</ansibrightblack> ")
                    )
            except KeyboardInterrupt:
                continue                    # Ctrl-C at an empty prompt: ignore
            except EOFError:
                self.ui.print("[dim]Goodbye.[/dim]")
                return

            line = (line or "").strip()
            if not line:
                continue
            if line in ("/exit", "/quit", "exit", "quit"):
                self.ui.print("[dim]Goodbye.[/dim]")
                return
            try:
                await self.handle_line(line)
            except Exception as exc:  # never let one bad turn kill the session
                self.ui.error(f"{type(exc).__name__}: {exc}")

    async def shutdown(self) -> None:
        bh.write_ring_state(self.config, "idle")
        if self._reminder_task:
            self._reminder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reminder_task
        for job in self.ctx.jobs.values():
            if job.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    job.proc.terminate()
        if self.agent.messages:
            path = self.transcript.save(self.agent.messages, self.agent.totals)
            self.ui.notice(f"Transcript saved to {path}")
        await self.client.aclose()

    # --- input routing -----------------------------------------------------

    async def handle_line(self, line: str) -> None:
        if line.startswith("/"):
            await self.handle_command(line)
            return

        # A bare path to an image in an ordinary message means "look at this",
        # exactly as it did in the original HELENA.
        found = self._find_path(line)
        if found:
            token, path = found
            if path.suffix.lower() in IMAGE_SUFFIXES:
                question = line.replace(token, "").strip()
                await self.cmd_image([str(path)] + (question.split() if question else []))
                return

        await self._guarded_turn(line)

    def _find_path(self, text: str) -> tuple[str, Path] | None:
        """Find a token in free text that resolves to an existing file.

        Only path-shaped tokens are considered, so ordinary words never match.
        The original token comes back too, so the caller can strip exactly what
        the user typed rather than the resolved absolute path.
        """
        for token in text.split():
            stripped = token.strip("\"'(),;:!?")
            if not ("/" in stripped or stripped.startswith("~")):
                continue
            candidate = Path(os.path.expanduser(stripped))
            if not candidate.is_absolute():
                candidate = self.config.workspace / candidate
            if candidate.is_file():
                return stripped, candidate
        return None

    async def _guarded_turn(self, text: str) -> None:
        """Run one agent turn, cancellable with Ctrl-C.

        If barehands is configured, the board's ring mirrors the shape of
        this method exactly: "thinking" the moment work starts, "speaking"
        only around the actual TTS playback, "idle" the instant everything
        settles — including every early-return path (interrupted, cancelled),
        via the outer try/finally, so the ring can never get stuck mid-state
        after a Ctrl-C.
        """
        images, self._pending_images = self._pending_images, []
        self.busy = True
        bh.write_ring_state(self.config, "thinking")
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(self.agent.send(text, images=images or None))
        self._current_task = task

        installed = False
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGINT, task.cancel)
            installed = True
        try:
            try:
                result = await task
            except asyncio.CancelledError:
                self.ui.print()
                self.ui.warn("Interrupted. The conversation is intact — say what to do differently.")
                return
            except KeyboardInterrupt:
                task.cancel()
                self.ui.warn("Interrupted.")
                return

            self.ui.usage({
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_seconds": result.seconds,
                "tool_calls": result.tool_calls,
            })
            self.transcript.save(self.agent.messages, self.agent.totals)
            self._flush_reminders()
            if self.config.speak_replies and result.text.strip():
                # Best-effort — a TTS hiccup (rate limit, network) should never
                # take down the actual conversation, just skip the audio quietly
                # with a one-line note instead of raising into the REPL loop.
                bh.write_ring_state(self.config, "speaking")
                try:
                    await tts.speak(result.text, self.config.elevenlabs_api_key, self.config.elevenlabs_voice_id)
                except tts.TTSError as exc:
                    self.ui.warn(f"(spoken reply skipped: {exc})")
        finally:
            self.busy = False
            self._current_task = None
            bh.write_ring_state(self.config, "idle")
            if installed:
                # remove_signal_handler restores Python's default SIGINT
                # behaviour, which is what prompt_toolkit expects at the prompt.
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.remove_signal_handler(signal.SIGINT)

    # --- reminders ---------------------------------------------------------

    async def _reminder_loop(self) -> None:
        while True:
            await asyncio.sleep(REMINDER_INTERVAL)
            if self.busy:
                continue          # picked up right after the turn finishes
            self._flush_reminders()

    def _flush_reminders(self) -> None:
        for item in profile_store.due_reminders():
            self.ui.print(f"\n[bold yellow]⏰ Reminder:[/bold yellow] {item['text']}")

    # --- commands ----------------------------------------------------------

    async def handle_command(self, line: str) -> None:
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        handlers = {
            "/help": self.cmd_help,
            "/model": self.cmd_model,
            "/models": self.cmd_models,
            "/pull": self.cmd_pull,
            "/mode": self.cmd_mode,
            "/workspace": self.cmd_workspace,
            "/trust": self.cmd_trust,
            "/permissions": self.cmd_permissions,
            "/tools": self.cmd_tools,
            "/agents": self.cmd_agents,
            "/agent": self.cmd_agent,
            "/image": self.cmd_image,
            "/read": self.cmd_read,
            "/search": self.cmd_search,
            "/session": self.cmd_session,
            "/compact": self.cmd_compact,
            "/clear": self.cmd_clear,
            "/cost": self.cmd_cost,
            "/jobs": self.cmd_jobs,
            "/memory": self.cmd_memory,
            "/remember": self.cmd_remember,
            "/reminders": self.cmd_reminders,
            "/init": self.cmd_init,
            "/cd": self.cmd_cd,
            "/doctor": self.cmd_doctor,
            "/stream": self.cmd_stream,
            "/voice": self.cmd_voice,
            "/say": self.cmd_say,
            "/speak": self.cmd_speak,
            "/voice-setup": self.cmd_voice_setup,
            "/barehands-setup": self.cmd_barehands_setup,
            "/board": self.cmd_board,
            "/board-state": self.cmd_board_state,
        }
        handler = handlers.get(cmd)
        if not handler:
            self.ui.warn(f"Unknown command {cmd}. /help lists them all.")
            return
        result = handler(args)
        if asyncio.iscoroutine(result):
            await result

    def cmd_help(self, args: list[str]) -> None:
        self.ui.table(
            "Commands", ["command", "what it does"],
            [[name, help_text] for name, help_text in COMMANDS.items()],
        )
        self.ui.print(
            "\n[dim]Anything else is a message to HELENA. Paste an image path in a message "
            "to have it looked at. Ctrl-C interrupts a running turn; Ctrl-D exits.[/dim]"
        )

    async def cmd_model(self, args: list[str]) -> None:
        if not args:
            health = await self.client.health()
            self.ui.print(f"Model: [bold]{self.config.model or health.get('default_model')}[/bold]"
                          f"{' (server default)' if not self.config.model else ''}")
            return
        name = args[0]
        self.config.model = name
        self.agent.model = name
        self.ui.print(f"Model set to [bold]{name}[/bold] for this session. "
                      f"[dim]Persist it with a `model` entry in {self.config.project_settings_path}[/dim]")

    async def cmd_models(self, args: list[str]) -> None:
        try:
            data = await self.client.models()
        except ServerError as exc:
            self.ui.error(str(exc))
            return
        rows = []
        for m in data.get("models", []):
            size = f"{(m.get('size') or 0) / 1e9:.1f} GB" if m.get("size") else "—"
            caps = " ".join(filter(None, [
                "tools" if m.get("supports_tools") else "",
                "vision" if m.get("supports_vision") else "",
            ])) or "—"
            rows.append([m["name"], m.get("parameter_size") or "—", size, caps])
        if not rows:
            self.ui.warn("No models are pulled. Try `/pull qwen2.5:7b-instruct`.")
            return
        self.ui.table("Available models", ["name", "params", "size", "capabilities"], rows)
        self.ui.notice(f"server default: {data.get('default_model')} · vision: {data.get('vision_model')}")

    async def cmd_pull(self, args: list[str]) -> None:
        if not args:
            self.ui.warn("Usage: /pull <model>, e.g. /pull qwen2.5:7b-instruct")
            return
        name = args[0]
        self.ui.notice(f"Pulling {name} — this can take a while.")
        last = ""
        try:
            async for event in self.client.pull(name):
                if event.get("type") == "error":
                    self.ui.error(event["error"])
                    return
                if event.get("type") == "done":
                    self.ui.print(f"[green]Pulled {name}.[/green]")
                    return
                status = event.get("status", "")
                percent = event.get("percent")
                line = f"{status} {percent}%" if percent is not None else status
                if line != last:
                    self.ui.notice(line)
                    last = line
        except ServerError as exc:
            self.ui.error(str(exc))

    def cmd_mode(self, args: list[str]) -> None:
        if not args:
            self.ui.print(f"Permission mode: [bold]{self.config.mode}[/bold]")
            self.ui.notice("ask = confirm writes/commands · auto = edits pre-approved · "
                           "plan = read-only · yolo = no prompts")
            return
        mode = args[0].lower()
        if mode not in VALID_MODES:
            self.ui.warn(f"Mode must be one of: {', '.join(VALID_MODES)}")
            return
        self.permissions.set_mode(mode)
        self.ui.print(f"Permission mode: [bold]{mode}[/bold]")
        if mode == "yolo":
            self.ui.warn("Every tool call now runs without asking. Catastrophic commands are still blocked.")

    def cmd_workspace(self, args: list[str]) -> None:
        if not args:
            state = "unlocked — files anywhere can be read/edited" if self.config.allow_outside_workspace \
                else f"locked to {self.config.workspace}"
            self.ui.print(f"Workspace: [bold]{state}[/bold]")
            self.ui.notice("/workspace unlock to allow file tools outside this folder · /workspace lock to restore the guard")
            return
        choice = args[0].lower()
        if choice in ("unlock", "allow", "on"):
            self.config.allow_outside_workspace = True
            self.ui.warn("Workspace guard lifted — read_file/write_file/edit_file/etc. can now touch anything on this Mac, "
                         "not just this project. (This never fully applied to run_command, which could already reach "
                         "outside the workspace via the shell — this just extends the same trust to the structured tools.)")
        elif choice in ("lock", "off"):
            self.config.allow_outside_workspace = False
            self.ui.print("Workspace guard restored — file tools confined to this project again.")
        else:
            self.ui.warn("Usage: /workspace [unlock|lock]")

    def cmd_trust(self, args: list[str]) -> None:
        """One command for 'stop asking, and let it touch my whole Mac' — exactly
        yolo mode plus an unlocked workspace, named for what it actually does."""
        self.permissions.set_mode("yolo")
        self.config.allow_outside_workspace = True
        self.ui.warn(
            "Full trust enabled: no permission prompts, and file tools can reach anywhere on this Mac, not just "
            "this project folder. Genuinely destructive commands (formatting a disk, `rm -rf /`, force-pushing "
            "over history, and the like) are still hard-blocked regardless — that check never gets bypassed. "
            "Dial it back anytime with /mode ask and /workspace lock."
        )

    def cmd_permissions(self, args: list[str]) -> None:
        if not args or args[0] == "list":
            self.ui.table(
                "Permission rules", ["kind", "rule"],
                [["allow", r] for r in self.config.allow] + [["deny", r] for r in self.config.deny]
                or [["—", "(none — mode defaults apply)"]],
            )
            self.ui.notice(f"mode: {self.config.mode} · session grants: {len(self.permissions.session_allow)}")
            return
        action = args[0].lower()
        if action in ("allow", "deny") and len(args) > 1:
            rule = " ".join(args[1:])
            self.config.add_rule(action, rule)
            self.ui.print(f"Added {action} rule [bold]{rule}[/bold] "
                          f"[dim](saved to {self.config.project_settings_path})[/dim]")
            return
        if action == "reset":
            self.config.allow.clear()
            self.config.deny.clear()
            self.permissions.session_allow.clear()
            self.ui.print("In-memory rules cleared. Files on disk are untouched.")
            return
        self.ui.warn("Usage: /permissions [list | allow <rule> | deny <rule> | reset]")

    def cmd_tools(self, args: list[str]) -> None:
        rows = []
        for tool in self.agent.tools:
            summary = " ".join(tool.description.split())[:88]
            rows.append([tool.name, tool.action.value, summary])
        self.ui.table("Tools", ["name", "permission", "what it does"], rows)

    def cmd_agents(self, args: list[str]) -> None:
        rows = [[spec.name, ", ".join(spec.tools[:4]) + ("…" if len(spec.tools) > 4 else ""),
                 " ".join(spec.description.split())[:70]] for spec in AGENT_SPECS.values()]
        self.ui.table("Subagents", ["type", "tools", "purpose"], rows)
        self.ui.notice("HELENA delegates on its own; /agent <type> <task> runs one directly.")

    async def cmd_agent(self, args: list[str]) -> None:
        if len(args) < 2:
            self.ui.warn("Usage: /agent <type> <task>, e.g. /agent explorer where is auth handled")
            return
        agent_type, task = args[0], " ".join(args[1:])
        if agent_type not in AGENT_SPECS:
            self.ui.warn(f"Unknown agent {agent_type!r}. Known: {', '.join(AGENT_SPECS)}")
            return
        await self._guarded_turn(
            f"Use the spawn_agent tool with agent_type={agent_type!r} for this task, then "
            f"relay its report: {task}"
        )

    async def cmd_image(self, args: list[str]) -> None:
        if not args:
            self.ui.warn("Usage: /image <path> [question]")
            return
        raw_path = args[0]
        question = " ".join(args[1:]).strip()
        path = Path(os.path.expanduser(raw_path))
        if not path.is_absolute():
            path = self.config.workspace / path
        if not path.is_file():
            self.ui.warn(f"No such file: {path}")
            return
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            self.ui.warn(f"{path.name} isn't a supported image type ({', '.join(sorted(IMAGE_SUFFIXES))}).")
            return

        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        if await self._chat_model_sees_images():
            self._pending_images = [encoded]
            await self._guarded_turn(question or f"Look at this image ({path.name}) and tell me what's in it.")
            return

        # The chat model can't take images, so describe it with the vision
        # model and hand the text to the conversation. Slightly lossy, but it
        # works with any local setup instead of erroring out.
        self.ui.notice(f"Chat model isn't multimodal — describing {path.name} with the vision model first.")
        try:
            described = await self.client.vision(
                [encoded], question or "Describe this image in detail, including any text.",
                model=self.config.vision_model or None,
            )
        except ServerError as exc:
            self.ui.error(f"{exc}\nPull a vision model first, e.g. `/pull llava`.")
            return
        if not described.content.strip():
            self.ui.warn("The vision model returned nothing. Is a multimodal model pulled?")
            return
        self.ui.print(f"[dim]{described.model} saw:[/dim]")
        self.ui.render_markdown(described.content)
        await self._guarded_turn(
            f'I shared the image "{path.name}". A vision model described it as:\n\n'
            f"{described.content}\n\n{question or 'What do you make of it?'}"
        )

    async def _chat_model_sees_images(self) -> bool:
        try:
            data = await self.client.models()
        except ServerError:
            return False
        target = self.config.model or data.get("default_model", "")
        return any(m["name"] == target and m.get("supports_vision") for m in data.get("models", []))

    async def cmd_read(self, args: list[str]) -> None:
        if not args:
            self.ui.warn("Usage: /read <path>")
            return
        path = Path(os.path.expanduser(args[0]))
        if not path.is_absolute():
            path = self.config.workspace / path
        if not path.is_file():
            self.ui.warn(f"No such file: {path}")
            return
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError as exc:
            self.ui.error(str(exc))
            return
        clipped = text[:20_000]
        self.ctx.read_files[str(path.resolve())] = path.stat().st_mtime
        self.agent.messages.append({
            "role": "user",
            "content": f"Here is `{path}` for reference:\n\n```\n{clipped}\n```"
                       + ("\n[truncated]" if len(text) > len(clipped) else ""),
        })
        self.agent.messages.append({
            "role": "assistant",
            "content": f"Got {path.name} ({len(text.splitlines())} lines). What would you like to do with it?",
        })
        self.ui.print(f"Loaded [bold]{path.name}[/bold] into the conversation "
                      f"({len(text.splitlines())} lines{', truncated' if len(text) > len(clipped) else ''}).")

    async def cmd_search(self, args: list[str]) -> None:
        if not args:
            self.ui.warn("Usage: /search <query>")
            return
        from .tools.web import WebSearchTool

        tool = WebSearchTool()
        self.ui.notice(f"Searching for {' '.join(args)!r}…")
        result = await tool.run({"query": " ".join(args)}, self.ctx)
        self.ui.print(result.content)

    def cmd_session(self, args: list[str]) -> None:
        sub = args[0] if args else "list"
        if sub == "list":
            rows = [[r["id"], r["age"], str(r["messages"]), r["title"]]
                    for r in list_transcripts(self.config.transcript_dir)]
            if not rows:
                self.ui.notice("No saved sessions yet.")
                return
            self.ui.table("Sessions", ["id", "when", "msgs", "title"], rows)
            return
        if sub == "save":
            path = self.transcript.save(self.agent.messages, self.agent.totals)
            self.ui.print(f"Saved to {path}")
            return
        if sub == "new":
            self.agent.messages.clear()
            self.transcript = Transcript.new(self.config.transcript_dir, model=self.config.model)
            self.ui.print("Started a new session.")
            return
        if sub == "load":
            needle = args[1] if len(args) > 1 else "last"
            path = find_transcript(self.config.transcript_dir, needle)
            if not path:
                self.ui.warn(f"No session matching {needle!r}.")
                return
            loaded = Transcript.load(path)
            self.agent.messages = [m for m in loaded.messages if not m.get("images")]
            self.transcript = loaded
            self.ui.print(f"Loaded session [bold]{loaded.id}[/bold] "
                          f"({len(self.agent.messages)} messages): {loaded.title}")
            return
        self.ui.warn("Usage: /session [list | new | save | load <id>]")

    def cmd_compact(self, args: list[str]) -> None:
        removed = self.agent.compact()
        self.ui.print(f"Compacted history — dropped {removed} tool message(s), "
                      f"{len(self.agent.messages)} remain.")

    def cmd_clear(self, args: list[str]) -> None:
        self.agent.messages.clear()
        self.ctx.todos.clear()
        self.ui.print("Conversation cleared.")

    def cmd_cost(self, args: list[str]) -> None:
        totals = self.agent.totals
        stats = self.permissions.stats
        self.ui.table(
            "This session", ["metric", "value"],
            [
                ["turns", f"{int(totals['turns'])}"],
                ["tool calls", f"{int(totals['tool_calls'])}"],
                ["prompt tokens", f"{int(totals['prompt_tokens']):,}"],
                ["completion tokens", f"{int(totals['completion_tokens']):,}"],
                ["model time", f"{totals['seconds']:.1f}s"],
                ["permissions", f"{stats['allowed']} allowed · {stats['denied']} denied · {stats['asked']} asked"],
                ["cost", "$0.00 — everything ran locally"],
            ],
        )

    def cmd_jobs(self, args: list[str]) -> None:
        if not self.ctx.jobs:
            self.ui.notice("No background jobs.")
            return
        rows = []
        for job in self.ctx.jobs.values():
            state = "running" if job.proc.returncode is None else f"exit {job.proc.returncode}"
            rows.append([job.id, state, job.command[:60], str(job.stdout_path)])
        self.ui.table("Background jobs", ["id", "state", "command", "log"], rows)

    def cmd_memory(self, args: list[str]) -> None:
        path = self.config.memory_path
        if not args:
            if path.is_file():
                self.ui.print(f"[dim]{path}[/dim]")
                self.ui.render_markdown(path.read_text("utf-8")[:4000])
            else:
                self.ui.notice(f"No {path.name} yet. /init writes one, or /memory <line> appends.")
            return
        line = " ".join(args)
        with path.open("a", encoding="utf-8") as fh:
            if not path.stat().st_size:
                fh.write(f"# {self.config.workspace.name}\n\n")
            fh.write(f"- {line}\n")
        self.ui.print(f"Added to {path.name}: {line}")

    def cmd_remember(self, args: list[str]) -> None:
        if not args:
            facts = profile_store.load_profile()["facts"]
            if not facts:
                self.ui.notice("Nothing remembered yet. /remember <fact> saves one.")
                return
            self.ui.table("Remembered", ["when", "fact"],
                          [[f["created_at"][:10], f["text"]] for f in facts])
            return
        fact = " ".join(args)
        profile_store.add_fact(fact)
        self.ui.print(f"Noted: {fact}")

    def cmd_reminders(self, args: list[str]) -> None:
        pending = profile_store.pending_reminders()
        if not pending:
            self.ui.notice("No pending reminders.")
            return
        self.ui.table("Reminders", ["when", "what"],
                      [[r["when_iso"] or f'unparsed ("{r["when_text"]}")', r["text"]] for r in pending])

    async def cmd_init(self, args: list[str]) -> None:
        path = self.config.memory_path
        if path.is_file() and "--force" not in args:
            self.ui.warn(f"{path.name} already exists. /init --force overwrites it.")
            return
        await self._guarded_turn(
            "Explore this project and write a HELENA.md at the workspace root. Look at the "
            "directory structure, the build/dependency files, the test setup, and a few "
            "representative source files. The file should cover: what this project is, how to "
            "build/run/test it, the layout, and any conventions a new contributor would need "
            "(naming, error handling, formatting). Be specific to what you actually find — no "
            "generic advice. Keep it under 60 lines, and write it with write_file."
        )

    def cmd_cd(self, args: list[str]) -> None:
        if not args:
            self.ui.print(f"Workspace: {self.config.workspace}")
            return
        target = Path(os.path.expanduser(args[0]))
        if not target.is_absolute():
            target = self.config.workspace / target
        if not target.is_dir():
            self.ui.warn(f"Not a directory: {target}")
            return
        self.config.workspace = target.resolve()
        self.ctx.workspace = target.resolve()
        self.ui.print(f"Workspace: [bold]{self.config.workspace}[/bold]")

    async def cmd_doctor(self, args: list[str]) -> None:
        rows: list[list[str]] = []
        try:
            health = await self.client.health()
            rows.append(["server", f"ok — {self.config.server_url} (v{health.get('version')})"])
            rows.append([
                "ollama",
                f"ok — {health.get('ollama_host')} (v{health.get('ollama_version')}), "
                f"{health.get('model_count')} models"
                if health.get("ollama_reachable") else f"unreachable — {health.get('detail')}",
            ])
        except ServerError as exc:
            rows.append(["server", f"unreachable — {exc}"])
            self.ui.table("Doctor", ["check", "status"], rows)
            return

        try:
            data = await self.client.models()
            names = [m["name"] for m in data.get("models", [])]
            chat_model = self.config.model or data.get("default_model", "")
            rows.append(["chat model", f"{chat_model} — {'pulled' if chat_model in names else 'NOT pulled'}"])
            tool_capable = [m["name"] for m in data.get("models", []) if m.get("supports_tools")]
            vision_capable = [m["name"] for m in data.get("models", []) if m.get("supports_vision")]
            rows.append(["tool-calling models", ", ".join(tool_capable) or "none found — agentic work needs one"])
            rows.append(["vision models", ", ".join(vision_capable) or "none — /pull llava for image support"])
        except ServerError as exc:
            rows.append(["models", str(exc)])

        rows.append(["workspace", str(self.config.workspace)])
        rows.append(["permission mode", self.config.mode])
        rows.append(["settings", str(self.config.project_settings_path)
                     if self.config.project_settings_path.is_file() else "(none — using defaults)"])

        voice_problem = voice.check_available()
        rows.append(["voice input (stt)", "ready" if voice_problem is None else "needs install — run /voice-setup"])
        rows.append([
            "voice output (tts)",
            "configured" if self.config.elevenlabs_api_key else "no ELEVENLABS_API_KEY set — run /voice-setup",
        ])
        rows.append(["python interpreter", sys.executable])

        if bh.is_configured(self.config):
            alive = await bh.server_alive(self.config)
            rows.append([
                "barehands board",
                f"configured at {self.config.barehands_path} — "
                + ("server responding" if alive else "server NOT responding, run /barehands-setup"),
            ])
        else:
            rows.append(["barehands board", "not set up — run /barehands-setup"])
        self.ui.table("Doctor", ["check", "status"], rows)

    def cmd_stream(self, args: list[str]) -> None:
        if args and args[0] in ("on", "off"):
            self.config.stream = args[0] == "on"
        self.ui.print(f"Streaming: [bold]{'on' if self.config.stream else 'off'}[/bold]")

    # --- voice ---------------------------------------------------------

    async def cmd_voice(self, args: list[str]) -> None:
        """Record from the microphone until Enter, transcribe locally, then
        feed the result through the exact same path as typed text — so
        everything downstream (image-path detection, tool use, permission
        prompts) behaves identically whether you spoke or typed it."""
        problem = voice.check_available()
        if problem:
            self.ui.warn(problem)
            return

        self.ui.print("[dim]🎙️  Recording — press Enter when you're done talking.[/dim]")
        bh.write_ring_state(self.config, "listening")
        try:
            audio = await voice.record_until_enter()
        except Exception as exc:
            self.ui.error(f"Couldn't record audio: {exc}")
            return
        finally:
            bh.write_ring_state(self.config, "idle")

        self.ui.print("[dim]Transcribing...[/dim]")
        try:
            text = await voice.transcribe(audio, self.config.voice_input_model)
        except Exception as exc:
            self.ui.error(f"Transcription failed: {exc}")
            return

        if not text:
            self.ui.warn("Didn't catch anything — try again, and make sure the right input device is selected.")
            return

        self.ui.print(f"[dim]you (voice) ›[/dim] {text}")
        await self.handle_line(text)

    async def cmd_say(self, args: list[str]) -> None:
        text = " ".join(args).strip()
        if not text:
            self.ui.warn("Usage: /say <text>")
            return
        bh.write_ring_state(self.config, "speaking")
        try:
            await tts.speak(text, self.config.elevenlabs_api_key, self.config.elevenlabs_voice_id)
        except tts.TTSError as exc:
            self.ui.error(str(exc))
        finally:
            bh.write_ring_state(self.config, "idle")

    def cmd_speak(self, args: list[str]) -> None:
        if args and args[0] in ("on", "off"):
            if args[0] == "on" and not self.config.elevenlabs_api_key:
                self.ui.warn("No ElevenLabs API key configured yet — run /voice-setup for the steps, then try again.")
                return
            self.config.speak_replies = args[0] == "on"
        self.ui.print(f"Auto-speak replies: [bold]{'on' if self.config.speak_replies else 'off'}[/bold]")

    async def cmd_voice_setup(self, args: list[str]) -> None:
        stt_problem = voice.check_available()
        stt_status = "ready" if stt_problem is None else "needs install (see below)"
        tts_status = "configured" if self.config.elevenlabs_api_key else "needs an API key (see below)"

        shell = os.environ.get("SHELL", "")
        if "zsh" in shell:
            profile_file = "~/.zshrc"
        elif "bash" in shell:
            profile_file = "~/.bash_profile"
        else:
            profile_file = "your shell's profile file (~/.zshrc on modern macOS)"

        self.ui.print(f"""
[bold]Voice input (speech-to-text)[/bold] — {stt_status}
  Fully local via faster-whisper, no account or key needed.
  Running Python: {sys.executable}
  1. In the SAME terminal you run `helena` from: source .venv/bin/activate
  2. pip install -e ".[voice]"
  3. Exit this session (/exit) and start a brand-new `helena` — an already-running
     process keeps whatever packages it started with, so re-running /voice right after
     installing in another tab won't pick it up.
  4. /voice — records until you press Enter, transcribes, sends it like typed text
{f'  Current problem: {stt_problem}' if stt_problem else ''}

[bold]HELENA's spoken voice (text-to-speech)[/bold] — {tts_status}
  Uses ElevenLabs — the one part of this project that isn't free/local, since
  no local TTS sounds like a real voice yet.
  1. Sign up at https://elevenlabs.io and open Settings → API Keys
  2. Make the key permanent — a plain `export` only lasts until you close this
     terminal tab. Add it to your shell profile instead, then reload it:
       echo 'export ELEVENLABS_API_KEY=your-key-here' >> {profile_file}
       source {profile_file}
  3. Confirm it stuck: echo $ELEVENLABS_API_KEY   — should print your key, not blank
  4. Optional: pick a voice at https://elevenlabs.io/voice-library, copy its
     voice ID, and add it the same way:
       echo 'export ELEVENLABS_VOICE_ID=that-id' >> {profile_file}
  5. /say testing one two three   — to confirm it works
  6. /speak on                     — to have every reply spoken automatically

  If /say still fails after this, it's almost always: the key was exported in a
  different terminal tab than the one running `helena`, or `helena` was already
  running when you edited {profile_file} (env vars are only read once, at startup —
  /exit and start `helena` fresh after editing your profile).
""")

    # --- barehands ---------------------------------------------------------

    async def cmd_barehands_setup(self, args: list[str]) -> None:
        """Clone (if needed), start, and connect the barehands hand-tracked
        board — github.com/jaredrhod/barehands. barehands itself has zero
        dependencies (stdlib-only Python), so this is just: get the code,
        run it, tell HELENA where it lives. No pip install step exists
        because none is needed.
        """
        target = Path(os.path.expanduser(args[0])).resolve() if args else (Path.home() / "barehands")

        if bh.repo_path(self.config) == target and await bh.server_alive(self.config):
            self.ui.print(
                f"barehands is already set up at [bold]{target}[/bold] and its server is responding. "
                "Nothing to do — /board-state to look at it, or /board to put something up."
            )
            return

        if (target / "server.py").is_file():
            self.ui.notice(f"Found an existing barehands checkout at {target} — skipping clone.")
        else:
            if target.exists() and any(target.iterdir()):
                self.ui.warn(f"{target} already exists and isn't empty. Pass a different path: "
                             f"/barehands-setup ~/somewhere-else")
                return
            self.ui.notice(f"Cloning github.com/jaredrhod/barehands into {target}…")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "clone", "https://github.com/jaredrhod/barehands", str(target),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await proc.communicate()
            except FileNotFoundError:
                self.ui.error("`git` isn't installed or isn't on PATH — install git and try again.")
                return
            if proc.returncode != 0:
                self.ui.error(f"git clone failed:\n{out.decode('utf-8', 'replace')[-1500:]}")
                return
            self.ui.print(f"[green]Cloned to {target}.[/green]")

        # Read the port it'll actually listen on (barehands.json, or the
        # server's own default) before starting it, so we know what to poll.
        port = 8794
        config_path = target / "barehands.json"
        if config_path.is_file():
            try:
                import json
                port = int(json.loads(config_path.read_text("utf-8")).get("port", 8794))
            except (OSError, ValueError, TypeError):
                pass

        already_running = False
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(f"http://127.0.0.1:{port}/config", timeout=2.0)
                already_running = res.status_code == 200
        except httpx.HTTPError:
            already_running = False

        if already_running:
            self.ui.notice(f"Something is already answering on port {port} that looks like barehands — using it as-is.")
        else:
            self.ui.notice(f"Starting the barehands server on port {port}…")
            from .tools.shell import RunCommandTool

            runner = RunCommandTool()
            started = await runner._run_background(f'"{sys.executable}" server.py', target, self.ctx)
            job_id = started.meta.get("job_id")
            job = self.ctx.jobs.get(job_id) if job_id else None
            if job is None:
                self.ui.error(f"Couldn't start it: {started.content}")
                return

            deadline = time.monotonic() + 8
            alive = False
            while time.monotonic() < deadline:
                if job.proc.returncode is not None:
                    break
                try:
                    async with httpx.AsyncClient() as client:
                        res = await client.get(f"http://127.0.0.1:{port}/config", timeout=1.0)
                        if res.status_code == 200:
                            alive = True
                            break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.4)

            if job.proc.returncode is not None:
                try:
                    log = job.stdout_path.read_text("utf-8", errors="replace")
                except OSError:
                    log = ""
                self.ui.error(f"The server exited immediately:\n{log[-1000:]}")
                return
            if not alive:
                self.ui.warn(
                    f"Started it (job {job_id}), but it isn't answering on port {port} yet. "
                    f"Check on it with /jobs or check_job(job_id=\"{job_id}\"); a busy port "
                    "usually means another barehands (or something else) already owns it."
                )
            else:
                self.ui.print(f"[green]barehands is up (job {job_id}).[/green]")

        self.config.barehands_path = str(target)
        self.config.barehands_port = port
        saved_to = self.config.save_user()
        self.ui.print(f"[dim]Saved to {saved_to} — every project now knows where barehands lives.[/dim]")

        self.ui.print(f"""
[bold]Next[/bold]
  1. Open [bold]http://127.0.0.1:{port}/stage.html[/bold] in Chrome and allow the camera.
  2. Wave — tap the ring to bloom the orbs, pinch a card to move it.
  3. HELENA can now use board_command / board_state / board_stage_media, and
     the ring already mirrors HELENA's state (idle/listening/thinking/speaking)
     automatically from now on — nothing else to wire up.

[bold]For the Stark-tech projector look[/bold]
  - Full-screen that Chrome tab on whichever display you're going to mirror
    (Cmd+Ctrl+F), then Control Center → Screen Mirroring → your projector's
    AirPlay device. AirPlay mirrors the whole display, so put the board on
    its own Space/display and mirror that one.
  - Camera stays pointed at your hands, not at the projected image, or the
    tracker will confuse the projection for a second hand.

barehands is stdlib-only Python — nothing else to install. It's a separate
project (github.com/jaredrhod/barehands, AGPL-3.0-or-later, © Jared
Rhodenizer) that HELENA talks to over http://127.0.0.1:{port} — nothing
here is bundled into HELENA itself.
""")

    async def cmd_board(self, args: list[str]) -> None:
        if not args:
            self.ui.warn(
                'Usage: /board <action> [key=value ...], e.g. '
                '/board present src=models/car.glb title="My Car"'
            )
            self.ui.notice(f"Actions: {', '.join(bh.ALLOWED_ACTIONS)}")
            return
        action, rest = args[0], args[1:]
        command: dict[str, Any] = {"a": action}
        try:
            tokens = shlex.split(" ".join(rest))
        except ValueError as exc:
            self.ui.warn(f"Couldn't parse that (unbalanced quotes?): {exc}")
            return
        for token in tokens:
            if "=" not in token:
                self.ui.warn(f"Ignoring {token!r} — expected key=value.")
                continue
            key, _, value = token.partition("=")
            command[key] = value

        try:
            status, body = await bh.post_command(self.config, command)
        except bh.BarehandsError as exc:
            self.ui.error(str(exc))
            return
        if status == 204:
            self.ui.print(f"[green]Board took it: {action}[/green]")
        else:
            self.ui.error(f"Board rejected it (HTTP {status}). {body[:200]}")

    async def cmd_board_state(self, args: list[str]) -> None:
        try:
            state = await bh.get_state(self.config)
        except bh.BarehandsError as exc:
            self.ui.error(str(exc))
            return
        self.ui.print(bh.describe_state(state))
