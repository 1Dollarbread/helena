"""HTTP client for the HELENA model server.

The harness never talks to Ollama directly — everything goes through the
FastAPI server, so the model layer can move (another machine, a GPU box on the
LAN) without the harness noticing.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx


class ServerError(RuntimeError):
    pass


@dataclass
class StreamResult:
    """Everything a single completion produced."""

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    done_reason: str | None = None


class ServerClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 900.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # `transport` exists so tests can drive the FastAPI app in-process over
        # ASGI, exercising the real HTTP layer without binding a port.
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers=headers,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- introspection -----------------------------------------------------

    async def health(self) -> dict[str, Any]:
        try:
            res = await self._client.get("/health", timeout=10.0)
            res.raise_for_status()
            return res.json()
        except httpx.HTTPError as exc:
            raise ServerError(f"HELENA server unreachable at {self.base_url}: {exc}") from exc

    async def models(self) -> dict[str, Any]:
        res = await self._client.get("/v1/models", timeout=30.0)
        self._raise_for_status(res)
        return res.json()

    async def pull(self, name: str) -> AsyncIterator[dict[str, Any]]:
        async with self._client.stream(
            "POST", "/v1/models/pull", json={"name": name}, timeout=None
        ) as res:
            self._raise_for_status(res)
            async for event in self._iter_sse(res):
                yield event

    # --- generation --------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
        fmt: str | dict[str, Any] | None = None,
    ) -> StreamResult:
        payload = self._payload(messages, model, tools, options, fmt, stream=False)
        res = await self._client.post("/v1/chat", json=payload)
        self._raise_for_status(res)
        data = res.json()
        msg = data.get("message") or {}
        return StreamResult(
            content=msg.get("content") or "",
            tool_calls=msg.get("tool_calls") or [],
            usage=data.get("usage") or {},
            model=data.get("model", ""),
            done_reason=data.get("done_reason"),
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
        fmt: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yields `{"type": "token"|"tool_calls"|"done"|"error", ...}` events."""
        payload = self._payload(messages, model, tools, options, fmt, stream=True)
        async with self._client.stream(
            "POST", "/v1/chat/stream", json=payload, timeout=None
        ) as res:
            self._raise_for_status(res)
            async for event in self._iter_sse(res):
                yield event

    async def vision(
        self, images: list[str], prompt: str, *, model: str | None = None, system: str | None = None
    ) -> StreamResult:
        payload: dict[str, Any] = {"images": images, "prompt": prompt}
        if model:
            payload["model"] = model
        if system:
            payload["system"] = system
        res = await self._client.post("/v1/vision", json=payload, timeout=None)
        self._raise_for_status(res)
        data = res.json()
        return StreamResult(
            content=(data.get("message") or {}).get("content", ""),
            usage=data.get("usage") or {},
            model=data.get("model", ""),
        )

    async def embed(self, inputs: list[str], *, model: str | None = None) -> list[list[float]]:
        payload: dict[str, Any] = {"input": inputs}
        if model:
            payload["model"] = model
        res = await self._client.post("/v1/embeddings", json=payload, timeout=60.0)
        self._raise_for_status(res)
        return res.json().get("embeddings") or []

    # --- helpers -----------------------------------------------------------

    def _payload(
        self,
        messages: list[dict[str, Any]],
        model: str | None,
        tools: list[dict[str, Any]] | None,
        options: dict[str, Any] | None,
        fmt: str | dict[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"messages": messages, "stream": stream}
        if model:
            payload["model"] = model
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        if fmt is not None:
            payload["format"] = fmt
        return payload

    @staticmethod
    def _raise_for_status(res: httpx.Response) -> None:
        if res.status_code < 400:
            return
        detail = ""
        try:
            body = res.read() if not res.is_closed else b""
            detail = json.loads(body).get("detail", "") if body else ""
        except Exception:
            detail = ""
        raise ServerError(f"Server returned HTTP {res.status_code}{': ' + detail if detail else ''}")

    @staticmethod
    async def _iter_sse(res: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        async for line in res.aiter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


async def ensure_server(base_url: str, token: str = "", auto_start: bool = True) -> tuple[bool, str]:
    """Make sure a server is answering; start one locally if not.

    Returns (ok, message). Only local URLs are auto-started — spawning a
    process because a remote host is down would be surprising and useless.
    """
    probe = ServerClient(base_url, token, timeout=10.0)
    try:
        await probe.health()
        return True, "already running"
    except ServerError:
        pass
    finally:
        await probe.aclose()

    is_local = any(h in base_url for h in ("127.0.0.1", "localhost", "0.0.0.0"))
    if not auto_start or not is_local:
        return False, f"no server at {base_url}"

    port = base_url.rsplit(":", 1)[-1].split("/")[0]
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "helena_server", "--port", port, "--log-level", "warning"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ},
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"could not start server: {exc}"

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, "server process exited during startup"
        client = ServerClient(base_url, token, timeout=5.0)
        try:
            await client.health()
            return True, f"started (pid {proc.pid})"
        except ServerError:
            await asyncio.sleep(0.4)
        finally:
            await client.aclose()
    return False, "server did not become ready within 20s"
