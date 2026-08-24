"""The agentic loop.

One `send()` is one turn from the user's point of view, but internally it runs
until the model stops asking for tools: stream a completion, execute whatever
tools it requested (each one gated by the permission engine), feed the real
results back, repeat. Nothing here decides whether a tool *may* run — that is
`permissions.py` — and nothing here formats output — that is `ui.py`.
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from . import barehands_client as bh
from .client import ServerError
from .permissions import Decision, PermissionRequest
from .tools.base import Tool, ToolContext, ToolError, ToolResult, truncate

SYSTEM_PROMPT = """You are {name}, a local-first AI agent working in a terminal on the user's own machine. You run entirely on locally-hosted models — nothing the user says leaves their computer.

Your character: sharp, direct, warm without being chatty. You are a capable colleague, not a customer-service voice. Say what you did and what you found; skip the preamble and the flattery.

## How you work

You have real tools. When a request genuinely calls for one, use it — do not describe what you would do, and never claim you did something you did not actually do through a tool call. The user sees every tool you run, so inventing an action is both wrong and obvious. The reverse matters just as much: plenty of requests need no tool at all — general knowledge, opinions, explaining a concept, simple facts you already know (capitals, common conversions, well-known history) — and calling one anyway just to look busy is its own kind of noise. Answer those directly. Reach for a tool when the honest answer requires doing something real: reading a file that exists, checking a live price, running a command, searching for something you don't actually know.

- Investigate before you change anything. Read the file before editing it; search before assuming a symbol exists.
- Prefer the specific tool over a shell command: read_file over cat, search_text over grep, find_files over find, edit_file over sed.
- A fenced code block in your reply is not a deliverable — it's something the user has to copy out by hand, which defeats the entire point of having file tools. If you're producing a real file the project needs (not a short snippet answering a question about how something works), write it for real. For a full-stack app or any multi-file project: plan the file tree with todo_write, then create every file — package.json, every source file, config, all of it — in one create_project call rather than one write_file call per file; a single call is both faster and far less likely to leave something half-scaffolded than remembering to call write_file N separate times. Never respond to "build me X" with a wall of code blocks and instructions to paste them in.
- For starting a local dev server (a JS/Python project, a demo, "run this on a port"), use run_dev_server — it detects the right start command and reports the actual URL, instead of you guessing a port. After scaffolding something runnable, actually start it with run_dev_server and give the user the real working URL — don't just describe the commands they'd need to run themselves. For any other long-running background process, use run_command with background: true, then check_job to see its output. spawn_agent is a different thing entirely: delegating a self-contained task to a separate conversation, not a way to keep a process alive — and when work genuinely splits into independent parts (a frontend and a backend, several unrelated modules), issue those spawn_agent calls together in the same turn rather than one after another; independent subagents run concurrently, which matters a lot for something like a full-stack scaffold. Only split work this way when the parts don't depend on each other finishing first.
- After changing code, verify it: run the tests, the linter, or the program itself.
- Work in small, checkable steps. For anything with more than about three steps, keep todo_write updated so the user can see where you are.
- If a tool fails, read the error and adapt. Do not repeat the identical call and hope — if the exact same call fails twice in a row, that is a signal to change something (the arguments, the approach, or ask the user), not to try again unchanged.
- If the user's request is ambiguous in a way that changes what you would build, ask. Otherwise make the sensible call and say what you assumed.
- Some tools need the user's approval. If one is declined, do not try to route around it — say what you needed and why, and offer an alternative. If the same call gets declined twice, stop entirely: say plainly that you're blocked waiting on approval, and let the user decide (approve it explicitly, switch to auto/yolo mode, or a different approach) rather than asking a third time.

## Never fake a tool call

This is the single most important rule. If you are not making a real tool call this turn, do not write anything that looks like one: no arrow-bulleted or bracketed status lines naming a tool or an action, no backtick-wrapped pseudo function-call syntax, no invented tool names. The interface renders real tool activity on its own the moment you actually call one — text imitating that display is indistinguishable from a lie to the user reading it. For a multi-step task, call one real tool, look at its real result, then decide the next step — never narrate the whole plan as prose and call it done. If you find yourself about to describe running something, stop and either call the actual tool or say plainly that you haven't done it yet.

