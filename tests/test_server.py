"""Server API tests, against the real FastAPI app with a fake Ollama behind it."""

from __future__ import annotations

import json

import pytest

from helena_server.app import from_ollama_message, to_ollama_message, usage_from
from helena_server.ollama import infer_capabilities
from helena_server.schemas import Message, ToolCall, ToolCallFunction


@pytest.fixture
def client(server_client):
    return server_client


def test_health_reports_ollama(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["ollama_reachable"] is True
    assert body["ollama_version"] == "0.6.0-fake"
    assert body["model_count"] == 2


def test_models_infer_capabilities(client):
    body = client.get("/v1/models").json()
    by_name = {m["name"]: m for m in body["models"]}
    assert by_name["qwen2.5:7b-instruct"]["supports_tools"] is True
    assert by_name["qwen2.5:7b-instruct"]["supports_vision"] is False
    assert by_name["llava:latest"]["supports_vision"] is True
    assert body["default_model"] == "qwen2.5:7b-instruct"


def test_chat_returns_message_and_usage(client, fake_ollama):
    fake_ollama.replies = [{"content": "Hello there."}]
    res = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    body = res.json()
    assert res.status_code == 200
    assert body["message"]["content"] == "Hello there."
    assert body["usage"]["prompt_tokens"] == 120
    assert body["usage"]["completion_tokens"] == 30
    assert body["usage"]["tokens_per_second"] == 30.0


def test_chat_passes_tools_through_and_returns_tool_calls(client, fake_ollama):
    fake_ollama.replies = [{
        "content": "",
        "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}],
    }]
    tools = [{"type": "function", "function": {"name": "read_file", "description": "d", "parameters": {}}}]
    body = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "read a.py"}], "tools": tools}).json()

    assert body["message"]["tool_calls"][0]["function"]["name"] == "read_file"
    assert body["message"]["tool_calls"][0]["function"]["arguments"] == {"path": "a.py"}
    # The server forwards tools verbatim — it never executes anything itself.
    assert fake_ollama.calls[-1]["tools"] == tools


def test_chat_stream_emits_tokens_then_done(client, fake_ollama):
    fake_ollama.replies = [{"content": "streamed reply"}]
    with client.stream("POST", "/v1/chat/stream", json={"messages": [{"role": "user", "content": "go"}]}) as res:
        events = [json.loads(line[5:]) for line in res.iter_lines() if line.startswith("data:")]

    assert [e["type"] for e in events][-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "token") == "streamed reply"
    assert events[-1]["usage"]["completion_tokens"] == 30


def test_chat_stream_emits_tool_calls_event(client, fake_ollama):
    fake_ollama.replies = [{
        "content": "",
        "tool_calls": [{"function": {"name": "run_command", "arguments": {"command": "ls"}}}],
    }]
    with client.stream("POST", "/v1/chat/stream", json={"messages": [{"role": "user", "content": "list"}]}) as res:
        events = [json.loads(line[5:]) for line in res.iter_lines() if line.startswith("data:")]

    tool_events = [e for e in events if e["type"] == "tool_calls"]
    assert tool_events and tool_events[0]["tool_calls"][0]["function"]["name"] == "run_command"


def test_sessions_round_trip(client, fake_ollama):
    created = client.post("/v1/sessions", json={"title": "demo"}).json()
    sid = created["id"]

    fake_ollama.replies = [{"content": "first"}, {"content": "second"}]
    client.post("/v1/chat", json={"messages": [{"role": "user", "content": "one"}], "session_id": sid})
    client.post("/v1/chat", json={"messages": [{"role": "user", "content": "two"}], "session_id": sid})

    detail = client.get(f"/v1/sessions/{sid}").json()
    assert [m["content"] for m in detail["messages"]] == ["one", "first", "two", "second"]
    # The second request carries the stored history upstream: the first
    # exchange plus the new user message (its answer is appended afterwards).
    assert [m["content"] for m in fake_ollama.calls[-1]["messages"]] == ["one", "first", "two"]

    assert client.delete(f"/v1/sessions/{sid}").json() == {"deleted": sid}
    assert client.get(f"/v1/sessions/{sid}").status_code == 404


def test_chat_with_unknown_session_is_404(client):
    res = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}], "session_id": "nope"})
    assert res.status_code == 404


def test_vision_upload_encodes_image(client, fake_ollama):
    fake_ollama.replies = [{"content": "a red square"}]
    res = client.post(
        "/v1/vision/upload",
        files={"file": ("shot.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        data={"prompt": "what is this?"},
    )
    assert res.status_code == 200
    assert res.json()["message"]["content"] == "a red square"
    sent = fake_ollama.calls[-1]["messages"][-1]
    assert sent["images"] and isinstance(sent["images"][0], str)


def test_embeddings(client):
    body = client.post("/v1/embeddings", json={"input": ["a", "b"]}).json()
    assert body["dimensions"] == 3
    assert len(body["embeddings"]) == 2


def test_model_pull_streams_progress(client):
    with client.stream("POST", "/v1/models/pull", json={"name": "llama3.1"}) as res:
        events = [json.loads(line[5:]) for line in res.iter_lines() if line.startswith("data:")]
    assert events[1]["percent"] == 50.0
    assert events[-1]["type"] == "done"


def test_ollama_failure_becomes_502(client, fake_ollama):
    from helena_server.ollama import OllamaError

    async def boom(**kwargs):
        raise OllamaError("Could not reach Ollama at http://fake")

    fake_ollama.chat = boom
    res = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert res.status_code == 502
    assert "Could not reach Ollama" in res.json()["detail"]


def test_auth_required_when_token_set(client, server_settings):
    server_settings.api_token = "secret"
    try:
        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models", headers={"Authorization": "Bearer secret"}).status_code == 200
        # /health stays open so a client can diagnose without credentials.
        assert client.get("/health").status_code == 200
    finally:
        server_settings.api_token = ""


# --- conversion helpers -----------------------------------------------------


def test_to_ollama_message_parses_string_arguments():
    msg = Message(
        role="assistant",
        tool_calls=[ToolCall(function=ToolCallFunction(name="t", arguments='{"a": 1}'))],
    )
    assert to_ollama_message(msg)["tool_calls"][0]["function"]["arguments"] == {"a": 1}


def test_tool_message_carries_every_naming_convention():
    out = to_ollama_message(Message(role="tool", content="ok", name="read_file", tool_call_id="c1"))
    assert out["tool_name"] == "read_file" and out["name"] == "read_file" and out["tool_call_id"] == "c1"


def test_from_ollama_message_assigns_call_ids():
    msg = from_ollama_message({
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "a", "arguments": {}}}, {"function": {"name": "b", "arguments": {}}}],
    })
    assert [c.id for c in msg.tool_calls] == ["call_0", "call_1"]


def test_usage_from_handles_missing_fields():
    usage = usage_from({})
    assert usage.total_tokens is None and usage.tokens_per_second is None


@pytest.mark.parametrize(
    "name,vision,tools",
    [
        ("llava:13b", True, False),
        ("qwen2.5:7b-instruct", False, True),
        ("llama3.2-vision:11b", True, True),
        ("nomic-embed-text", False, False),
    ],
)
def test_infer_capabilities(name, vision, tools):
    assert infer_capabilities(name) == (vision, tools)
