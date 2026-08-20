"""End-to-end agent tests.

These drive the *real* harness client over ASGI into the *real* FastAPI app,
with only Ollama itself faked. So a passing test here means the whole chain
works: tool schemas out, tool calls back, permission gate, execution, results
fed to the next turn.
"""

from __future__ import annotations

import httpx
import pytest

from helena_harness.agent import Agent
from helena_harness.client import ServerClient
from helena_harness.permissions import PermissionEngine
from helena_harness.tools import build_tools
from helena_harness.tools.base import ToolContext
from helena_harness.ui import SilentUI


def tool_call(name: str, **arguments):
    return {"function": {"name": name, "arguments": arguments}}


@pytest.fixture
async def make_agent(asgi_app, fake_ollama, harness_config):
    """Build an agent wired to the in-process server over real HTTP semantics."""
    clients: list[ServerClient] = []

    def factory(*, tools=None, mode=None, ask=None):
        if mode:
            harness_config.mode = mode
        client = ServerClient("http://server", transport=httpx.ASGITransport(app=asgi_app))
        clients.append(client)
        ctx = ToolContext(
            workspace=harness_config.workspace,
            config=harness_config,
            client=client,
            permissions=PermissionEngine(harness_config, ask=ask),
            ui=SilentUI(),
        )
        selected = build_tools(ctx)
        if tools is not None:
            selected = [t for t in selected if t.name in tools]
        return Agent(ctx=ctx, tools=selected, label="HELENA")

    yield factory
    for client in clients:
        await client.aclose()


async def test_plain_reply_without_tools(make_agent, fake_ollama):
    fake_ollama.replies = [{"content": "Nothing to do here."}]
    agent = make_agent()
    result = await agent.send("hello")

    assert result.text == "Nothing to do here."
    assert result.tool_calls == 0
    assert result.stopped == "complete"
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


async def test_tool_call_executes_and_feeds_the_result_back(make_agent, fake_ollama, workspace):
    (workspace / "notes.txt").write_text("the answer is 42\n")
    fake_ollama.replies = [
        {"content": "", "tool_calls": [tool_call("read_file", path="notes.txt")]},
        {"content": "The file says the answer is 42."},
    ]
    agent = make_agent()
    result = await agent.send("what's in notes.txt?")

    assert result.tool_calls == 1
    assert result.text == "The file says the answer is 42."
    roles = [m["role"] for m in agent.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]

    tool_message = agent.messages[2]
    assert tool_message["name"] == "read_file"
    assert "the answer is 42" in tool_message["content"]
    # The second model call saw the real tool output.
    assert any("the answer is 42" in str(m.get("content")) for m in fake_ollama.calls[-1]["messages"])


async def test_tools_are_advertised_to_the_model(make_agent, fake_ollama):
    fake_ollama.replies = [{"content": "ok"}]
    agent = make_agent()
    await agent.send("hi")

    names = {t["function"]["name"] for t in fake_ollama.calls[-1]["tools"]}
    assert {"read_file", "edit_file", "run_command", "web_search", "spawn_agent"} <= names
    for spec in fake_ollama.calls[-1]["tools"]:
        assert spec["function"]["description"].strip()
        assert spec["function"]["parameters"]["type"] == "object"


async def test_multi_step_work_runs_in_one_turn(make_agent, fake_ollama, workspace):
    fake_ollama.replies = [
        {"content": "", "tool_calls": [tool_call("write_file", path="app.py", content="x = 1\n")]},
        {"content": "", "tool_calls": [tool_call("run_command", command="cat app.py")]},
        {"content": "Wrote app.py and confirmed its contents."},
    ]
    agent = make_agent()
    result = await agent.send("create app.py with x = 1, then show me")

    assert result.tool_calls == 2
    assert result.iterations == 3
    assert (workspace / "app.py").read_text() == "x = 1\n"


async def test_denied_tool_call_is_reported_not_executed(make_agent, fake_ollama, workspace):
    async def refuse(_request, _result):
        return "no"

    fake_ollama.replies = [
        {"content": "", "tool_calls": [tool_call("write_file", path="secret.txt", content="oops")]},
        {"content": "Understood — I won't write that."},
    ]
    agent = make_agent(mode="ask", ask=refuse)
    result = await agent.send("write secret.txt")

    assert not (workspace / "secret.txt").exists()
    assert "Not run" in agent.messages[2]["content"]
    assert "Do not retry" in agent.messages[2]["content"]
    assert result.text == "Understood — I won't write that."


