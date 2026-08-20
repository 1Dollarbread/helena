"""The spawn_agent tool — delegation to a subagent."""

from __future__ import annotations

import asyncio
from typing import Any

from ..permissions import Action
from ..subagents import AGENT_SPECS
from .base import Tool, ToolContext, ToolError, ToolResult


class SpawnAgentTool(Tool):
    name = "spawn_agent"
    description = ""      # filled in at construction from the agent registry
    action = Action.NONE
    read_only = False     # a coder subagent writes; permissions still apply per call
    parameters = {
        "type": "object",
        "properties": {
            "agent_type": {"type": "string", "enum": list(AGENT_SPECS)},
            "task": {
                "type": "string",
                "description": "Complete, self-contained instructions. The subagent cannot ask "
                               "you anything, and does not see this conversation.",
            },
            "context": {
                "type": "string",
                "description": "Optional background: file paths, constraints, what you already tried.",
            },
        },
        "required": ["agent_type", "task"],
    }

    def __init__(self) -> None:
        catalogue = "\n".join(f"  - {s.name}: {s.description}" for s in AGENT_SPECS.values())
        self.description = f"""
    Delegate a self-contained task to a subagent that works in its own context
    and reports back a summary. Use it when a task would otherwise flood this
    conversation with tool output — broad code searches, web research, a scoped
    implementation — which matters a lot with a small local context window.

    Available agents:
{catalogue}

    Give complete instructions: the subagent cannot ask questions and sees none
    of this conversation. Anything it does still goes through the same
    permission checks, so the user is asked before it writes or runs anything.

    Call this multiple times in the same turn when the work splits into
    genuinely independent parts — a backend and a frontend, several unrelated
    modules. Independent calls in one turn run concurrently, not one after
    another, so this is the fast way to build something like a full-stack app.
    Only split work this way when the parts don't depend on each other
    finishing first; a real dependency still needs to happen in order across
    separate turns.
    """

    def preview(self, args: dict[str, Any]) -> str:
        task = (args.get("task") or "").strip().replace("\n", " ")
        return f"Delegate to {args.get('agent_type', '?')}: {task[:80]}{'…' if len(task) > 80 else ''}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # Imported here: agent.py imports the tool registry, which imports this
        # module. Deferring the import keeps that cycle from biting at startup.
        from ..agent import Agent
        from . import build_tools

        agent_type = args.get("agent_type") or ""
        spec = AGENT_SPECS.get(agent_type)
        if not spec:
            raise ToolError(
                f"Unknown agent type {agent_type!r}. Available: {', '.join(AGENT_SPECS)}."
            )
        task = (args.get("task") or "").strip()
        if not task:
            raise ToolError("`task` is required — say exactly what the subagent should do.")

        if ctx.depth >= ctx.config.subagent_max_depth:
            raise ToolError(
                f"Subagent nesting limit reached (depth {ctx.depth}). Do this work yourself."
            )

        child_ctx = ctx.child(agent_name=spec.name)
        tools = [t for t in build_tools(child_ctx) if t.name in spec.tools]
        prompt = spec.system_prompt.format(parent=ctx.config.name).strip()
        prompt += f"\n\n## Environment\n\nWorking directory: {ctx.workspace}\nPermission mode: {ctx.config.mode}"
        prompt += (
            f"\n\n## Your real tools\n\nYour only real tools, by name, are: {', '.join(t.name for t in tools) or '(none)'}. "
            "No other tool name is real, and no other agent's tools are available to you. If you are not making a real "
            "tool call this turn, do not write anything that looks like one (no fake status lines, no invented tool names)."
        )

        sub = Agent(
            ctx=child_ctx,
            tools=tools,
            label=spec.name,
            model=spec.model or ctx.config.subagent_model or ctx.config.model or None,
            max_iterations=spec.max_iterations,
            system_prompt=prompt,
            nested=True,
        )

        message = task if not args.get("context") else f"{task}\n\n## Context\n\n{args['context']}"

        ctx.ui.print(f"[dim]  ┌─ {spec.name} subagent starting[/dim]")
        previous_indent = ctx.ui.indent_prefix
        ctx.ui.indent(previous_indent + "  │ ")
        try:
            result = await sub.send(message)
        except asyncio.CancelledError:
            ctx.ui.indent(previous_indent)
            ctx.ui.print(f"[dim]  └─ {spec.name} interrupted[/dim]")
            raise
        finally:
            ctx.ui.indent(previous_indent)

        ctx.ui.print(
            f"[dim]  └─ {spec.name} finished — {result.tool_calls} tool call(s), "
            f"{result.completion_tokens:,} tokens, {result.seconds:.0f}s[/dim]"
        )

        if result.stopped == "error":
            raise ToolError(f"The {spec.name} subagent failed: {result.error}")
        report = result.text.strip()
        if not report:
            return ToolResult(
                ok=False,
                content=(
                    f"The {spec.name} subagent finished without a written report "
                    f"({result.stopped}). Try a narrower task, or do it yourself."
                ),
                display="no report",
            )
        return ToolResult(
            ok=True,
            content=f"Report from the {spec.name} subagent:\n\n{report}",
            display=f"{spec.name} reported back ({result.tool_calls} tool calls)",
            meta={"agent": spec.name, "iterations": result.iterations},
        )
