"""Image analysis through the server's vision endpoint."""

from __future__ import annotations

import base64
from typing import Any

import httpx

from ..permissions import Action
from .base import IMAGE_SUFFIXES, Tool, ToolContext, ToolError, ToolResult, rel, resolve_path

MAX_IMAGE_BYTES = 20 * 1024 * 1024


class AnalyzeImageTool(Tool):
    name = "analyze_image"
    description = """
    Look at an image and answer a question about it — a screenshot, a photo, a
    diagram, a design mock. Give either a local `path` or a `url`.
    Ask something specific ("what error does this show?", "transcribe the text")
    rather than just "describe it" when you have a specific goal.
    """
    action = Action.READ
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Local image file (png, jpg, gif, webp, bmp)."},
            "url": {"type": "string", "description": "Image URL, if it isn't on disk."},
            "question": {"type": "string", "description": "What you want to know about the image."},
            "model": {"type": "string", "description": "Override the vision model (default: the server's)."},
        },
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return str(args.get("path") or args.get("url") or "")

    def preview(self, args: dict[str, Any]) -> str:
        return f"Look at {args.get('path') or args.get('url') or '?'}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        question = args.get("question") or "Describe this image in detail."
        raw_path, url = args.get("path"), args.get("url")
        if not raw_path and not url:
            raise ToolError("Give either `path` or `url`.")

        if raw_path:
            path = resolve_path(ctx, raw_path, must_exist=True)
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                raise ToolError(
                    f"{rel(ctx, path)} isn't a supported image type "
                    f"({', '.join(sorted(IMAGE_SUFFIXES))})."
                )
            data = path.read_bytes()
            label = rel(ctx, path)
        else:
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                    res = await client.get(url)
                    res.raise_for_status()
                    data = res.content
            except httpx.HTTPError as exc:
                raise ToolError(f"Could not download {url}: {exc}") from exc
            label = url

        if len(data) > MAX_IMAGE_BYTES:
            raise ToolError(f"{label} is {len(data) / 1e6:.1f} MB — over the 20 MB limit.")

        encoded = base64.b64encode(data).decode("ascii")
        result = await ctx.client.vision(
            [encoded], question, model=args.get("model") or ctx.config.vision_model or None
        )
        if not result.content.strip():
            return ToolResult(
                ok=False,
                content=(
                    f"The vision model returned nothing for {label}. Check that a multimodal "
                    "model is pulled (e.g. `ollama pull llava`) and selected."
                ),
                display="empty response",
            )
        return ToolResult(
            ok=True,
            content=f"Looking at {label} — {question}\n\n{result.content.strip()}",
            display=f"{result.model or 'vision model'} described {label}",
        )
