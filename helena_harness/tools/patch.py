"""Batch, atomic multi-hunk edits to a single file.

`edit_file` (files.py) is exact-string replace, one hunk per call — the right
tool for a single targeted change. Plenty of real edits are several small
changes to the same file at once (rename a symbol in three places, add an
import and use it below), and doing those as N separate edit_file calls has a
real failure mode on a local model: hunk 2 can shift the text hunk 3 was
matched against, or the model can lose track partway through and leave a file
half-edited with no way to tell from the outside that it happened.

`multi_edit` fixes that by treating the whole batch as one transaction: every
hunk is applied, in order, against an in-memory copy of the file, and the
write to disk only happens if every one of them succeeds. Any single
old_string that doesn't match — including one that would already have been
consumed by an earlier hunk in the same call — fails the entire call with no
bytes written, rather than leaving some hunks applied and others not.
"""

from __future__ import annotations

from typing import Any

from ..permissions import Action
from .base import Tool, ToolContext, ToolError, ToolResult, rel, resolve_path
from .files import diff_stat, unified_diff


class MultiEditTool(Tool):
    name = "multi_edit"
    description = """
    Apply several exact-string edits to ONE file as a single atomic operation.
    Each edit is {old_string, new_string, replace_all?} — same rules as
    edit_file — applied in the order given against the file's actual,
    evolving content, so a later hunk can match text an earlier hunk just
    introduced.

    Use this instead of several separate edit_file calls on the same file
    when you already know the whole set of changes it needs — it either
    applies all of them or none of them, so the file can never end up
    half-edited. The file must have been read first, exactly like edit_file.
    """
    action = Action.WRITE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "edits": {
                "type": "array",
                "description": "Applied in order. At least one required.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    def preview(self, args: dict[str, Any]) -> str:
        edits = args.get("edits") or []
        return f"Multi-edit {args.get('path', '?')} ({len(edits)} hunk(s))"

    @staticmethod
    def _apply_all(text: str, edits: list[dict[str, Any]], name: str) -> str:
        for i, edit in enumerate(edits, start=1):
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            if old == new:
                raise ToolError(f"Hunk {i}: old_string and new_string are identical — nothing to do.")
            count = text.count(old)
            if count == 0:
                raise ToolError(
                    f"Hunk {i}: old_string was not found in {name} at this point in the batch "
                    "(an earlier hunk may have already changed this text). Re-read the file and "
                    "re-derive the remaining hunks — nothing from this call has been written."
                )
            if count > 1 and not edit.get("replace_all"):
                raise ToolError(
                    f"Hunk {i}: old_string appears {count} times in {name}. Add surrounding lines "
                    "to make it unique, or set replace_all: true for this hunk."
                )
            text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
        return text

    def detail(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            path = resolve_path(ctx, args.get("path", ""), must_exist=True)
            before = path.read_text("utf-8", errors="replace")
            after = self._apply_all(before, args.get("edits") or [], rel(ctx, path))
        except (ToolError, OSError):
            return ""
        return unified_diff(before, after, rel(ctx, path))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        edits = args.get("edits") or []
        if not edits:
            raise ToolError("`edits` must be a non-empty list.")
        path = resolve_path(ctx, args.get("path", ""), must_exist=True)
        if path.is_dir():
            raise ToolError(f"{rel(ctx, path)} is a directory.")
        key = str(path)
        if key not in ctx.read_files:
            raise ToolError(
                f"Read {rel(ctx, path)} before editing it — that's how you get the exact text to match."
            )
        current_mtime = path.stat().st_mtime
        if current_mtime > ctx.read_files[key] + 0.001:
            ctx.read_files.pop(key, None)
            raise ToolError(
                f"{rel(ctx, path)} changed on disk since you read it. Read it again, then re-apply the edits."
            )

        before = path.read_text("utf-8", errors="replace")
        # Raises ToolError on any hunk failure — nothing is written in that case.
        after = self._apply_all(before, edits, rel(ctx, path))
        path.write_text(after, encoding="utf-8")
        ctx.read_files[key] = path.stat().st_mtime

        added, removed = diff_stat(before, after)
        diff = unified_diff(before, after, rel(ctx, path))
        return ToolResult(
            ok=True,
            content=f"Applied {len(edits)} hunk(s) to {rel(ctx, path)}, +{added}/-{removed}.",
            display=f"multi-edited {rel(ctx, path)}  +{added}/-{removed}",
            meta={"diff": diff, "path": str(path)},
        )