async def test_plan_mode_blocks_writes_without_asking(make_agent, fake_ollama, workspace):
    fake_ollama.replies = [
        {"content": "", "tool_calls": [tool_call("write_file", path="x.txt", content="nope")]},
        {"content": "I can't write in plan mode; here's what I'd change."},
    ]
    agent = make_agent(mode="plan")
    await agent.send("write x.txt")

    assert not (workspace / "x.txt").exists()
    assert "read-only" in agent.messages[2]["content"].lower()


async def test_tool_error_is_returned_to_the_model(make_agent, fake_ollama):
    fake_ollama.replies = [
        {"content": "", "tool_calls": [tool_call("read_file", path="missing.txt")]},
        {"content": "That file doesn't exist."},
    ]
    agent = make_agent()
    await agent.send("read missing.txt")

    assert agent.messages[2]["content"].startswith("Error: No such file")


async def test_unknown_tool_name_lists_the_real_ones(make_agent, fake_ollama):
    fake_ollama.replies = [
        {"content": "", "tool_calls": [tool_call("make_coffee", strength="strong")]},
        {"content": "No such tool, sorry."},
    ]
    agent = make_agent()
    await agent.send("make coffee")

    assert "no tool named 'make_coffee'" in agent.messages[2]["content"]
    assert "read_file" in agent.messages[2]["content"]


async def test_max_iterations_stops_a_runaway_loop(make_agent, fake_ollama, harness_config):
    harness_config.max_iterations = 3
    fake_ollama.replies = [
        {"content": "", "tool_calls": [tool_call("get_time")]} for _ in range(10)
    ]
    agent = make_agent()
    result = await agent.send("loop forever")

    assert result.stopped == "max_iterations"
    assert result.iterations == 3


async def test_inline_json_tool_call_is_recovered(make_agent, fake_ollama, workspace):
    """Small local models often emit a tool call as plain text."""
    (workspace / "a.txt").write_text("recovered\n")
    fake_ollama.replies = [
        {"content": '```json\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```'},
        {"content": "Got it."},
    ]
    agent = make_agent()
    result = await agent.send("read a.txt")

    assert result.tool_calls == 1
    assert "recovered" in agent.messages[2]["content"]


async def test_system_prompt_discourages_unnecessary_tool_use(make_agent, fake_ollama):
    fake_ollama.replies = [{"content": "ok"}]
    agent = make_agent()
    await agent.send("hi")

    system = fake_ollama.calls[-1]["messages"][0]["content"]
    assert "need no tool at all" in system


async def test_inline_json_that_is_not_a_tool_call_is_left_alone(make_agent, fake_ollama):
    fake_ollama.replies = [{"content": 'Here is some JSON: {"name": "Ada", "role": "engineer"}'}]
    agent = make_agent()
    result = await agent.send("show me json")

    assert result.tool_calls == 0
    assert result.text.startswith("Here is some JSON")


async def test_inline_json_with_wrong_arguments_is_rejected(make_agent, fake_ollama, workspace):
    """Reproduces the real failure this guards against: a model narrates a plausible-looking
    call to a real tool, but with an argument that tool doesn't actually accept (here, `cmd`
    on get_weather, which only ever takes `city`). Recovering that anyway would silently run
    the tool with an argument it ignores — better to recognize it as not a real call at all.
    """
    fake_ollama.replies = [
        {"content": 'Sure, running it now:\n`get_weather {"cmd": "cd app && npm start"}`'},
    ]
    agent = make_agent()
    result = await agent.send("run the dev server in the background")

    assert result.tool_calls == 0
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


async def test_inline_json_with_unknown_tool_name_is_rejected(make_agent, fake_ollama):
    """The other half of the same failure: a plausible-sounding tool name that was never
    actually registered (get_stock_price vs. the real get_stock) should never be recovered.
    """
    fake_ollama.replies = [
        {"content": 'get_stock_price --subprocess {"name": "get_stock_price", "arguments": {"cmd": "npm start"}}'},
    ]
    agent = make_agent()
    result = await agent.send("run it")

    assert result.tool_calls == 0


