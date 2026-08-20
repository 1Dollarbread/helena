"""Terminal rendering: streamed replies, tool cards, diffs, permission prompts.

Everything the user sees goes through this class, which keeps the agent loop
free of formatting concerns and makes the harness testable with a silent UI
(`UI(quiet=True)`).
"""

from __future__ import annotations

import sys
from typing import Any, Iterable

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .permissions import PermissionRequest, PermissionResult

TOOL_ICONS = {
    "read_file": "◆", "write_file": "✎", "edit_file": "✎", "delete_path": "✖",
    "list_dir": "▤", "find_files": "⌕", "search_text": "⌕", "run_command": "❯",
    "check_job": "◷", "web_search": "🌐", "fetch_url": "🌐", "analyze_image": "▣",
    "todo_write": "☑", "spawn_agent": "⛬", "get_weather": "☁", "get_stock": "$",
    "add_reminder": "⏰", "remember": "✦", "get_time": "◷",
}

STATUS_MARK = {"completed": "[green]✔[/green]", "in_progress": "[yellow]▸[/yellow]", "pending": "[dim]○[/dim]"}


class UI:
    def __init__(self, quiet: bool = False, theme: str = "cyan", no_color: bool = False) -> None:
        self.console = Console(
            highlight=False,
            soft_wrap=True,
            no_color=no_color,
            force_terminal=None if sys.stdout.isatty() else False,
        )
        self.quiet = quiet
        self.theme = theme
        self.prompt_session: Any = None   # set by the REPL; needed for asking
        self._indent = ""
        self._streaming = False

    # --- primitives --------------------------------------------------------

    def print(self, *args: Any, **kwargs: Any) -> None:
        if self.quiet:
            return
        self.console.print(*args, **kwargs)

    def raw(self, text: str) -> None:
        if self.quiet:
            return
        self.console.print(text, end="", markup=False, highlight=False)

    def notice(self, message: str) -> None:
        self.print(f"[dim]{self._indent}{message}[/dim]")

    def warn(self, message: str) -> None:
        self.print(f"[yellow]{self._indent}{message}[/yellow]")

    def error(self, message: str) -> None:
        self.print(f"[red]{self._indent}{message}[/red]")

    def rule(self, label: str = "") -> None:
        if not self.quiet:
            self.console.print(Rule(label, style="dim"))

    def indent(self, prefix: str) -> None:
        self._indent = prefix

    @property
    def indent_prefix(self) -> str:
        """Current nesting prefix — subagent output is rendered inside it."""
        return self._indent

    # --- conversation ------------------------------------------------------

    def banner(self, model: str, workspace: str, mode: str, server: str) -> None:
        if self.quiet:
            return
        title = Text("H.E.L.E.N.A", style=f"bold {self.theme}")
        subtitle = Text("Highly Efficient Logic Engine Network Assistant", style="dim")
        body = Table.grid(padding=(0, 2))
        body.add_column(style="dim", justify="right")
        body.add_column()
        body.add_row("model", model or "(server default)")
        body.add_row("workspace", workspace)
        body.add_row("permissions", mode)
        body.add_row("server", server)
        self.console.print(Panel(body, title=title, subtitle=subtitle, border_style=self.theme))
        self.console.print("[dim]/help for commands · /mode to change permissions · Ctrl-C interrupts, Ctrl-D exits[/dim]\n")

    def assistant_prefix(self, label: str = "HELENA") -> None:
        if self.quiet:
            return
        self.console.print(f"[bold {self.theme}]{self._indent}{label}[/bold {self.theme}] ", end="")
        self._streaming = True

    def stream_token(self, text: str) -> None:
        if self.quiet:
            return
        if self._indent and "\n" in text:
            text = text.replace("\n", "\n" + self._indent)
        self.console.print(text, end="", markup=False, highlight=False)

    def end_stream(self) -> None:
        if self.quiet or not self._streaming:
            return
        self.console.print()
        self._streaming = False

    def render_markdown(self, text: str) -> None:
        if self.quiet or not text.strip():
            return
        self.console.print(Markdown(text))

    # --- tools -------------------------------------------------------------

    def tool_start(self, tool: str, preview: str) -> None:
        icon = TOOL_ICONS.get(tool, "•")
        self.print(f"[dim]{self._indent}{icon} {preview}[/dim]")

    def tool_result(self, tool: str, ok: bool, summary: str, detail: str = "") -> None:
        if self.quiet or not summary:
            return
        color = "dim" if ok else "yellow"
        self.console.print(f"[{color}]{self._indent}  ↳ {summary}[/{color}]")
        if detail:
            # Tool output is arbitrary text — square brackets in it must never
            # be read as rich markup, so it goes through Text, not a tag string.
            for line in detail.splitlines()[:20]:
                self.console.print(Text(f"{self._indent}    {line}", style="dim"))

    def tool_output(self, text: str, limit: int = 12) -> None:
        """Show a clipped preview of raw tool output."""
        if self.quiet or not text.strip():
            return
        lines = text.splitlines()
        for line in lines[:limit]:
            self.console.print(Text(f"{self._indent}  │ {line[:200]}", style="dim"))
        if len(lines) > limit:
            self.console.print(Text(f"{self._indent}  │ … {len(lines) - limit} more lines", style="dim"))

    def diff(self, patch: str, max_lines: int = 40) -> None:
        if self.quiet or not patch.strip():
            return
        lines = patch.splitlines()
        shown = lines[:max_lines]
        for line in shown:
            if line.startswith("+++") or line.startswith("---"):
                style = "dim"
            elif line.startswith("+"):
                style = "green"
            elif line.startswith("-"):
                style = "red"
            elif line.startswith("@@"):
                style = "cyan"
            else:
                style = "dim"
            self.console.print(Text(f"{self._indent}  {line}", style=style))
        if len(lines) > max_lines:
            self.console.print(f"[dim]{self._indent}  … {len(lines) - max_lines} more diff lines[/dim]")

    def render_todos(self, todos: Iterable[dict[str, Any]]) -> None:
        todos = list(todos)
        if self.quiet or not todos:
            return
        self.console.print()
        for item in todos:
            mark = STATUS_MARK.get(item.get("status", "pending"), "○")
            style = "dim" if item.get("status") == "completed" else ""
            self.console.print(f"{self._indent}  {mark} [{style}]{item['task']}[/{style}]" if style
                               else f"{self._indent}  {mark} {item['task']}")
        self.console.print()

    def code(self, text: str, language: str = "python") -> None:
        if self.quiet:
            return
        self.console.print(Syntax(text, language, theme="ansi_dark", word_wrap=True))

    def table(self, title: str, columns: list[str], rows: list[list[str]]) -> None:
        if self.quiet:
            return
        table = Table(title=title, title_style=f"bold {self.theme}", box=None, pad_edge=False)
        for col in columns:
            table.add_column(col, overflow="fold")
        for row in rows:
            table.add_row(*row)
        self.console.print(table)

    def usage(self, stats: dict[str, Any]) -> None:
        if self.quiet:
            return
        bits = []
        if stats.get("prompt_tokens"):
            bits.append(f"{stats['prompt_tokens']:,} in")
        if stats.get("completion_tokens"):
            bits.append(f"{stats['completion_tokens']:,} out")
        if stats.get("tokens_per_second"):
            bits.append(f"{stats['tokens_per_second']:.0f} tok/s")
        if stats.get("total_seconds"):
            bits.append(f"{stats['total_seconds']:.1f}s")
        if stats.get("tool_calls"):
            bits.append(f"{stats['tool_calls']} tool call(s)")
        if bits:
            self.console.print(f"[dim]{self._indent}{' · '.join(bits)}[/dim]\n")

    # --- permission prompt -------------------------------------------------

    async def ask_permission(self, req: PermissionRequest, result: PermissionResult) -> str:
        """Blocking prompt. Returns once | always | session | no."""
        if self.quiet or self.prompt_session is None:
            return "no"

        icon = TOOL_ICONS.get(req.tool, "•")
        header = Text()
        header.append(f"{icon} {req.tool}", style="bold yellow")
        if req.agent != "helena":
            header.append(f"  (subagent: {req.agent})", style="dim")

        self.console.print()
        self.console.print(
            Panel(Text(req.preview, style="bold"), title=header, border_style="yellow", expand=False)
        )
        if req.detail:
            if req.tool in ("write_file", "edit_file") and req.detail.startswith(("---", "+++", "@@")):
                self.diff(req.detail, max_lines=30)
            else:
                for line in req.detail.splitlines()[:20]:
                    self.console.print(Text(f"  {line}", style="dim"))
        if result.danger:
            self.console.print(f"[bold red]  ⚠ This {result.danger}.[/bold red]")

        # Text(), not a markup string: "[y]" and friends would be parsed as
        # style tags and swallowed.
        self.console.print(
            Text(
                "  [y] yes, once    [a] yes, always allow this    "
                "[s] yes, all session    [n] no (default)",
                style="dim",
            )
        )
        try:
            answer = (await self.prompt_session.prompt_async("  allow? ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "no"
        return {
            "y": "once", "yes": "once", "": "no",
            "a": "always", "always": "always",
            "s": "session", "session": "session",
            "n": "no", "no": "no",
        }.get(answer, "no")


class SilentUI(UI):
    """UI that renders nothing — used by tests and non-interactive runs."""

    def __init__(self) -> None:
        super().__init__(quiet=True)
