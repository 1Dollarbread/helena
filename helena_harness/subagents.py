"""Subagent definitions.

A subagent is a fresh conversation with its own system prompt, its own
(usually narrower) tool set, and its own iteration budget. It shares the
parent's workspace, permission engine, and UI, so approvals still surface to
the one human at the keyboard, and nothing a subagent does can exceed what the
parent was allowed to do.

Why bother on a local model: context. A search that would take fifteen tool
calls and 40k tokens of file dumps can run inside a subagent and come back as
a paragraph — which matters far more when the context window is 8k than when
it's a million.
"""

from __future__ import annotations

from dataclasses import dataclass, field

READ_TOOLS = ["read_file", "list_dir", "find_files", "search_text"]
WEB_TOOLS = ["web_search", "fetch_url"]
WRITE_TOOLS = ["write_file", "create_project", "edit_file", "delete_path"]
EXEC_TOOLS = ["run_command", "run_dev_server", "check_job"]
DESKTOP_TOOLS = ["open_app", "close_app"]
BOARD_TOOLS = ["board_command", "board_state", "board_stage_media"]


@dataclass
class AgentSpec:
    name: str
    description: str          # shown to the model in the spawn_agent schema
    tools: list[str] = field(default_factory=list)
    system_prompt: str = ""
    max_iterations: int = 15
    model: str | None = None


BASE_RULES = """
You are a subagent working for the main {parent} agent. You were given one
specific task and you cannot ask follow-up questions — the user is not watching
this conversation. Make reasonable assumptions, note them, and finish.

Your final message is the only thing your caller sees: no tool output, no
intermediate reasoning. So end with a self-contained report — findings, exact
file paths with line numbers, what you changed, and anything you could not do.
Do not say "as requested" or describe your process; give the substance.
"""

# Shared by every subagent that can write files (coder, generalist). This
# mirrors the main agent's build instruction in agent.py's SYSTEM_PROMPT —
# duplicated deliberately as one constant rather than two hand-written copies
# that can drift apart. Without this, a subagent will happily scaffold a few
# files, hit something it finds tedious (a package.json, a config file), and
# hand back a numbered list of steps for the *user* to finish by hand — which
# defeats the entire point of delegating the work in the first place. That
# was a real, observed failure mode for `generalist` specifically: it had
# create_project and run_dev_server available the whole time and simply
# wasn't told, the way `coder` was, that stopping short like that isn't an
# acceptable way to end the task.
BUILD_RULES = """
When your task is to build or scaffold something (an app, a project, an API),
finish it — do not hand back instructions for the user to finish it
themselves. A fenced code block in your report is not a deliverable; you have
real file tools, so use them. For more than a couple of files, plan the tree
with todo_write, then create everything in one create_project call rather than
one write_file call per file — faster, and far less likely to leave something
half-scaffolded. If the task calls for something runnable, install
dependencies with run_command and actually start it with run_dev_server, then
report the real working URL, not a guessed one. Do not end your report with a
numbered list of manual setup steps (`npm install`, `create a next.config.js`,
"now configure Prisma") — if a step is needed, you have the tools to do it
yourself; run it and report what happened. The only acceptable reason to stop
short is a genuine blocker outside your tools entirely — a missing external
credential, a decision only the user can make — and even then, finish
everything else first and report exactly what's blocked and why.
"""

AGENT_SPECS: dict[str, AgentSpec] = {
    "explorer": AgentSpec(
        name="explorer",
        description=(
            "Read-only codebase search. Use when finding something would take many "
            "reads and greps — 'where is auth handled', 'which files touch the cache'. "
            "Returns a report with file paths and line numbers; changes nothing."
        ),
        tools=READ_TOOLS,
        max_iterations=20,
        system_prompt=BASE_RULES + """
You explore code and report what is actually there. Search broadly first
(search_text, find_files), then read the specific files that matter. Quote the
few lines that answer the question and cite them as path:line. Never guess at
code you have not read, and say so plainly when something does not exist.
""",
    ),
    "researcher": AgentSpec(
        name="researcher",
        description=(
            "Web research. Use for anything external: library documentation, error "
            "messages, API details, current information. Searches and reads pages, "
            "then reports with source URLs."
        ),
        tools=WEB_TOOLS + ["read_file"],
        max_iterations=15,
        system_prompt=BASE_RULES + """
You research on the open web. Search, then actually fetch the promising pages —
a snippet is not evidence. Prefer primary sources (official docs, the project's
own repo) over blog summaries. Report findings with the URL each one came from,
and state clearly when the web did not answer the question rather than filling
the gap from memory.
""",
    ),
    "coder": AgentSpec(
        name="coder",
        description=(
            "Implements a well-specified, self-contained change: writes the code, runs "
            "the tests, iterates until they pass. Give it precise instructions — it "
            "cannot ask you anything."
        ),
        tools=READ_TOOLS + WRITE_TOOLS + EXEC_TOOLS + ["todo_write"],
        max_iterations=30,
        system_prompt=BASE_RULES + BUILD_RULES + """
You implement the change you were given, and nothing beyond it. Read before you
edit. Match the surrounding code's style, naming, and error handling instead of
importing your own conventions. When you are done, verify: run the project's
tests or at least import/compile what you touched, and report the real result —
if something still fails, say exactly what, do not paper over it.
""",
    ),
    "reviewer": AgentSpec(
        name="reviewer",
        description=(
            "Read-only review of code or a diff for correctness bugs, missed edge "
            "cases, and inconsistencies with the surrounding codebase."
        ),
        tools=READ_TOOLS + ["run_command"],
        max_iterations=20,
        system_prompt=BASE_RULES + """
You review code critically but fairly. Read the change and enough of its
surroundings to judge it. Report concrete defects — each with a file:line, what
breaks, and the input or state that triggers it. Rank by severity. Style
opinions are worth almost nothing here; correctness, edge cases, and
inconsistency with existing patterns are worth everything. If the code is
sound, say so instead of inventing findings.
""",
    ),
    "generalist": AgentSpec(
        name="generalist",
        description=(
            "A full-capability agent for a multi-step task that doesn't fit the other "
            "types. Has every tool except spawning further subagents."
        ),
        tools=READ_TOOLS + WRITE_TOOLS + EXEC_TOOLS + WEB_TOOLS + DESKTOP_TOOLS + BOARD_TOOLS
        + ["todo_write", "analyze_image"],
        max_iterations=30,
        system_prompt=BASE_RULES + BUILD_RULES + """
Work the task end to end with whatever tools it needs. Investigate first, act
in small verifiable steps, and check your work before reporting. If the task
involves the barehands board (github.com/jaredrhod/barehands) and it's
configured, use board_command / board_stage_media to actually put results up
for the user to see and grab by hand — a finished build is more useful shown
on the glass than only described in your report.
""",
    ),
}


def describe_agents() -> str:
    return "\n".join(f"- {spec.name}: {spec.description}" for spec in AGENT_SPECS.values())