async def test_system_prompt_lists_real_tool_names(make_agent, fake_ollama):
    fake_ollama.replies = [{"content": "ok"}]
    agent = make_agent()
    await agent.send("hi")

    system = fake_ollama.calls[-1]["messages"][0]["content"]
    assert "get_stock" in system
    assert "run_command" in system


async def test_independent_read_only_calls_run_concurrently(make_agent, fake_ollama, workspace):
    """Multiple independent reads in one turn shouldn't be forced through one at a time —
    this is the concrete efficiency change: they run via asyncio.gather instead of a loop.
    Ordering in the final message list must still match the order the model asked for them,
    regardless of which one actually finished first.
    """
    (workspace / "a.txt").write_text("A\n")
    (workspace / "b.txt").write_text("B\n")
    fake_ollama.replies = [
        {"content": "", "tool_calls": [
            tool_call("read_file", path="a.txt"),
            tool_call("read_file", path="b.txt"),
        ]},
        {"content": "Read both."},
    ]
    agent = make_agent()
    result = await agent.send("read a.txt and b.txt")

    assert result.tool_calls == 2
    tool_messages = [m for m in agent.messages if m["role"] == "tool"]
    assert len(tool_messages) == 2
    assert "A" in tool_messages[0]["content"]
    assert "B" in tool_messages[1]["content"]


async def test_write_calls_stay_sequential_even_when_batched(make_agent, fake_ollama, workspace):
    """Writes must not be routed through the parallel path — order and one-at-a-time
    permission handling both depend on staying sequential for anything not read-only.
    """
    fake_ollama.replies = [
        {"content": "", "tool_calls": [
            tool_call("write_file", path="one.txt", content="1\n"),
            tool_call("write_file", path="two.txt", content="2\n"),
        ]},
        {"content": "Wrote both."},
    ]
    agent = make_agent(mode="auto")
    await agent.send("write both files")

    assert (workspace / "one.txt").read_text() == "1\n"
    assert (workspace / "two.txt").read_text() == "2\n"


async def test_subagent_reports_back_to_the_parent(make_agent, fake_ollama, workspace):
    (workspace / "auth.py").write_text("def login():\n    return True\n")
    fake_ollama.replies = [
        {"content": "", "tool_calls": [tool_call(
            "spawn_agent", agent_type="explorer", task="find where login is defined"
        )]},
        # --- the subagent's own conversation ---
        {"content": "", "tool_calls": [tool_call("search_text", pattern="def login")]},
        {"content": "login is defined at auth.py:1."},
        # --- back in the parent ---
        {"content": "The explorer found it: auth.py:1."},
    ]
    agent = make_agent()
    result = await agent.send("where is login defined?")

    assert "auth.py:1" in result.text
    tool_result = agent.messages[2]["content"]
    assert "Report from the explorer subagent" in tool_result
    assert "auth.py:1" in tool_result
    # The subagent only ever saw its own restricted tool set.
    subagent_call = fake_ollama.calls[1]
    offered = {t["function"]["name"] for t in subagent_call["tools"]}
    assert offered == {"read_file", "list_dir", "find_files", "search_text"}


async def test_subagent_depth_is_capped(make_agent, fake_ollama, harness_config):
    harness_config.subagent_max_depth = 0
    fake_ollama.replies = [
        {"content": "", "tool_calls": [tool_call("spawn_agent", agent_type="explorer", task="dig")]},
        {"content": "Doing it myself instead."},
    ]
    agent = make_agent()
    await agent.send("delegate this")

    assert "nesting limit" in agent.messages[2]["content"]


async def test_independent_spawn_agent_calls_run_concurrently(make_agent, fake_ollama, workspace):
    """Two independent subagent delegations in one turn should both complete
    and land back correctly — the actual mechanism behind "build the frontend
    and backend at the same time" being faster than delegating one at a time.
    Written to not assume which of the two consumes which queued reply first,
    since that's an implementation detail of how asyncio happens to interleave
    them, not something the test should be fragile to.
    """
    fake_ollama.replies = [
        {"content": "", "tool_calls": [
            tool_call("spawn_agent", agent_type="explorer", task="find the auth module"),
            tool_call("spawn_agent", agent_type="researcher", task="look up fastapi cors setup"),
        ]},
        {"content": "Explorer done: auth.py"},
        {"content": "Researcher done: use CORSMiddleware"},
        {"content": "Both finished."},
    ]
    agent = make_agent()
    result = await agent.send("find auth and research CORS setup, in parallel")

    assert result.text == "Both finished."
    tool_messages = [m for m in agent.messages if m["role"] == "tool"]
    assert len(tool_messages) == 2
    combined = " ".join(m["content"] for m in tool_messages).lower()
    assert "explorer" in combined and "researcher" in combined
    assert "auth.py" in combined
    assert "corsmiddleware" in combined


