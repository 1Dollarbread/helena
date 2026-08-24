"""Cross-project memory: the sqlite store, the embeddings plumbing on top of
it, the recall step that injects relevant memories into the system prompt,
and the /remember tool that feeds it."""

from __future__ import annotations

import httpx
import pytest

from helena_harness import memory as memory_store
from helena_harness import profile as profile_store
from helena_harness.agent import Agent
from helena_harness.client import ServerClient, ServerError
from helena_harness.permissions import PermissionEngine
from helena_harness.tools import build_tools
from helena_harness.tools.base import ToolContext
from helena_harness.tools.extras import RememberTool
from helena_harness.ui import SilentUI


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_PATH", tmp_path / "memory.db")
    return memory_store


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_store, "PROFILE_PATH", tmp_path / "profile.json")
    return profile_store


# --- the sqlite store itself -------------------------------------------------


def test_save_and_search_finds_the_similar_memory(isolated_memory):
    isolated_memory.save("likes tabs over spaces", [1.0, 0.0, 0.0], project="app-a")
    isolated_memory.save("prefers dark mode everywhere", [0.0, 1.0, 0.0], project="app-b")

    hits = isolated_memory.search([1.0, 0.0, 0.0])

    assert len(hits) == 1
    assert hits[0]["text"] == "likes tabs over spaces"
    assert hits[0]["project"] == "app-a"
    assert hits[0]["score"] == pytest.approx(1.0)


def test_search_respects_min_score_threshold(isolated_memory):
    isolated_memory.save("unrelated note", [0.0, 1.0, 0.0])

    assert isolated_memory.search([1.0, 0.0, 0.0], min_score=0.5) == []


def test_search_orders_hits_by_similarity(isolated_memory):
    isolated_memory.save("closest", [1.0, 0.0])
    isolated_memory.save("mid", [0.8, 0.6])
    isolated_memory.save("far", [0.1, 0.99])

    hits = isolated_memory.search([1.0, 0.0], top_k=3, min_score=0.0)

    assert [h["text"] for h in hits] == ["closest", "mid", "far"]


def test_search_handles_a_zero_query_vector_without_crashing(isolated_memory):
    isolated_memory.save("something", [1.0, 0.0])

    # A zero vector has no direction, so cosine similarity against it is 0.0
    # for anything — below the default threshold, and no crash either way.
    assert isolated_memory.search([0.0, 0.0]) == []


def test_top_k_caps_the_number_of_hits(isolated_memory):
    for i in range(5):
        isolated_memory.save(f"note {i}", [1.0, 0.0])

    assert len(isolated_memory.search([1.0, 0.0], top_k=2, min_score=0.0)) == 2


def test_forget_removes_a_memory(isolated_memory):
    row = isolated_memory.save("temporary note", [1.0, 0.0])
    assert isolated_memory.count() == 1

    assert isolated_memory.forget(row["id"]) is True
    assert isolated_memory.count() == 0
    assert isolated_memory.forget(row["id"]) is False


def test_all_memories_orders_most_recent_first(isolated_memory):
    isolated_memory.save("first", [1.0, 0.0])
    isolated_memory.save("second", [0.0, 1.0])

    rows = isolated_memory.all_memories()

    assert [r["text"] for r in rows] == ["second", "first"]
    assert "embedding" not in rows[0]


# --- the embeddings client + the best-effort save helper ---------------------


async def test_server_client_embed_hits_the_real_endpoint(asgi_app):
    client = ServerClient("http://server", transport=httpx.ASGITransport(app=asgi_app))
    try:
        vectors = await client.embed(["hello", "world"])
    finally:
        await client.aclose()

    assert vectors == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]


async def test_remember_saves_the_embedded_memory(asgi_app, isolated_memory):
    client = ServerClient("http://server", transport=httpx.ASGITransport(app=asgi_app))
    try:
        row = await memory_store.remember(client, "", "my-project", "prefers pytest")
    finally:
        await client.aclose()

    assert row["text"] == "prefers pytest"
    assert isolated_memory.count() == 1


async def test_remember_is_silent_when_embedding_fails(isolated_memory):
    class BrokenClient:
        async def embed(self, inputs, *, model=None):
            raise ServerError("no server")

    result = await memory_store.remember(BrokenClient(), "", None, "some fact")

    assert result is None
    assert isolated_memory.count() == 0


# --- the remember tool: profile fact + cross-project memory, together -------


