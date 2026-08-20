"""Async client for the Ollama HTTP API.

Everything the server can do with a model funnels through here, so the rest
of the app never has to know Ollama's wire format. Ollama streams NDJSON (one
JSON object per line, not SSE), which `_stream_ndjson` normalizes into an
async iterator of dicts.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

# Local models — especially smaller ones — don't all respect chat turn
# boundaries, and will happily keep generating a fake back-and-forth in a
# single response. These stop sequences cut generation the moment the model
# starts inventing a new turn.
TURN_STOP_SEQUENCES = ["\nuser:", "\nUser:", "\nUSER:", "\nyou >", "\nHuman:"]

# Substrings that identify model families. Ollama's /api/tags doesn't reliably
# report capabilities, so name matching is the practical fallback. It only
# affects hints and defaults — never correctness of a request.
VISION_HINTS = (
    "llava", "bakllava", "moondream", "vision", "-vl", "minicpm-v",
    "llama3.2-vision", "gemma3", "qwen2.5vl", "qwen3-vl", "pixtral", "granite3.2-vision",
)
TOOL_HINTS = (
    "llama3.1", "llama3.2", "llama3.3", "llama4", "qwen2.5", "qwen3", "qwq",
    "mistral", "mixtral", "nemo", "firefunction", "command-r", "hermes",
    "granite3", "devstral", "gpt-oss", "smollm2", "cogito", "magistral",
)


class OllamaError(RuntimeError):
    """Raised when Ollama is unreachable or returns an error response."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def infer_capabilities(name: str, families: list[str] | None = None) -> tuple[bool, bool]:
    """Return (supports_vision, supports_tools) inferred from a model name."""
    haystack = name.lower() + " " + " ".join(families or []).lower()
    vision = any(hint in haystack for hint in VISION_HINTS)
    tools = any(hint in haystack for hint in TOOL_HINTS)
    return vision, tools


class OllamaClient:
    def __init__(self, host: str, timeout: float = 600.0) -> None:
        self.host = host.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self.host,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- plumbing ----------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            res = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise OllamaError(f"Could not reach Ollama at {self.host}: {exc}") from exc
        if res.status_code >= 400:
            raise OllamaError(
                f"Ollama returned HTTP {res.status_code}: {res.text.strip()[:500]}",
                status_code=502 if res.status_code >= 500 else 400,
            )
        return res.json()

    async def _stream_ndjson(
        self, path: str, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._client.stream("POST", path, json=payload) as res:
                if res.status_code >= 400:
                    body = (await res.aread()).decode("utf-8", "replace")
                    raise OllamaError(
                        f"Ollama returned HTTP {res.status_code}: {body.strip()[:500]}",
                        status_code=502 if res.status_code >= 500 else 400,
                    )
                async for line in res.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # Ollama occasionally emits a partial line on abort;
                        # skipping it is better than killing the stream.
                        continue
        except httpx.HTTPError as exc:
            raise OllamaError(f"Could not reach Ollama at {self.host}: {exc}") from exc

    # --- introspection -----------------------------------------------------

    async def version(self) -> str | None:
        try:
            res = await self._client.get("/api/version", timeout=5.0)
            if res.status_code == 200:
                return res.json().get("version")
        except httpx.HTTPError:
            return None
        return None

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            res = await self._client.get("/api/tags", timeout=15.0)
        except httpx.HTTPError as exc:
            raise OllamaError(f"Could not reach Ollama at {self.host}: {exc}") from exc
        if res.status_code != 200:
            raise OllamaError(f"Ollama returned HTTP {res.status_code} listing models")
        return res.json().get("models", [])

    async def show(self, name: str) -> dict[str, Any]:
        return await self._post("/api/show", {"model": name})

    async def pull(self, name: str, insecure: bool = False) -> AsyncIterator[dict[str, Any]]:
        payload = {"model": name, "insecure": insecure, "stream": True}
        async for chunk in self._stream_ndjson("/api/pull", payload):
            yield chunk

    async def delete_model(self, name: str) -> None:
        try:
            res = await self._client.request("DELETE", "/api/delete", json={"model": name})
        except httpx.HTTPError as exc:
            raise OllamaError(f"Could not reach Ollama at {self.host}: {exc}") from exc
        if res.status_code >= 400:
            raise OllamaError(
                f"Could not delete {name}: HTTP {res.status_code}",
                status_code=404 if res.status_code == 404 else 400,
            )

    # --- generation --------------------------------------------------------

    def _chat_payload(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        options: dict[str, Any] | None,
        stream: bool,
        fmt: str | dict[str, Any] | None,
        keep_alive: str | None,
    ) -> dict[str, Any]:
        opts = dict(options or {})
        opts.setdefault("stop", TURN_STOP_SEQUENCES)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": opts,
        }
        if tools:
            payload["tools"] = tools
        if fmt is not None:
            payload["format"] = fmt
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        return payload

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
        fmt: str | dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> dict[str, Any]:
        payload = self._chat_payload(
            model=model, messages=messages, tools=tools, options=options,
            stream=False, fmt=fmt, keep_alive=keep_alive,
        )
        return await self._post("/api/chat", payload)

    async def chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
        fmt: str | dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        payload = self._chat_payload(
            model=model, messages=messages, tools=tools, options=options,
            stream=True, fmt=fmt, keep_alive=keep_alive,
        )
        async for chunk in self._stream_ndjson("/api/chat", payload):
            yield chunk

    async def embed(self, *, model: str, inputs: list[str]) -> list[list[float]]:
        data = await self._post("/api/embed", {"model": model, "input": inputs})
        embeddings = data.get("embeddings")
        if embeddings is None and "embedding" in data:  # older Ollama builds
            embeddings = [data["embedding"]]
        return embeddings or []
