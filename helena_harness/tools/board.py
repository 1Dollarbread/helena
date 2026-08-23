"""Tools for the barehands hand-tracked board (github.com/jaredrhod/barehands) —
HELENA's hands and eyes on a separate, physical control surface: a camera
watches the user's real hands, and cards, images, and 3D models float over it
as glass they can pinch, drag, and throw. See barehands_client.py for the
protocol and setup helpers, and README.md's "Bare hands" section for how the
whole rig fits together (camera -> board -> optionally AirPlay to a
projector).

Every call here reaches nothing but 127.0.0.1 — the barehands server only
ever binds to localhost, same trust boundary as the Ollama server HELENA
already talks to.
"""

from __future__ import annotations

import shutil
from typing import Any

from .. import barehands_client as bh
from ..permissions import Action
from .base import Tool, ToolContext, ToolError, ToolResult, rel, resolve_path

_ACTION_ENUM = list(bh.ALLOWED_ACTIONS)


class BoardCommandTool(Tool):
    name = "board_command"
    description = """
    Put something on the barehands board — the hand-tracked glass surface the
    user controls with bare hands in front of a camera. This is show-and-tell:
    when the user asks to SEE something ("show me", "put it up", "pull up X"),
    don't just answer in text — stage it here and say what you put up.

    Actions: "present" lands something center stage, enlarged and spotlit,
    everything else dimmed — the show-me verb, for when the user asks to be
    shown something specific. "add_card" / "add_img" / "hand" add an ensemble
    piece without the spotlight. "explode" / "assemble" part or reassemble a
    3D model's exploded view. "yank" pulls something across the board into the
    user's hand; "hover" nudges it partway without fully delivering it.
    "clear" sweeps the board; "reset" re-centers the ring.

    `src` (for add_img / hand / present) must point at a file already inside
    the barehands media airlock — call board_stage_media first if the file
    isn't there yet; this tool cannot reach outside that folder, the server
    enforces it regardless of what's passed here. `file` (for a notes-orb
    item) is an "<orb-index>/<relative-path>" reference, the same form the
    barehands board's own note listings use.
    """
    action = Action.WRITE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "enum": _ACTION_ENUM, "description": "The board action to perform."},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "src": {"type": "string", "description": "Path inside the media airlock, e.g. 'models/car.glb' or 'misc/logo.png'."},
            "file": {"type": "string", "description": "A '<orb>/<relpath>' note reference."},
            "open": {"type": "boolean", "description": "With a present of a `file`: open/spotlight that note."},
        },
        "required": ["a"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return str(args.get("a", ""))

    def preview(self, args: dict[str, Any]) -> str:
        bits = [f"board: {args.get('a', '?')}"]
        if args.get("title"):
            bits.append(f'"{args["title"]}"')
        if args.get("src"):
            bits.append(args["src"])
        return " ".join(bits)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = {k: v for k, v in args.items() if v is not None}
        try:
            status, body = await bh.post_command(ctx.config, command)
        except bh.BarehandsError as exc:
            raise ToolError(str(exc)) from exc
        if status == 204:
            label = f' "{args["title"]}"' if args.get("title") else ""
            return ToolResult(
                ok=True,
                content=f"Board took it: {args.get('a')}{label}",
                display=f"board: {args.get('a')}",
            )
        raise ToolError(
            f"Board rejected this (HTTP {status}). Usually means `src` or `file` isn't inside "
            "the media airlock or notes jail — try board_state or board_stage_media first."
            + (f" Response: {body[:200]}" if body else "")
        )


class BoardStateTool(Tool):
    name = "board_state"
    description = """
    Read what's actually on the barehands board right now — HELENA's eyes on
    the board. The user moves things by hand, so never assume what's up there
    from memory; call this before commenting on or referring to board contents.
    """
    action = Action.READ
    read_only = True
    parameters = {"type": "object", "properties": {}}

    def preview(self, args: dict[str, Any]) -> str:
        return "Look at the board"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            state = await bh.get_state(ctx.config)
        except bh.BarehandsError as exc:
            raise ToolError(str(exc)) from exc
        text = bh.describe_state(state)
        n = len((state or {}).get("items") or [])
        return ToolResult(ok=True, content=text, display=f"{n} item(s) on board")


class BoardStageMediaTool(Tool):
    name = "board_stage_media"
    description = """
    Copy a file from the workspace into the barehands media airlock so it can
    then be staged on the board with board_command. This is the necessary
    first step for "put this image/model on the board" whenever the file
    isn't already inside barehands's media/ folder — the board can only ever
    display files that physically live there, a safety jail enforced by the
    barehands server itself, not by HELENA. Accepts images
    (.png/.jpg/.jpeg/.webp/.gif/.webm) and 3D models (.glb/.gltf). Pass
    `present: true` to copy and spotlight it on the board in one step.
    """
    action = Action.WRITE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to copy, from the workspace."},
            "subfolder": {
                "type": "string",
                "description": "Airlock subfolder: 'misc' (default, images/video), 'fx' (transparent "
                               "props), 'models' (3D, solid), or 'holo' (3D, rendered as a blue "
                               "hologram wireframe).",
            },
            "title": {"type": "string", "description": "Optional title if presenting immediately."},
            "present": {"type": "boolean", "description": "Also spotlight it on the board right after copying."},
        },
        "required": ["path"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    def preview(self, args: dict[str, Any]) -> str:
        return f"Stage {args.get('path', '?')} to the board's media folder"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        source = resolve_path(ctx, args.get("path", ""), must_exist=True)
        if source.is_dir():
            raise ToolError(f"{rel(ctx, source)} is a directory — pass a single file.")
        if source.suffix.lower() not in bh.MEDIA_EXTENSIONS:
            raise ToolError(
                f"{source.suffix or '(no extension)'} isn't a type the board stages. "
                f"Allowed: {', '.join(sorted(bh.MEDIA_EXTENSIONS))}."
            )
        try:
            root = bh.media_root(ctx.config)
        except bh.BarehandsError as exc:
            raise ToolError(str(exc)) from exc

        subfolder = (args.get("subfolder") or "misc").strip().strip("/")
        if subfolder not in ("misc", "fx", "models", "holo"):
            raise ToolError("subfolder must be one of: misc, fx, models, holo.")
        dest_dir = root / subfolder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / source.name
        try:
            shutil.copy2(source, dest)
        except OSError as exc:
            raise ToolError(f"Could not copy into the media airlock: {exc}") from exc

        airlock_rel = f"{subfolder}/{source.name}"
        note = f"Staged {rel(ctx, source)} into the media airlock as {airlock_rel}."

        if args.get("present"):
            command: dict[str, Any] = {"a": "present", "src": airlock_rel}
            if args.get("title"):
                command["title"] = args["title"]
            try:
                status, _ = await bh.post_command(ctx.config, command)
            except bh.BarehandsError as exc:
                raise ToolError(f"{note} But presenting it failed: {exc}") from exc
            if status != 204:
                raise ToolError(f"{note} But the board rejected the present (HTTP {status}).")
            note += " Presented on the board."

        return ToolResult(ok=True, content=note, display=f"staged {airlock_rel}")