async def test_remember_tool_saves_to_both_profile_and_memory(
    asgi_app, harness_config, isolated_memory
):
    client = ServerClient("http://server", transport=httpx.ASGITransport(app=asgi_app))
    ctx = ToolContext(
        workspace=harness_config.workspace,
        config=harness_config,
        client=client,
        permissions=PermissionEngine(harness_config),
        ui=SilentUI(),
    )
    try:
        result = await RememberTool().run({"fact": "prefers small PRs"}, ctx)
    finally:
        await client.aclose()

    assert result.ok
    assert profile_store.load_profile()["facts"][-1]["text"] == "prefers small PRs"
    rows = isolated_memory.all_memories()
    assert rows[0]["text"] == "prefers small PRs"
    assert rows[0]["project"] == harness_config.workspace.name


async def test_remember_tool_skips_memory_when_disabled(asgi_app, harness_config, isolated_memory):
    harness_config.memory_enabled = False
    client = ServerClient("http://server", transport=httpx.ASGITransport(app=asgi_app))
    ctx = ToolContext(
        workspace=harness_config.workspace,
        config=harness_config,
        client=client,
        permissions=PermissionEngine(harness_config),
        ui=SilentUI(),
    )
    try:
        await RememberTool().run({"fact": "should not be embedded"}, ctx)
    finally:
        await client.aclose()

    assert isolated_memory.count() == 0
    assert profile_store.load_profile()["facts"][-1]["text"] == "should not be embedded"


# --- the agent recalling memory before it answers ---------------------------


async def _build_agent(asgi_app, harness_config) -> tuple[Agent, ServerClient]:
    client = ServerClient("http://server", transport=httpx.ASGITransport(app=asgi_app))
    ctx = ToolContext(
        workspace=harness_config.workspace,
        config=harness_config,
        client=client,
        permissions=PermissionEngine(harness_config),
        ui=SilentUI(),
    )
    return Agent(ctx=ctx, tools=build_tools(ctx), label="HELENA"), client


async def test_agent_recalls_relevant_memory_before_answering(
    asgi_app, fake_ollama, harness_config, isolated_memory
):
    # The fake embedding model always returns [0.1, 0.2, 0.3] regardless of
    # input text, so a memory saved with that same vector is a guaranteed
    # match for whatever the agent embeds next.
    isolated_memory.save("prefers pytest over unittest", [0.1, 0.2, 0.3], project="other-project")
    fake_ollama.replies = [{"content": "Noted."}]

    agent, client = await _build_agent(asgi_app, harness_config)
    try:
        await agent.send("what testing framework should I use?")
    finally:
        await client.aclose()

    system = fake_ollama.calls[-1]["messages"][0]["content"]
    assert "Recalled from other sessions" in system
    assert "prefers pytest over unittest" in system


async def test_agent_adds_no_recall_block_when_memory_is_empty(
    asgi_app, fake_ollama, harness_config, isolated_memory
):
    fake_ollama.replies = [{"content": "ok"}]

    agent, client = await _build_agent(asgi_app, harness_config)
    try:
        await agent.send("hi")
    finally:
        await client.aclose()

    system = fake_ollama.calls[-1]["messages"][0]["content"]
    assert "Recalled from other sessions" not in system


async def test_memory_disabled_skips_recall_even_with_a_matching_memory(
    asgi_app, fake_ollama, harness_config, isolated_memory
):
    isolated_memory.save("prefers pytest over unittest", [0.1, 0.2, 0.3])
    fake_ollama.replies = [{"content": "ok"}]
    harness_config.memory_enabled = False

    agent, client = await _build_agent(asgi_app, harness_config)
    try:
        await agent.send("what testing framework should I use?")
    finally:
        await client.aclose()

    system = fake_ollama.calls[-1]["messages"][0]["content"]
    assert "Recalled from other sessions" not in system


async def test_subagent_gets_no_recall_block(asgi_app, fake_ollama, harness_config, isolated_memory):
    """Subagents use a fixed system_prompt (see build_system_prompt), so memory
    recall — which is about knowing the user across the *main* conversation —
    should never fire (and never spend an embedding call) for one."""
    isolated_memory.save("prefers pytest over unittest", [0.1, 0.2, 0.3])
    client = ServerClient("http://server", transport=httpx.ASGITransport(app=asgi_app))
    ctx = ToolContext(
        workspace=harness_config.workspace,
        config=harness_config,
        client=client,
        permissions=PermissionEngine(harness_config),
        ui=SilentUI(),
    )
    sub = Agent(ctx=ctx, tools=build_tools(ctx), label="sub", system_prompt="You are a subagent.", nested=True)
    try:
        recalled = await sub.recall_memory("what testing framework should I use?")
    finally:
        await client.aclose()

    assert recalled == ""
