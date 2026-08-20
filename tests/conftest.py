"""Shared fixtures: a fake Ollama, an in-process server, and a scripted client."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from helena_harness.config import Config
from helena_harness.permissions import PermissionEngine
from helena_harness.tools.base import ToolContext
from helena_harness.ui import SilentUI


class FakeOllama:
    """Stands in for `helena_server.ollama.OllamaClient`.

    Scripted with a list of assistant replies; each call to chat/chat_stream
    consumes the next one. A reply is a dict like
    `{"content": "...", "tool_calls": [...]}`.
    """

    def __init__(self, replies: list[dict[str, Any]] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []
        self.host = "http://fake-ollama"
        self.models_data = [
            {
                "name": "qwen2.5:7b-instruct",
                "size": 4_700_000_000,
                "details": {"parameter_size": "7B", "quantization_level": "Q4_K_M", "family": "qwen2"},
            },
            {"name": "llava:latest", "size": 4_100_000_000, "details": {"parameter_size": "7B", "family": "clip"}},
        ]

    def _next(self) -> dict[str, Any]:
        if not self.replies:
            return {"content": "(no scripted reply left)"}
        return self.replies.pop(0)

    async def version(self) -> str:
        return "0.6.0-fake"

    async def list_models(self) -> list[dict[str, Any]]:
        return self.models_data

    async def show(self, name: str) -> dict[str, Any]:
        return {"details": {"family": "qwen2"}, "capabilities": ["completion", "tools"]}

    async def chat(self, *, model, messages, tools=None, options=None, fmt=None, keep_alive=None):
        self.calls.append({"model": model, "messages": messages, "tools": tools, "options": options})
        reply = self._next()
        return {
            "model": model,
            "message": {
                "role": "assistant",
                "content": reply.get("content", ""),
                "tool_calls": reply.get("tool_calls", []),
            },
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 120,
            "eval_count": 30,
            "total_duration": 1_500_000_000,
            "eval_duration": 1_000_000_000,
        }

    async def chat_stream(self, *, model, messages, tools=None, options=None, fmt=None, keep_alive=None) -> AsyncIterator[dict[str, Any]]:
        self.calls.append({"model": model, "messages": messages, "tools": tools, "options": options})
        reply = self._next()
        for piece in _chunks(reply.get("content", "")):
            yield {"message": {"role": "assistant", "content": piece}, "done": False}
            await asyncio.sleep(0)
        final: dict[str, Any] = {
            "model": model,
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 120,
            "eval_count": 30,
            "total_duration": 1_500_000_000,
            "eval_duration": 1_000_000_000,
        }
        if reply.get("tool_calls"):
            final["message"]["tool_calls"] = reply["tool_calls"]
        yield final

    async def embed(self, *, model, inputs):
        return [[0.1, 0.2, 0.3] for _ in inputs]

    async def pull(self, name, insecure=False):
        yield {"status": "pulling manifest"}
        yield {"status": "downloading", "total": 100, "completed": 50}
        yield {"status": "success"}

    async def delete_model(self, name: str) -> None:
        self.models_data = [m for m in self.models_data if m["name"] != name]

    async def aclose(self) -> None:
        return None


def _chunks(text: str, size: int = 8) -> list[str]:
    return [text[i: i + size] for i in range(0, len(text), size)] or []


@pytest.fixture
def fake_ollama() -> FakeOllama:
    return FakeOllama()


@pytest.fixture
def server_settings(tmp_path, monkeypatch):
    """Point the app at a temp database and a known default model."""
    from helena_server import app as app_module
    from helena_server.config import Settings, get_settings

    get_settings.cache_clear()
    settings = Settings(
        db_path=str(tmp_path / "test.db"), api_token="", default_model="qwen2.5:7b-instruct"
    )
    # The app resolves settings through this module-level name in both the
    # lifespan and the dependency, so one patch covers everything.
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    app_module.app.dependency_overrides[app_module.settings_dep] = lambda: settings
    yield settings
    app_module.app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def server_app(server_settings):
    """The real FastAPI app. Its lifespan runs when a TestClient starts it."""
    from helena_server import app as app_module

    return app_module.app


@pytest.fixture
def asgi_app(server_settings, fake_ollama, tmp_path):
    """The app with its state wired by hand, for httpx.ASGITransport tests.

    ASGITransport does not run the lifespan, so the objects the lifespan would
    have built are supplied here instead.
    """
    import asyncio as _asyncio

    from helena_server import app as app_module
    from helena_server.store import SessionStore

    app = app_module.app
    store = SessionStore(tmp_path / "asgi.db")
    app.state.settings = server_settings
    app.state.ollama = fake_ollama
    app.state.store = store
    app.state.semaphore = _asyncio.Semaphore(2)
    yield app
    store.close()


@pytest.fixture
def server_client(server_app, fake_ollama):
    """A started app with the fake Ollama swapped in after startup."""
    from fastapi.testclient import TestClient

    with TestClient(server_app) as client:
        # Lifespan built a real OllamaClient; replace it now that it exists.
        server_app.state.ollama = fake_ollama
        yield client


@pytest.fixture
def harness_config(tmp_path) -> Config:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = Config(workspace=workspace, mode="yolo", stream=True)
    config.model = "qwen2.5:7b-instruct"
    return config


@pytest.fixture
def tool_ctx(harness_config) -> ToolContext:
    return ToolContext(
        workspace=harness_config.workspace,
        config=harness_config,
        client=None,  # tools that need the server get it injected per-test
        permissions=PermissionEngine(harness_config),
        ui=SilentUI(),
    )


@pytest.fixture
def workspace(harness_config) -> Path:
    return harness_config.workspace
