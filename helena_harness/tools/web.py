"""Web tools: DuckDuckGo search and URL fetching.

DuckDuckGo is the only search engine with a usable keyless surface, and it has
two of them:

* the Instant Answer API (api.duckduckgo.com) — structured facts, definitions,
  and topic summaries, but no ranked web results;
* the HTML endpoints (html./lite.duckduckgo.com) — the actual result list that
  the site itself renders.

Neither is a documented, stable API. This module uses the Instant Answer API
for a headline fact, the HTML endpoint for real results, and Wikipedia as a
last resort, so a change to any one of them degrades the tool instead of
breaking it.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from ..permissions import Action
from .base import Tool, ToolContext, ToolError, ToolResult, truncate

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
WS_RE = re.compile(r"[ \t]+")
BLANKS_RE = re.compile(r"\n{3,}")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)

# Anchors are matched in two steps — find the tag by its class, then pull the
# href out of its attributes — because DuckDuckGo's two endpoints order those
# attributes differently, and a single positional regex silently matches only
# one of them.
HREF_RE = re.compile(r"""href\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
ANCHOR_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<title>.*?)</a>", re.DOTALL | re.IGNORECASE)
SNIPPET_RE = re.compile(
    r"""<(?P<tag>a|td)\b[^>]*class\s*=\s*['"][^'"]*result[-_]+snippet[^'"]*['"][^>]*>(?P<snippet>.*?)</(?P=tag)>""",
    re.DOTALL | re.IGNORECASE,
)
RESULT_CLASS_RE = re.compile(
    r"""class\s*=\s*['"][^'"]*\b(result__a|result-link)\b[^'"]*['"]""", re.IGNORECASE
)


def strip_tags(fragment: str) -> str:
    return html.unescape(TAG_RE.sub("", fragment)).strip()