Your only real tools, by name, are: {tool_names}. No other tool name is real. If none of these fit what's being asked, say so — don't invent one that sounds plausible.

## Answering

Be concise by default: a couple of sentences beats a report. Expand when the user asks for depth or the material genuinely needs it. Use markdown for structure when it helps and skip it when it doesn't. Reference code as `path:line` so the user can jump to it. Give exactly one reply per turn — never write the user's next message for them.

## Environment

{environment}
{memory}{profile}{recall}{board}"""

MAX_INLINE_TOOL_SCAN = 4000

# Matches the specific failure this guards against: asked to build something,
# the model answers with a pile of fenced code blocks and instructions to
# copy them in, instead of actually calling write_file. A couple of short
# snippets answering "how would I do X" is normal and should never trigger
# this — it takes a real build-shaped request AND a substantial wall of code
# blocks together before the loop bothers to self-correct.
_BUILD_REQUEST_RE = re.compile(
    r"\b(build|create|make|scaffold|set ?up|generate|write)\b[^.?!]{0,40}\b"
    r"(app|project|site|website|api|backend|frontend|full[- ]?stack|game|server)\b",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# The synthetic pseudo-tool name used to bounce a near-miss inline-recovered
# tool call back to the model as a diagnostic, instead of either running it
# with wrong arguments or silently discarding it. See _inline_tool_calls.
_MISMATCH_MARKER = "__unrecognized_tool_call__"

# How many times the identical (tool, key) pair may be denied before the loop
# stops relaying "try again" back to the model and instead forces it to stop
# and say so to the user. See _execute_call.
MAX_IDENTICAL_DENIALS = 2


def _looks_like_pasted_files(reply_text: str, triggering_request: str) -> bool:
    if not _BUILD_REQUEST_RE.search(triggering_request or ""):
        return False
    blocks = _CODE_FENCE_RE.findall(reply_text or "")
    if len(blocks) < 2:
        return False
    return sum(len(b) for b in blocks) > 300


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: int = 0
    iterations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    stopped: str = "complete"     # complete | max_iterations | error | interrupted
    error: str = ""


@dataclass
class Agent:
    """A conversation with a model plus the tools it is allowed to use."""

    ctx: ToolContext
    tools: Sequence[Tool]
    label: str = "HELENA"
    model: str | None = None
    max_iterations: int | None = None
    system_prompt: str | None = None
    nested: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    totals: dict[str, float] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0, "turns": 0, "tool_calls": 0}
    )
    # (tool_name, permission_key) -> consecutive-denial count. Never reset
    # mid-conversation on purpose: the whole point is to catch a call that
    # keeps getting asked for and keeps getting declined, across iterations
    # and across turns, and make the loop stop relaying "do not retry" and
    # actually force a stop instead of trusting a small local model to
    # honor that instruction on its own. See _execute_call.
    _denial_counts: dict[tuple[str, str], int] = field(default_factory=dict, repr=False)
    # Same idea, but for a tool call that keeps *failing* with the identical
    # error rather than being declined — the other half of "it keeps saying
    # let me try that again, in a loop". The most common real case: a write
    # outside the confined workspace (resolve_path's ToolError) that the
    # model retries unchanged because the error text alone didn't stop it.
    # Keyed on (tool, key, error text) so a *different* error on the same
    # file — genuine progress — doesn't trip the breaker.
    _error_counts: dict[tuple[str, str, str], int] = field(default_factory=dict, repr=False)
    # Cross-project memory recalled for the *current* turn, computed once in
    # `send()` (not per iteration — the tool loop can call `_complete` several
    # times before a turn is done, and the user's question doesn't change
    # mid-turn). See `recall_memory`.
    _memory_block: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        self._by_name = {t.name: t for t in self.tools}

    # --- prompt assembly ---------------------------------------------------

    def environment_block(self) -> str:
        cfg = self.ctx.config
        lines = [
            f"Working directory: {self.ctx.workspace}",
            f"Platform: {platform.system()} {platform.release()} ({platform.machine()})",
            f"Today: {datetime.now().strftime('%A, %d %B %Y')}",
            f"Permission mode: {cfg.mode}"
            + {
                "ask": " — writes and commands need the user's approval, reads are free.",
                "auto": " — file edits are pre-approved; commands still ask.",
                "plan": " — READ-ONLY. You cannot write files or run commands; investigate and propose instead.",
                "yolo": " — everything is pre-approved. Be correspondingly careful.",
            }.get(cfg.mode, ""),
        ]
        return "\n".join(lines)

    def board_block(self) -> str:
        """Barehands usage guidance — only added when it's actually set up,
        so the prompt stays quiet about a feature that isn't configured."""
        if not bh.is_configured(self.ctx.config):
            return ""
        return (
            "\n\n## The barehands board\n\n"
            "A hand-tracked glass board is running on this machine (localhost only) — "
            "the user's own hands, watched by a camera, moving cards, images, and 3D "
            "models through the air. You have hands and eyes on it:\n"
            "- When the user asks to SEE something (\"show me\", \"put it up\", \"pull up "
            "X\"), don't answer with a wall of text in the terminal — stage it on the "
            "board with board_command and say what you put up. The board is your "
            "show-and-tell; reach for it whenever seeing beats reading.\n"
            "- board_command's a: \"present\" lands something center stage, enlarged and "
            "spotlit, everything else dimmed — the show-me verb.\n"
            "- Only files physically inside the barehands media folder can ever be "
            "staged. If an image or 3D model the user wants shown isn't there yet, call "
            "board_stage_media first — it copies the file in and can present it in one step.\n"
            "- Call board_state before commenting on what's on the board — the user "
            "moves things by hand, so never trust memory over the board's own truth."
        )

    def build_system_prompt(self) -> str:
        if self.system_prompt:
            return self.system_prompt
        from . import profile as profile_store

        memory = self.ctx.config.read_memory()
        memory_block = f"\n\n## Project instructions (from HELENA.md)\n\n{memory}" if memory else ""
        summary = profile_store.context_summary()
        profile_block = f"\n\n## About the user\n\n{summary}" if summary else ""
        recall_block = (
            f"\n\n## Recalled from other sessions\n\n{self._memory_block}" if self._memory_block else ""
        )
        return SYSTEM_PROMPT.format(
            name=self.ctx.config.name,
            environment=self.environment_block(),
            memory=memory_block,
            profile=profile_block,
            recall=recall_block,
            board=self.board_block(),
            tool_names=", ".join(sorted(self._by_name)) or "(none configured)",
        )

    async def recall_memory(self, query: str) -> str:
        """Search cross-project memory for whatever's relevant to this turn.

        Best-effort and silent on failure: a server that's down or an
        embedding model that isn't pulled shouldn't block a turn, and a
        subagent gets its own fixed system_prompt (see build_system_prompt)
        so there's nothing to inject for it.
        """
        if self.nested or not self.ctx.config.memory_enabled or not query.strip():
            return ""
        from . import memory as memory_store

        if not memory_store.count():
            return ""
        try:
            vectors = await self.ctx.client.embed([query], model=self.ctx.config.embed_model or None)
        except ServerError:
            return ""
        if not vectors:
            return ""
        hits = memory_store.search(vectors[0], top_k=self.ctx.config.memory_top_k)
        return "\n".join(f"- {hit['text']}" for hit in hits)

    def tool_specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self.tools]

    # --- history -----------------------------------------------------------

    def trimmed_history(self) -> list[dict[str, Any]]:
        """Keep the conversation inside the context window.

        Trimming has one hard rule: never start the window on a `tool` message
        whose assistant call was dropped — models reject or hallucinate around
        an orphaned result.
        """
        limit = self.ctx.config.history_messages
        if len(self.messages) <= limit:
            return list(self.messages)
        window = self.messages[-limit:]
        while window and window[0].get("role") == "tool":
            window.pop(0)
        first_user = next((m for m in self.messages if m.get("role") == "user"), None)
        if first_user and first_user not in window:
            note = {
                "role": "user",
                "content": f"[earlier context trimmed] The original request was: {first_user.get('content', '')[:800]}",
            }
            window.insert(0, note)
        return window

    def compact(self) -> int:
        """Drop tool chatter, keeping the user/assistant thread. Returns removed count."""
        before = len(self.messages)
        kept = []
        for msg in self.messages:
            if msg.get("role") == "tool":
                continue
            if msg.get("role") == "assistant" and msg.get("tool_calls") and not (msg.get("content") or "").strip():
                continue
            trimmed = dict(msg)
            trimmed.pop("tool_calls", None)
            kept.append(trimmed)
        self.messages = kept
        return before - len(self.messages)

    # --- the loop ----------------------------------------------------------

    async def send(self, user_text: str, images: list[str] | None = None) -> TurnResult:
        message: dict[str, Any] = {"role": "user", "content": user_text}
        if images:
            message["images"] = images
        self.messages.append(message)
        self._memory_block = await self.recall_memory(user_text)
        return await self._run_loop()

    async def _run_loop(self) -> TurnResult:
        result = TurnResult()
        started = time.monotonic()
        max_iterations = self.max_iterations or self.ctx.config.max_iterations
        options = {"num_ctx": self.ctx.config.num_ctx, "temperature": self.ctx.config.temperature}

        for iteration in range(1, max_iterations + 1):
            result.iterations = iteration
            try:
                completion = await self._complete(options)
            except ServerError as exc:
                result.stopped, result.error = "error", str(exc)
                self.ctx.ui.end_stream()
                self.ctx.ui.error(str(exc))
                break
            except asyncio.CancelledError:
                self.ctx.ui.end_stream()
                self.messages.append({
                    "role": "assistant",
                    "content": "[interrupted by the user before finishing]",
                })
                result.stopped = "interrupted"
                raise

            text, tool_calls, usage = completion
            result.text = text or result.text
            result.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            result.completion_tokens += int(usage.get("completion_tokens") or 0)

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": text}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.messages.append(assistant_msg)

            if not tool_calls:
                result.stopped = "complete"
                break

            result.tool_calls += len(tool_calls)
            await self._execute_calls(tool_calls)
        else:
            result.stopped = "max_iterations"
            self.ctx.ui.warn(
                f"Stopped after {max_iterations} steps without a final answer. "
                "Ask me to continue, or raise max_iterations in settings."
            )

        result.seconds = time.monotonic() - started
        self.totals["prompt_tokens"] += result.prompt_tokens
        self.totals["completion_tokens"] += result.completion_tokens
        self.totals["seconds"] += result.seconds
        self.totals["tool_calls"] += result.tool_calls
        self.totals["turns"] += 1
        return result

    async def _complete(self, options: dict[str, Any]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """One model call. Streams when configured, falls back to a plain call."""
        messages = [{"role": "system", "content": self.build_system_prompt()}] + self.trimmed_history()
        specs = self.tool_specs()
        model = self.model or self.ctx.config.model or None

        if not self.ctx.config.stream:
            reply = await self.ctx.client.chat(messages, model=model, tools=specs, options=options)
            if reply.content.strip():
                self.ctx.ui.assistant_prefix(self.label)
                self.ctx.ui.stream_token(reply.content)
                self.ctx.ui.end_stream()
            return reply.content, self._normalize_calls(reply.tool_calls, reply.content), reply.usage

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        printed = False

        async for event in self.ctx.client.chat_stream(messages, model=model, tools=specs, options=options):
            kind = event.get("type")
            if kind == "token":
                if not printed:
                    self.ctx.ui.assistant_prefix(self.label)
                    printed = True
                text_parts.append(event["text"])
                self.ctx.ui.stream_token(event["text"])
            elif kind == "tool_calls":
                tool_calls = event.get("tool_calls") or []
            elif kind == "done":
                usage = event.get("usage") or {}
            elif kind == "error":
                if printed:
                    self.ctx.ui.end_stream()
                raise ServerError(event.get("error", "unknown server error"))

        if printed:
            self.ctx.ui.end_stream()
        text = "".join(text_parts)
        return text, self._normalize_calls(tool_calls, text), usage

    # --- tool calls --------------------------------------------------------

    def _normalize_calls(self, raw_calls: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for i, call in enumerate(raw_calls or []):
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({
                "id": call.get("id") or f"call_{i}",
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": args or {}},
            })
        if not calls and text:
            calls = self._inline_tool_calls(text)
        return calls

    def _inline_tool_calls(self, text: str) -> list[dict[str, Any]]:
        """Recover tool calls from models that emit them as text.

        Plenty of small local models know the *shape* of a tool call but not
        the protocol, and answer with a bare JSON object or a ```json block.
        Parsing that back is the difference between a working agent and one
        that narrates its intentions forever.

        Every candidate is validated against the target tool's actual JSON
        schema before being accepted — a matched tool name is not enough on
        its own. Without that check, a model narrating something like
        `get_weather {"cmd": "npm start"}` would get "recovered" as a real
        call to get_weather with a `cmd` argument that tool doesn't define
        and silently ignores, running the wrong thing with no error. That
        candidate is rejected outright rather than executed.

        But rejecting it outright used to mean discarding it silently — zero
        recovered calls, the turn treated as "complete" with the model's own
        narration as the final answer. For a model that was genuinely trying
        to call a real tool with slightly wrong argument names, that is a
        dead end: nothing ran, no error was ever shown to it, and the only
        way anyone finds out is the user asking again next turn and getting
        the identical silent non-result — which is exactly the "it keeps
        saying let me try that again, in a loop" failure this now fixes. So
        the best schema-mismatched-but-name-recognized candidate is kept and
        returned as a special marker call (_MISMATCH_MARKER) instead of being
        dropped; _execute_call turns that into a concrete, actionable
        diagnostic (the exact argument names it should have used) fed straight
        back into the same turn, instead of silently going nowhere.
        """
        snippet = text.strip()[:MAX_INLINE_TOOL_SCAN]
        if "{" not in snippet:
            return []
        candidates: list[str] = []
        for match in re.finditer(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", snippet, re.DOTALL):
            candidates.append(match.group(1))
        if snippet.startswith("{") and snippet.endswith("}"):
            candidates.append(snippet)
        for match in re.finditer(r'\{[^{}]*"(?:name|tool)"\s*:\s*"[\w_]+".*?\}', snippet, re.DOTALL):
            candidates.append(match.group(0))

        best_mismatch: dict[str, Any] | None = None
        for raw in candidates:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            name = obj.get("name") or obj.get("tool") or obj.get("function")
            if isinstance(name, dict):
                name = name.get("name")
            if not isinstance(name, str) or name not in self._by_name:
                continue
            args = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                continue
            if self._args_match_schema(self._by_name[name], args):
                return [{"id": "inline_0", "type": "function", "function": {"name": name, "arguments": args}}]
            if best_mismatch is None:
                best_mismatch = {"name": name, "args": args}

        if best_mismatch is not None:
            tool = self._by_name[best_mismatch["name"]]
            schema = tool.parameters or {}
            return [{
                "id": "inline_mismatch_0",
                "type": "function",
                "function": {
                    "name": _MISMATCH_MARKER,
                    "arguments": {
                        "attempted_tool": best_mismatch["name"],
                        "provided_keys": sorted(best_mismatch["args"]),
                        "required": schema.get("required") or [],
                        "allowed": sorted((schema.get("properties") or {}).keys()),
                    },
                },
            }]
        return []

    @staticmethod
    def _args_match_schema(tool: Tool, args: dict[str, Any]) -> bool:
        """Does `args` actually fit this tool's declared parameters?

        Every required field must be present, and every key in `args` must
        be a declared property — an unrecognized key (like `cmd` on a tool
        that has no such parameter) is exactly the signature of a model
        inventing plausible-looking arguments rather than using a real one.
        """
        schema = tool.parameters or {}
        required = schema.get("required") or []
        if not all(field in args for field in required):
            return False
        properties = schema.get("properties")
        if properties and not set(args).issubset(properties):
            return False
        return True

    async def _execute_calls(self, tool_calls: list[dict[str, Any]]) -> None:
        """Run one turn's worth of tool calls, appending results in request order.

        Pure read-only calls (read_file, web_search, get_weather, and the
        like) run concurrently via asyncio.gather, since a model asking for
        several independent lookups in one turn is common and there's no
        reason to make it wait on each in sequence.

        spawn_agent calls also run concurrently with each other and with the
        read-only bucket — this is the actual speedup for something like
        scaffolding a full-stack app with a "backend" subagent and a
        "frontend" subagent in the same turn. This is safe because: (1) each
        subagent gets its own Agent instance and conversation, so there's no
        shared mutable model state to race on; (2) permission prompts are
        serialized by a lock on the shared PermissionEngine (see
        permissions.py) rather than actually running concurrently, so two
        subagents needing approval at once queue cleanly instead of
        corrupting the terminal; and (3) Python's cooperative single-threaded
        asyncio means the shared ctx.jobs/read_files dicts never see a torn
        write, just possibly-interleaved updates — a minor correctness
        wrinkle, not a safety one.

        Everything else — direct writes, direct shell execution — stays
        strictly sequential: those can conflict with each other in ways a
        subagent boundary doesn't protect against (two direct edits to the
        same file racing), so it's safest to reason about them one at a time.
        """
        parallel_calls, sequential_calls = [], []
        for call in tool_calls:
            name = (call.get("function") or {}).get("name", "")
            tool = self._by_name.get(name)
            if tool is not None and (tool.read_only or name == "spawn_agent"):
                parallel_calls.append(call)
            else:
                sequential_calls.append(call)

        results: dict[str, dict[str, Any]] = {}
        if parallel_calls:
            outcomes = await asyncio.gather(*(self._execute_call(c) for c in parallel_calls))
            for call, message in zip(parallel_calls, outcomes):
                results[call.get("id") or ""] = message
        for call in sequential_calls:
            results[call.get("id") or ""] = await self._execute_call(call)

        # Append in the same order the model asked for them, regardless of
        # which bucket or how fast each one actually finished — some models
        # correlate tool_call_id to position as well as to the id itself.
        for call in tool_calls:
            self.messages.append(results[call.get("id") or ""])

    async def _execute_call(self, call: dict[str, Any]) -> dict[str, Any]:
        fn = call.get("function") or {}
        name = fn.get("name", "")
        args = fn.get("arguments") or {}

        if name == _MISMATCH_MARKER:
            # Not a real tool call — see _inline_tool_calls. Nothing runs;
            # this is a diagnostic bounced straight back so a model that was
            # genuinely trying to call something real can correct itself
            # within the same turn instead of the attempt silently vanishing.
            attempted = args.get("attempted_tool", "?")
            provided = args.get("provided_keys") or []
            required = args.get("required") or []
            allowed = args.get("allowed") or []
            self.ctx.ui.warn(
                f"(saw a near-miss call to {attempted} with the wrong argument names — "
                "bounced it back so the model can retry with the correct ones)"
            )
            return self._tool_message(
                call, attempted,
                f"Error: that looked like an attempt to call `{attempted}`, but the arguments "
                f"don't match its real parameters. You passed: {provided or '(none)'}. "
                f"Required: {required or '(none)'}. Allowed arguments: {allowed or '(none)'}. "
                f"Call {attempted} again using exactly those argument names — as a real tool "
                "call, not text.",
            )

        tool = self._by_name.get(name)

        if tool is None:
            known = ", ".join(sorted(self._by_name)) or "(none)"
            return self._tool_message(
                call, name,
                f"Error: no tool named {name!r}. Available tools: {known}.",
            )

        try:
            preview = tool.preview(args)
        except Exception:
            preview = name
        self.ctx.ui.tool_start(name, preview)

        # Permission gate.
        try:
            detail = tool.detail(args, self.ctx)
        except Exception:
            detail = ""
        key = tool.permission_key(args) or ""
        request = PermissionRequest(
            tool=name,
            action=tool.action,
            key=key,
            preview=preview,
            detail=detail,
            agent=self.ctx.agent_name,
        )
        verdict = await self.ctx.permissions.check(request)
        if verdict.decision is Decision.DENY:
            self.ctx.ui.tool_result(name, False, f"declined — {verdict.reason}")
            denial_key = (name, key)
            count = self._denial_counts[denial_key] = self._denial_counts.get(denial_key, 0) + 1
            if count >= MAX_IDENTICAL_DENIALS:
                # The exact same call has now been declined more than once.
                # A cooperative model would already have stopped on its own
                # per the system prompt's instruction not to retry — but a
                # smaller local model often doesn't reliably follow that, and
                # the result is the "keeps saying let me try that again, in a
                # loop" failure. This forces the stop at the code level
                # instead of relying on the model's judgment for it.
                return self._tool_message(
                    call, name,
                    f"Not run (declined {count} times now — this exact call, same reason): "
                    f"{verdict.reason} Stop retrying this call entirely. Tell the user, plainly "
                    "and in your reply text, that you're blocked waiting on their approval for "
                    "this specific action, and let them choose: approve it explicitly, switch to "
                    "/mode auto or /trust if they want writes pre-approved, or suggest a "
                    "different approach. Do not attempt this exact call again unless the user "
                    "explicitly tells you to.",
                )
            return self._tool_message(
                call, name,
                f"Not run: {verdict.reason} Do not retry this call unchanged; tell the user what "
                "you needed it for, or take a different approach.",
            )
        # A successful (non-deny) outcome clears any prior denial streak for
        # this exact call, so an earlier decline doesn't linger and trip the
        # breaker on an unrelated later attempt with the same key.
        self._denial_counts.pop((name, key), None)

        # Execution.
        self.ctx.tool_calls_made += 1
        try:
            result = await tool.run(args, self.ctx)
        except ToolError as exc:
            self.ctx.ui.tool_result(name, False, str(exc))
            return self._error_message(call, name, key, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a tool bug shouldn't kill the session
            message = f"{type(exc).__name__}: {exc}"
            self.ctx.ui.tool_result(name, False, message)
            return self._error_message(call, name, key, message)

        # A clean run clears any error streak for this exact call, same
        # reasoning as clearing the denial streak above.
        for error_key in [k for k in self._error_counts if k[0] == name and k[1] == key]:
            self._error_counts.pop(error_key, None)
        self._render_result(tool, result)
        content = truncate(result.content, self.ctx.config.max_tool_output_chars, "tool output")
        return self._tool_message(call, name, content)

    def _error_message(self, call: dict[str, Any], name: str, key: str, error_text: str) -> dict[str, Any]:
        """Build the tool-error message, escalating if this exact (tool, key,
        error) combination has now failed repeatedly in a row — see
        _error_counts. A model that keeps retrying an identical failing call
        unchanged (most commonly: a write rejected for being outside the
        confined workspace) needs a harder stop than one more copy of the
        same error text, which it has already shown it doesn't act on."""
        error_key = (name, key, error_text)
        count = self._error_counts[error_key] = self._error_counts.get(error_key, 0) + 1
        if count >= MAX_IDENTICAL_DENIALS:
            return self._tool_message(
                call, name,
                f"Error (this exact call has now failed the same way {count} times in a row): "
                f"{error_text}\nStop retrying this unchanged. Tell the user plainly, in your "
                "reply text, what you were trying to do and the exact error, and ask how they "
                "want to proceed — do not attempt this exact call again unless they tell you "
                "something that would actually change the outcome (a different path, "
                "/workspace unlock, etc.).",
            )
        return self._tool_message(call, name, f"Error: {error_text}")

    def _render_result(self, tool: Tool, result: ToolResult) -> None:
        summary = result.display or ("done" if result.ok else "failed")
        self.ctx.ui.tool_result(tool.name, result.ok, summary)
        if result.meta.get("diff"):
            self.ctx.ui.diff(result.meta["diff"])
        elif self.ctx.config.show_tool_output and tool.name in ("run_command", "check_job"):
            body = result.content.split("\n", 2)[-1] if result.content else ""
            self.ctx.ui.tool_output(body)

    @staticmethod
    def _tool_message(call: dict[str, Any], name: str, content: str) -> dict[str, Any]:
        return {
            "role": "tool",
            "content": content,
            "name": name,
            "tool_call_id": call.get("id") or "call_0",
        }