async def test_concurrent_permission_prompts_are_serialized_not_dropped(make_agent, fake_ollama, workspace):
    """Two subagents that both need an interactive approval at roughly the same
    moment must both actually get asked — the ask_lock exists specifically so
    concurrent prompts queue instead of one silently winning a race.
    """
    prompts_seen: list[str] = []

    async def ask(req, result):
        prompts_seen.append(req.tool)
        return "once"

    agent = make_agent(mode="ask", ask=ask)
    fake_ollama.replies = [
        {"content": "", "tool_calls": [
            tool_call("spawn_agent", agent_type="coder", task="write hello.txt saying hi"),
            tool_call("spawn_agent", agent_type="coder", task="write world.txt saying world"),
        ]},
        {"content": "", "tool_calls": [tool_call("write_file", path="hello.txt", content="hi")]},
        {"content": "Wrote hello.txt."},
        {"content": "", "tool_calls": [tool_call("write_file", path="world.txt", content="world")]},
        {"content": "Wrote world.txt."},
        {"content": "Both done."},
    ]
    result = await agent.send("write two files, delegated")

    assert result.text == "Both done."
    assert prompts_seen.count("write_file") == 2
    assert (workspace / "hello.txt").read_text() == "hi"
    assert (workspace / "world.txt").read_text() == "world"


async def test_history_trimming_never_orphans_a_tool_result(make_agent, harness_config):
    agent = make_agent()
    harness_config.history_messages = 4
    agent.messages = [
        {"role": "user", "content": "the original request"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": {}}}]},
        {"role": "tool", "content": "result", "name": "x", "tool_call_id": "c1"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "another"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "function": {"name": "y", "arguments": {}}}]},
        {"role": "tool", "content": "result 2", "name": "y", "tool_call_id": "c2"},
    ]
    window = agent.trimmed_history()

    assert window[0]["role"] != "tool"
    assert "the original request" in window[0]["content"]


async def test_compact_drops_tool_traffic(make_agent):
    agent = make_agent()
    agent.messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "function": {"name": "x", "arguments": {}}}]},
        {"role": "tool", "content": "huge output", "name": "x", "tool_call_id": "c"},
        {"role": "assistant", "content": "finished"},
    ]
    removed = agent.compact()

    assert removed == 2
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


async def test_server_error_ends_the_turn_cleanly(make_agent, fake_ollama):
    from helena_server.ollama import OllamaError

    async def boom(**kwargs):
        raise OllamaError("Ollama fell over")
        yield  # pragma: no cover - makes this an async generator

    fake_ollama.chat_stream = boom
    agent = make_agent()
    result = await agent.send("hello")

    assert result.stopped == "error"
    assert "Ollama fell over" in result.error


async def test_non_streaming_mode_still_works(make_agent, fake_ollama, harness_config):
    harness_config.stream = False
    fake_ollama.replies = [{"content": "buffered reply"}]
    agent = make_agent()
    result = await agent.send("hi")

    assert result.text == "buffered reply"


async def test_usage_totals_accumulate_across_turns(make_agent, fake_ollama):
    fake_ollama.replies = [{"content": "one"}, {"content": "two"}]
    agent = make_agent()
    await agent.send("first")
    await agent.send("second")

    assert agent.totals["turns"] == 2
    assert agent.totals["prompt_tokens"] == 240   # 120 per call from the fake
    assert agent.totals["completion_tokens"] == 60


async def test_system_prompt_carries_environment_and_mode(make_agent, fake_ollama, harness_config):
    harness_config.mode = "plan"
    (harness_config.workspace / "HELENA.md").write_text("# Project\n\nAlways use pnpm.\n")
    fake_ollama.replies = [{"content": "ok"}]
    agent = make_agent()
    await agent.send("hi")

    system = fake_ollama.calls[-1]["messages"][0]
    assert system["role"] == "system"
    assert str(harness_config.workspace) in system["content"]
    assert "READ-ONLY" in system["content"]
    assert "Always use pnpm." in system["content"]
