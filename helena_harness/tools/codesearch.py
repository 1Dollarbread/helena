"""Semantic code search — the RAG half of the coding-ability upgrades.

search_text (files.py) is exact regex — great when you know the string,
useless when you only know the *idea* ("where do we validate the session
token"). This embeds the query with the same model memory.py already uses
for cross-project recall, and searches a per-project chunk index built
incrementally from the workspace's own files (see codebase_index.py) — no
new dependency, no separate vector database.
"""

from __future__ import annotations

from typing import Any

from ..codebase_index import ensure_fresh, search
from ..permissions import Action
from .base import Tool, ToolContext, ToolError, ToolResult


class SearchCodebaseTool(Tool):
    name = "search_codebase"
    description = """
    Semantic search over this project's own source, by meaning rather than
    exact text — use this when you know roughly what something does but not
    what it's called or where it lives ("where do we validate the session
    token", "rate limiting logic"). For an exact string or symbol name,
    search_text is faster and more precise; reach for this when search_text
    would require guessing the wording.

    The index is built and kept fresh automatically (incrementally, by file
    mtime) — the first call in a fresh project may take a little longer while
    it embeds everything for the first time.
    """
    action = Action.READ
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What you're looking for, in plain language."},
            "top_k": {"type": "integer", "description": "How many chunks to return. Default 8."},
        },
        "required": ["query"],
    }

    def preview(self, args: dict[str, Any]) -> str:
        return f"Semantic search: {args.get('query', '?')}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            raise ToolError("`query` is required.")
        top_k = max(1, min(25, int(args.get("top_k") or 8)))

        try:
            await ensure_fresh(ctx)
        except Exception as exc:
            ctx.ui.warn(f"(codebase index refresh hit a snag: {exc} — searching what's already indexed)")

        try:
            vectors = await ctx.client.embed([query], model=ctx.config.embed_model or None)
        except Exception as exc:
            raise ToolError(f"Could not embed the query: {exc}") from exc
        if not vectors:
            raise ToolError("The embedding model returned nothing — is one pulled? (`/doctor`)")

        hits = search(ctx, vectors[0], top_k=top_k)
        if not hits:
            return ToolResult(
                ok=True,
                content="No indexed matches yet — the project may still be small, unindexed, "
                        "or the embedding model isn't returning useful vectors for this query.",
                display="no matches",
            )
        rendered = "\n\n".join(
            f"{h['path']}:{h['start_line']}-{h['end_line']} (score {h['score']})\n{h['text']}"
            for h in hits
        )
        return ToolResult(ok=True, content=rendered, display=f"{len(hits)} match(es)")
