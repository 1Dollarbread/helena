"""File tools: read, write, edit, delete, list, glob, grep."""

from __future__ import annotations

import difflib
import fnmatch
import re
from pathlib import Path
from typing import Any

from ..permissions import Action
from .base import (
    SKIP_DIRS,
    IMAGE_SUFFIXES,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    iter_files,
    looks_binary,
    rel,
    resolve_path,
    truncate,
)

MAX_READ_LINES = 2000
MAX_LINE_CHARS = 2000


def unified_diff(before: str, after: str, path: str, context: int = 3) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    return "".join(diff)


def diff_stat(before: str, after: str) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


class ReadFileTool(Tool):
    name = "read_file"
    description = """
    Read a text file from the workspace. Returns the contents with line numbers,
    which is how you should refer to them afterwards (path:line).
    Use offset/limit for large files. For images, use analyze_image instead.
    Always read a file before editing it.
    """
    action = Action.READ
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, absolute or relative to the workspace."},
            "offset": {"type": "integer", "description": "1-based line to start from. Default 1."},
            "limit": {"type": "integer", "description": f"How many lines to read. Default {MAX_READ_LINES}."},
        },
        "required": ["path"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    def preview(self, args: dict[str, Any]) -> str:
        loc = f" from line {args['offset']}" if args.get("offset") else ""
        return f"Read {args.get('path', '?')}{loc}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(ctx, args.get("path", ""), must_exist=True)
        if path.is_dir():
            raise ToolError(f"{rel(ctx, path)} is a directory — use list_dir.")
        if path.suffix.lower() in IMAGE_SUFFIXES:
            raise ToolError(
                f"{rel(ctx, path)} is an image. Use analyze_image to look at it."
            )
        if looks_binary(path):
            size = path.stat().st_size
            return ToolResult(
                ok=True,
                content=f"{rel(ctx, path)} is a binary file ({size:,} bytes). Not shown.",
                display=f"binary file, {size:,} bytes",
            )

        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"Could not read {rel(ctx, path)}: {exc}") from exc

        ctx.read_files[str(path)] = path.stat().st_mtime

        lines = text.splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = int(args.get("limit") or MAX_READ_LINES)
        chunk = lines[offset - 1: offset - 1 + limit]
        if not chunk and lines:
            raise ToolError(
                f"Offset {offset} is past the end of {rel(ctx, path)} ({len(lines)} lines)."
            )

        rendered = "\n".join(
            f"{i:>6}\t{line[:MAX_LINE_CHARS]}"
            + ("… [line truncated]" if len(line) > MAX_LINE_CHARS else "")
            for i, line in enumerate(chunk, start=offset)
        )
        shown_to = offset + len(chunk) - 1
        header = f"{rel(ctx, path)} (lines {offset}-{shown_to} of {len(lines)})"
        more = ""
        if shown_to < len(lines):
            more = f"\n\n[{len(lines) - shown_to} more lines — call again with offset={shown_to + 1}]"
        if not text.strip():
            return ToolResult(ok=True, content=f"{header}: file is empty.", display="empty file")
        return ToolResult(
            ok=True,
            content=f"{header}\n{rendered}{more}",
            display=f"{len(chunk)} lines",
            meta={"path": str(path), "lines": len(lines)},
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = """
    Create a file, or replace an existing file's entire contents.
    Prefer edit_file for changes to an existing file — this overwrites everything,
    so asking for "update"/"fix" something and getting this instead of edit_file
    is how a change can look identical to before (the rewrite reproduces the
    same content) or silently drops something the rewrite forgot to include.
    Parent directories are created as needed.
    """
    action = Action.WRITE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string", "description": "The complete new contents of the file."},
        },
        "required": ["path", "content"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    def preview(self, args: dict[str, Any]) -> str:
        content = args.get("content") or ""
        return f"Write {args.get('path', '?')} ({len(content.splitlines())} lines)"

    def detail(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            path = resolve_path(ctx, args.get("path", ""))
        except ToolError:
            return ""
        before = path.read_text("utf-8", errors="replace") if path.is_file() else ""
        return unified_diff(before, args.get("content") or "", rel(ctx, path))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(ctx, args.get("path", ""))
        content = args.get("content")
        if content is None:
            raise ToolError("`content` is required (pass an empty string for an empty file).")
        existed = path.is_file()
        before = path.read_text("utf-8", errors="replace") if existed else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Could not write {rel(ctx, path)}: {exc}") from exc
        ctx.read_files[str(path)] = path.stat().st_mtime
        added, removed = diff_stat(before, content)
        verb = "Updated" if existed else "Created"
        return ToolResult(
            ok=True,
            content=f"{verb} {rel(ctx, path)} ({len(content.splitlines())} lines, +{added}/-{removed}).",
            display=f"{verb.lower()} {rel(ctx, path)}  +{added}/-{removed}",
            meta={"diff": unified_diff(before, content, rel(ctx, path)), "path": str(path)},
        )


class CreateProjectTool(Tool):
    name = "create_project"
    description = """
    Create many files in a single call — the right tool for scaffolding a new
    project (a full-stack app, a package, a set of config files) instead of
    writing files one at a time, or worse, just showing the code in your reply
    instead of actually creating it. A reply containing a code block for a file
    the user asked you to build, with no matching write_file/create_project call
    behind it, has not created anything — the user would have to copy-paste it
    themselves, which defeats the entire point of having tools.

    NOT for updating a project that already exists — rewriting a file's full
    contents to change one part of it is how "update the server" turns into
    nothing actually changing (the model re-derives the same file from scratch
    and gets the same result), or silently drops something the rewrite forgot
    to include. For an existing file, read it and use edit_file for the
    specific part that needs to change instead.

    Give it a list of {path, content} pairs; every path is created (parent
    directories included) relative to the workspace, or `cwd` if given. After
    scaffolding, follow up with run_command (install dependencies) and
    run_dev_server (actually start it) — don't stop at just creating files if
    the user asked for something runnable.
    """
    action = Action.WRITE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "cwd": {"type": "string", "description": "Directory to create files in, relative to the workspace. Defaults to the workspace root."},
            "files": {
                "type": "array",
                "description": "One entry per file to create or overwrite.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative to `cwd` (or the workspace root)."},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        "required": ["files"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        files = args.get("files") or []
        cwd = args.get("cwd") or "."
        return f"{cwd} ({len(files)} files)"

    def preview(self, args: dict[str, Any]) -> str:
        files = args.get("files") or []
        names = ", ".join(f.get("path", "?") for f in files[:5])
        more = f" +{len(files) - 5} more" if len(files) > 5 else ""
        return f"Create {len(files)} file(s): {names}{more}"

    def detail(self, args: dict[str, Any], ctx: ToolContext) -> str:
        files = args.get("files") or []
        return "\n".join(f"+ {f.get('path', '?')} ({len((f.get('content') or '').splitlines())} lines)" for f in files)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        files = args.get("files") or []
        if not files:
            raise ToolError("`files` is required and must be a non-empty list.")
        cwd = (args.get("cwd") or "").strip().strip("/")

        created: list[str] = []
        for entry in files:
            rel_path = entry.get("path")
            content = entry.get("content")
            if not rel_path:
                raise ToolError("Every entry needs a `path`.")
            if content is None:
                raise ToolError(f"`{rel_path}` needs `content` (empty string for an empty file).")
            if Path(rel_path).is_absolute():
                raise ToolError(f"`{rel_path}` must be relative — create_project always writes inside the workspace.")
            combined = f"{cwd}/{rel_path}" if cwd else rel_path
            full = resolve_path(ctx, combined)
            full.parent.mkdir(parents=True, exist_ok=True)
            try:
                full.write_text(content, encoding="utf-8")
            except OSError as exc:
                raise ToolError(f"Could not write {rel(ctx, full)}: {exc}") from exc
            ctx.read_files[str(full)] = full.stat().st_mtime
            created.append(rel(ctx, full))

        listing = "\n".join(f"  {p}" for p in created)
        return ToolResult(
            ok=True,
            content=f"Created {len(created)} file(s):\n{listing}",
            display=f"created {len(created)} files",
            meta={"paths": created},
        )


class EditFileTool(Tool):
    name = "edit_file"
    description = """
    Replace an exact string in a file. The file must have been read first.
    `old_string` must match the file byte for byte (including indentation) and
    must be unique, unless replace_all is true. Keep the match small but
    unambiguous — a few surrounding lines is usually right.
    """
    action = Action.WRITE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text. Empty string deletes it."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring uniqueness."},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    def preview(self, args: dict[str, Any]) -> str:
        scope = "all occurrences in " if args.get("replace_all") else ""
        return f"Edit {scope}{args.get('path', '?')}"

    def detail(self, args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            path = resolve_path(ctx, args.get("path", ""), must_exist=True)
            before = path.read_text("utf-8", errors="replace")
        except (ToolError, OSError):
            return ""
        after = self._apply(before, args, rel(ctx, path), strict=False)
        return unified_diff(before, after, rel(ctx, path))

    @staticmethod
    def _apply(text: str, args: dict[str, Any], name: str, strict: bool = True) -> str:
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        if old == new:
            if strict:
                raise ToolError("old_string and new_string are identical — nothing to do.")
            return text
        count = text.count(old)
        if count == 0:
            if strict:
                raise ToolError(
                    f"old_string was not found in {name}. It must match exactly, including "
                    "whitespace and indentation — re-read the file and copy the text verbatim."
                )
            return text
        if count > 1 and not args.get("replace_all"):
            if strict:
                raise ToolError(
                    f"old_string appears {count} times in {name}. Add surrounding lines to make "
                    "it unique, or pass replace_all: true to change every occurrence."
                )
            return text
        return text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
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
            # Someone (the user, a build, another tool) touched it since we
            # looked. Refuse rather than clobber, and make the model re-read.
            ctx.read_files.pop(key, None)
            raise ToolError(
                f"{rel(ctx, path)} changed on disk since you read it. Read it again, then re-apply the edit."
            )

        before = path.read_text("utf-8", errors="replace")
        after = self._apply(before, args, rel(ctx, path))
        path.write_text(after, encoding="utf-8")
        ctx.read_files[key] = path.stat().st_mtime

        added, removed = diff_stat(before, after)
        occurrences = before.count(args.get("old_string", "")) if args.get("replace_all") else 1
        return ToolResult(
            ok=True,
            content=f"Edited {rel(ctx, path)} — {occurrences} replacement(s), +{added}/-{removed}.",
            display=f"edited {rel(ctx, path)}  +{added}/-{removed}",
            meta={"diff": unified_diff(before, after, rel(ctx, path)), "path": str(path)},
        )


class DeletePathTool(Tool):
    name = "delete_path"
    description = """
    Delete a file, or an empty directory. Directories with contents are refused —
    say what you want removed and let the user do it, or remove files one by one.
    """
    action = Action.WRITE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return str(args.get("path", ""))

    def preview(self, args: dict[str, Any]) -> str:
        return f"Delete {args.get('path', '?')}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(ctx, args.get("path", ""), must_exist=True)
        if path.is_dir():
            if any(path.iterdir()):
                raise ToolError(f"{rel(ctx, path)} is not empty. Refusing to delete a non-empty directory.")
            path.rmdir()
            return ToolResult(ok=True, content=f"Removed empty directory {rel(ctx, path)}.")
        size = path.stat().st_size
        path.unlink()
        ctx.read_files.pop(str(path), None)
        return ToolResult(
            ok=True,
            content=f"Deleted {rel(ctx, path)} ({size:,} bytes).",
            display=f"deleted {rel(ctx, path)}",
        )


class ListDirTool(Tool):
    name = "list_dir"
    description = """
    List a directory's contents. Dependency and build directories
    (node_modules, .git, __pycache__, .venv, dist…) are skipped.
    """
    action = Action.READ
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory. Defaults to the workspace root."},
            "depth": {"type": "integer", "description": "How many levels to descend. Default 1, max 4."},
        },
    }

    def preview(self, args: dict[str, Any]) -> str:
        return f"List {args.get('path') or '.'}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = resolve_path(ctx, args.get("path") or ".", must_exist=True)
        if not root.is_dir():
            raise ToolError(f"{rel(ctx, root)} is not a directory.")
        depth = max(1, min(4, int(args.get("depth") or 1)))

        lines: list[str] = []
        count = 0

        def walk(directory: Path, level: int, prefix: str) -> None:
            nonlocal count
            if level > depth or count > 800:
                return
            try:
                entries = sorted(
                    directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
                )
            except OSError:
                return
            for entry in entries:
                if entry.name in SKIP_DIRS or entry.name.startswith(".git"):
                    continue
                count += 1
                if entry.is_dir():
                    lines.append(f"{prefix}{entry.name}/")
                    walk(entry, level + 1, prefix + "  ")
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"{prefix}{entry.name}  ({size:,}b)")

        walk(root, 1, "")
        body = "\n".join(lines) or "(empty)"
        return ToolResult(
            ok=True,
            content=f"{rel(ctx, root)}/\n{body}",
            display=f"{count} entries",
        )


