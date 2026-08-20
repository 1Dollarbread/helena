"""`helena` entry point.

Interactive by default; `-p/--print` runs a single turn and exits, which is the
mode to use from scripts and other tools.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__
from .config import VALID_MODES, Config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helena",
        description="H.E.L.E.N.A — a local-first terminal agent running on your own models.",
        epilog="Examples:\n"
               "  helena                              start the interactive harness\n"
               "  helena 'what does agent.py do?'     start with a first message\n"
               "  helena -p 'run the tests' --mode auto   one-shot, no prompts\n"
               "  helena -C ~/code/api --mode plan    read-only session in another directory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("prompt", nargs="*", help="Optional first message.")
    parser.add_argument("-p", "--print", action="store_true", dest="one_shot",
                        help="Run one turn, print the reply, exit.")
    parser.add_argument("-C", "--workspace", default=None,
                        help="Directory to work in (default: the current one).")
    parser.add_argument("--model", default=None, help="Chat model, e.g. qwen2.5:7b-instruct.")
    parser.add_argument("--vision-model", default=None, help="Model used for images, e.g. llava.")
    parser.add_argument("--server", default=None, help="HELENA server URL.")
    parser.add_argument("--mode", choices=VALID_MODES, default=None,
                        help="Permission mode (default: ask).")
    parser.add_argument("--allow", action="append", default=[], metavar="RULE",
                        help="Pre-approve a rule, e.g. --allow 'run_command(pytest:*)'. Repeatable.")
    parser.add_argument("--deny", action="append", default=[], metavar="RULE",
                        help="Block a rule outright. Repeatable.")
    parser.add_argument("--allow-outside-workspace", action="store_true",
                        help="Let file tools reach outside the workspace directory.")
    parser.add_argument("--no-stream", action="store_true", help="Wait for whole replies instead of streaming.")
    parser.add_argument("--no-server-autostart", action="store_true",
                        help="Fail instead of starting a local model server.")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="Cap on tool-calling steps per turn.")
    parser.add_argument("--version", action="version", version=f"helena {__version__}")
    return parser


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    if args.model:
        config.model = args.model
    if args.vision_model:
        config.vision_model = args.vision_model
    if args.server:
        config.server_url = args.server
    if args.mode:
        config.mode = args.mode
    if args.allow:
        config.allow.extend(r for r in args.allow if r not in config.allow)
    if args.deny:
        config.deny.extend(r for r in args.deny if r not in config.deny)
    if args.allow_outside_workspace:
        config.allow_outside_workspace = True
    if args.no_stream:
        config.stream = False
    if args.no_server_autostart:
        config.auto_start_server = False
    if args.max_iterations:
        config.max_iterations = args.max_iterations
    return config


async def run_one_shot(config: Config, prompt: str) -> int:
    """Single turn, no prompt session — anything needing approval is refused."""
    from .agent import Agent
    from .client import ServerClient, ServerError, ensure_server
    from .permissions import PermissionEngine
    from .tools import build_tools
    from .tools.base import ToolContext
    from .ui import UI

    ui = UI(theme=config.theme)
    ok, note = await ensure_server(config.server_url, config.api_token, config.auto_start_server)
    if not ok:
        ui.error(f"No HELENA server: {note}. Start one with `helena-server`.")
        return 1

    client = ServerClient(config.server_url, config.api_token)
    ctx = ToolContext(
        workspace=config.workspace,
        config=config,
        client=client,
        permissions=PermissionEngine(config, ask=None),
        ui=ui,
    )
    ctx.permissions.config = config
    agent = Agent(ctx=ctx, tools=build_tools(ctx), label=config.name)
    try:
        result = await agent.send(prompt)
    except ServerError as exc:
        ui.error(str(exc))
        return 1
    finally:
        await client.aclose()

    if result.stopped == "error":
        return 1
    ui.usage({
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_seconds": result.seconds,
        "tool_calls": result.tool_calls,
    })
    return 0


async def run_interactive(config: Config, first: str | None) -> int:
    from .repl import Repl

    return await Repl(config).start(first_message=first)


def main() -> None:
    args = build_parser().parse_args()
    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    if not workspace.is_dir():
        print(f"helena: not a directory: {workspace}", file=sys.stderr)
        raise SystemExit(2)

    config = apply_overrides(Config.load(workspace), args)
    prompt = " ".join(args.prompt).strip()

    if args.one_shot:
        if not prompt and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            print("helena: -p needs a prompt (as an argument or on stdin)", file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(asyncio.run(run_one_shot(config, prompt)))

    try:
        raise SystemExit(asyncio.run(run_interactive(config, prompt or None)))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
