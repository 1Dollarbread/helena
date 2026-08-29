"""Tool registry.

`build_tools(ctx)` returns the full set for the main agent. Subagents get a
filtered view of the same list, so there is exactly one definition of what any
tool does and what permission it needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agent import SpawnAgentTool
from .base import Tool, ToolContext, ToolError, ToolResult
from .board import BoardCommandTool, BoardStateTool, BoardStageMediaTool
from .desktop import CloseAppTool, OpenAppTool
from .extras import RememberTool, RemindTool, StockTool, TimeTool, WeatherTool
from .files import (
    CreateProjectTool,
    DeletePathTool,
    EditFileTool,
    FindFilesTool,
    ListDirTool,
    ReadFileTool,
    SearchTextTool,
    TodoWriteTool,
    WriteFileTool,
)
from .print3d import GenerateModelTool, SendToPrinterTool, SliceModelTool
from .shell import CheckJobTool, DevServerTool, RunCommandTool
from .vision import AnalyzeImageTool
from .web import FetchUrlTool, WebSearchTool

if TYPE_CHECKING:  # pragma: no cover
    pass

# Ordered roughly by how often they get used, which is also the order the model
# sees them in — cheap nudge toward reading before writing. The board tools sit
# with analyze_image/todo_write since they're all "communicate back to the
# user" tools rather than filesystem/execution ones — they simply no-op with a
# clear "not set up, run /barehands-setup" error if barehands isn't configured,
# same pattern as voice.check_available(), so it's safe to always register them.
# The print3d tools follow the same pattern: always registered, each one
# raising a clear "not configured, see the README" error until its own prereqs
# (openscad / slicer path+profiles / bambu_ip+access_code+serial) are set.
TOOL_CLASSES: list[type[Tool]] = [
    ReadFileTool,
    ListDirTool,
    FindFilesTool,
    SearchTextTool,
    EditFileTool,
    WriteFileTool,
    CreateProjectTool,
    DeletePathTool,
    RunCommandTool,
    DevServerTool,
    CheckJobTool,
    WebSearchTool,
    FetchUrlTool,
    AnalyzeImageTool,
    BoardStateTool,
    BoardCommandTool,
    BoardStageMediaTool,
    TodoWriteTool,
    SpawnAgentTool,
    OpenAppTool,
    CloseAppTool,
    WeatherTool,
    StockTool,
    RemindTool,
    RememberTool,
    TimeTool,
    GenerateModelTool,
    SliceModelTool,
    SendToPrinterTool,
]


def build_tools(ctx: "ToolContext | None" = None) -> list[Tool]:
    """Instantiate every tool. `ctx` is accepted for symmetry and future gating."""
    return [cls() for cls in TOOL_CLASSES]


def tool_names() -> list[str]:
    return [cls.name for cls in TOOL_CLASSES]


__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "build_tools",
    "tool_names",
    "TOOL_CLASSES",
]