class FindFilesTool(Tool):
    name = "find_files"
    description = """
    Find files by glob pattern (e.g. "**/*.py", "src/**/test_*.js"),
    newest first. Use this when you know roughly what a file is called.
    """
    action = Action.READ
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": 'Glob, e.g. "**/*.ts".'},
            "path": {"type": "string", "description": "Directory to search from. Defaults to the workspace."},
            "limit": {"type": "integer", "description": "Max results. Default 100."},
        },
        "required": ["pattern"],
    }

    def preview(self, args: dict[str, Any]) -> str:
        return f"Find {args.get('pattern', '?')}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = resolve_path(ctx, args.get("path") or ".", must_exist=True)
        pattern = args.get("pattern") or "*"
        limit = int(args.get("limit") or 100)

        matches: list[tuple[float, Path]] = []
        for path in iter_files(root):
            candidate = str(path.relative_to(root))
            if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(path.name, pattern):
                try:
                    matches.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        matches.sort(reverse=True)
        found = [rel(ctx, p) for _, p in matches[:limit]]
        if not found:
            return ToolResult(
                ok=True,
                content=f"No files match {pattern!r} under {rel(ctx, root)}.",
                display="no matches",
            )
        more = f"\n… {len(matches) - len(found)} more" if len(matches) > len(found) else ""
        return ToolResult(
            ok=True,
            content=f"{len(matches)} match(es) for {pattern!r}:\n" + "\n".join(found) + more,
            display=f"{len(matches)} match(es)",
        )


class SearchTextTool(Tool):
    name = "search_text"
    description = """
    Search file contents with a regular expression (Python syntax), like grep -rn.
    Filter with `glob` (e.g. "*.py"). Returns path:line: matching text.
    This is the fastest way to find where something is defined or used.
    """
    action = Action.READ
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression."},
            "path": {"type": "string", "description": "File or directory to search. Defaults to the workspace."},
            "glob": {"type": "string", "description": 'Only search files matching this glob, e.g. "*.py".'},
            "ignore_case": {"type": "boolean"},
            "context": {"type": "integer", "description": "Lines of context around each match. Default 0."},
            "limit": {"type": "integer", "description": "Max matching lines. Default 100."},
        },
        "required": ["pattern"],
    }

    def preview(self, args: dict[str, Any]) -> str:
        where = args.get("glob") or args.get("path") or "workspace"
        return f"Search /{args.get('pattern', '?')}/ in {where}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = resolve_path(ctx, args.get("path") or ".", must_exist=True)
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            regex = re.compile(args.get("pattern", ""), flags)
        except re.error as exc:
            raise ToolError(f"Invalid regular expression: {exc}") from exc

        glob = args.get("glob")
        limit = int(args.get("limit") or 100)
        context = max(0, min(5, int(args.get("context") or 0)))

        candidates = [root] if root.is_file() else list(iter_files(root))
        hits: list[str] = []
        files_hit = 0
        for path in candidates:
            if glob and not (fnmatch.fnmatch(path.name, glob) or fnmatch.fnmatch(str(path), glob)):
                continue
            if looks_binary(path):
                continue
            try:
                lines = path.read_text("utf-8", errors="replace").splitlines()
            except OSError:
                continue
            matched_here = False
            for i, line in enumerate(lines):
                if not regex.search(line):
                    continue
                matched_here = True
                name = rel(ctx, path)
                if context:
                    lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                    for j in range(lo, hi):
                        marker = ":" if j == i else "-"
                        hits.append(f"{name}:{j + 1}{marker}{lines[j][:400]}")
                    hits.append("--")
                else:
                    hits.append(f"{name}:{i + 1}:{line.strip()[:400]}")
                if len(hits) >= limit:
                    break
            if matched_here:
                files_hit += 1
            if len(hits) >= limit:
                hits.append(f"… stopped at {limit} matches — narrow the pattern for the rest")
                break

        if not hits:
            return ToolResult(
                ok=True,
                content=f"No matches for /{args.get('pattern')}/ under {rel(ctx, root)}"
                        + (f" (glob {glob})" if glob else "") + ".",
                display="no matches",
            )
        return ToolResult(
            ok=True,
            content=truncate("\n".join(hits), ctx.config.max_tool_output_chars, "matches"),
            display=f"{len(hits)} line(s) in {files_hit} file(s)",
        )


class TodoWriteTool(Tool):
    name = "todo_write"
    description = """
    Record or update your task list for multi-step work. Pass the whole list each
    time. Statuses: pending, in_progress, completed. Keep exactly one task
    in_progress. The user sees this list, so it is also how you show progress.
    """
    action = Action.NONE
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    },
                    "required": ["task", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    def preview(self, args: dict[str, Any]) -> str:
        return f"Update task list ({len(args.get('todos') or [])} items)"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        todos = args.get("todos") or []
        if not isinstance(todos, list):
            raise ToolError("`todos` must be a list of {task, status} objects.")
        ctx.todos.clear()
        for item in todos:
            if not isinstance(item, dict) or "task" not in item:
                continue
            ctx.todos.append({
                "task": str(item.get("task")),
                "status": str(item.get("status", "pending")),
            })
        ctx.ui.render_todos(ctx.todos)
        done = sum(1 for t in ctx.todos if t["status"] == "completed")
        return ToolResult(
            ok=True,
            content="Task list updated:\n" + "\n".join(
                f"[{'x' if t['status'] == 'completed' else '~' if t['status'] == 'in_progress' else ' '}] {t['task']}"
                for t in ctx.todos
            ),
            display=f"{done}/{len(ctx.todos)} done",
        )