def clean_ddg_url(href: str) -> str:
    """DuckDuckGo wraps outbound links in /l/?uddg=<encoded> redirects."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        params = urllib.parse.parse_qs(parsed.query)
        target = params.get("uddg", [None])[0]
        if target:
            return urllib.parse.unquote(target)
    return href


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


def parse_ddg_html(body: str, limit: int = 10) -> list[SearchHit]:
    """Extract results from either DuckDuckGo HTML endpoint."""
    snippets = [strip_tags(m.group("snippet")) for m in SNIPPET_RE.finditer(body)]
    hits: list[SearchHit] = []

    for match in ANCHOR_RE.finditer(body):
        attrs = match.group("attrs")
        if not RESULT_CLASS_RE.search(attrs):
            continue
        href_match = HREF_RE.search(attrs)
        if not href_match:
            continue
        url = clean_ddg_url(html.unescape(href_match.group(1)))
        title = strip_tags(match.group("title"))
        if not title or not url.startswith("http"):
            continue
        index = len(hits)
        hits.append(SearchHit(title=title, url=url, snippet=snippets[index] if index < len(snippets) else ""))
        if len(hits) >= limit:
            break
    return hits


def html_to_text(body: str) -> str:
    body = SCRIPT_RE.sub(" ", body)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</(p|div|li|tr|h[1-6]|section|article)>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"<li[^>]*>", "• ", body, flags=re.IGNORECASE)
    text = strip_tags(body)
    text = WS_RE.sub(" ", text)
    return BLANKS_RE.sub("\n\n", text).strip()


async def instant_answer(client: httpx.AsyncClient, query: str) -> dict[str, Any] | None:
    url = "https://api.duckduckgo.com/"
    params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
    try:
        res = await client.get(url, params=params, headers=HEADERS, timeout=15.0)
        if res.status_code != 200:
            return None
        data = res.json()
    except (httpx.HTTPError, ValueError):
        return None
    text = data.get("AbstractText") or data.get("Answer") or ""
    if not text:
        related = data.get("RelatedTopics") or []
        for item in related:
            if isinstance(item, dict) and item.get("Text"):
                text = item["Text"]
                data["AbstractURL"] = item.get("FirstURL") or data.get("AbstractURL")
                break
    if not text:
        return None
    return {
        "text": text,
        "source": data.get("AbstractSource") or "DuckDuckGo",
        "url": data.get("AbstractURL") or "",
    }


async def ddg_results(client: httpx.AsyncClient, query: str, limit: int) -> list[SearchHit]:
    for endpoint in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        try:
            res = await client.post(endpoint, data={"q": query}, headers=HEADERS, timeout=20.0)
            if res.status_code != 200:
                continue
            hits = parse_ddg_html(res.text, limit)
            if hits:
                return hits
        except httpx.HTTPError:
            continue
    return []


async def wikipedia_summary(client: httpx.AsyncClient, query: str) -> dict[str, Any] | None:
    try:
        search = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "format": "json", "srlimit": 1},
            headers=HEADERS, timeout=15.0,
        )
        hit = (search.json().get("query", {}).get("search") or [None])[0]
        if not hit:
            return None
        title = hit["title"]
        summary = await client.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}",
            headers=HEADERS, timeout=15.0,
        )
        data = summary.json()
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    if not data.get("extract"):
        return None
    return {
        "text": data["extract"],
        "source": f"Wikipedia — {title}",
        "url": (data.get("content_urls", {}).get("desktop", {}) or {}).get("page", ""),
    }


class WebSearchTool(Tool):
    name = "web_search"
    description = """
    Search the web with DuckDuckGo. Returns ranked results (title, URL, snippet)
    plus an instant answer when one exists.
    Snippets are short — follow up with fetch_url on the most promising result
    when you need the actual content. Use this for anything current, external,
    or outside what you already know.
    """
    action = Action.NETWORK
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "Max results, default 8."},
        },
        "required": ["query"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return str(args.get("query", ""))

    def preview(self, args: dict[str, Any]) -> str:
        return f'Search the web for "{args.get("query", "?")}"'

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            raise ToolError("`query` is required.")
        limit = max(1, min(20, int(args.get("limit") or 8)))

        async with httpx.AsyncClient(follow_redirects=True) as client:
            answer = await instant_answer(client, query)
            hits = await ddg_results(client, query, limit)
            if not hits and not answer:
                answer = await wikipedia_summary(client, query)

        if not hits and not answer:
            return ToolResult(
                ok=False,
                content=(
                    f"No results for {query!r}. DuckDuckGo returned nothing usable — it "
                    "rate-limits aggressively when queried in bursts. Try again with "
                    "different wording, or fetch a specific URL directly."
                ),
                display="no results",
            )

        lines = [f"Web results for {query!r}"]
        if answer:
            lines.append(f"\nInstant answer ({answer['source']}): {answer['text']}")
            if answer.get("url"):
                lines.append(f"  {answer['url']}")
        if hits:
            lines.append("")
            for i, hit in enumerate(hits, 1):
                lines.append(f"{i}. {hit.title}\n   {hit.url}")
                if hit.snippet:
                    lines.append(f"   {hit.snippet[:400]}")
        return ToolResult(
            ok=True,
            content=truncate("\n".join(lines), ctx.config.max_tool_output_chars, "results"),
            display=f"{len(hits)} result(s)" + (" + instant answer" if answer else ""),
            meta={"hits": [h.__dict__ for h in hits]},
        )


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = """
    Fetch a URL and return its readable text (HTML is stripped to prose; JSON and
    plain text come back as-is). Use after web_search to actually read a page, or
    directly when you already have the URL — including local services you are
    developing against.
    """
    action = Action.NETWORK
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "Cap on returned text. Default 12000."},
        },
        "required": ["url"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return str(args.get("url", ""))

    def preview(self, args: dict[str, Any]) -> str:
        return f"Fetch {args.get('url', '?')}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = (args.get("url") or "").strip()
        if not url:
            raise ToolError("`url` is required.")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        max_chars = max(500, min(60_000, int(args.get("max_chars") or 12_000)))

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                res = await client.get(url, headers=HEADERS)
        except httpx.HTTPError as exc:
            raise ToolError(f"Could not fetch {url}: {exc}") from exc

        content_type = res.headers.get("content-type", "")
        if res.status_code >= 400:
            return ToolResult(
                ok=False,
                content=f"{url} returned HTTP {res.status_code}.",
                display=f"HTTP {res.status_code}",
            )
        if "image/" in content_type or "octet-stream" in content_type:
            return ToolResult(
                ok=False,
                content=f"{url} is {content_type}, not text. Download it with run_command if you need it.",
                display=content_type,
            )

        body = res.text
        if "html" in content_type or body.lstrip().startswith("<!"):
            title_match = TITLE_RE.search(body)
            title = strip_tags(title_match.group(1)) if title_match else ""
            text = html_to_text(body)
            head = f"{title}\n{url}\n\n" if title else f"{url}\n\n"
        else:
            head = f"{url} ({content_type or 'text'})\n\n"
            text = body

        return ToolResult(
            ok=True,
            content=head + truncate(text, max_chars, "page text"),
            display=f"{len(text):,} chars from {urllib.parse.urlparse(url).netloc}",
        )
