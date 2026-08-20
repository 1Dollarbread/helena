# H.E.L.E.N.A

### Highly Efficient Logic Engine Network Assistant

A local-first agentic harness, in two parts:

1. **`helena_server`** — a FastAPI service that actually runs the models, through
   [Ollama](https://ollama.com). Chat, streaming, tool calling, vision, embeddings,
   and persisted sessions, over a clean HTTP API.
2. **`helena_harness`** — a full terminal agent that talks to that server. It reads
   and edits files, runs commands, searches the web, looks at images, delegates to
   subagents, and asks your permission before it changes anything.

Nothing leaves your machine. No API keys, no per-token cost, no third party — the
only outbound traffic is what you explicitly ask for (a web search, a URL fetch,
weather, market data).

```
┌──────────────────┐   HTTP/SSE    ┌──────────────────┐   HTTP    ┌────────┐
│  terminal agent  │ ────────────► │  FastAPI server  │ ────────► │ Ollama │
│  helena_harness  │ ◄──────────── │  helena_server   │ ◄──────── │ models │
└──────────────────┘  tokens +     └──────────────────┘           └────────┘
   tools · permissions   tool calls    sessions · vision
   subagents · files                   embeddings · pulls
```

The split matters: the server never executes a tool. It reports what the model
asked for and hands the decision back to the harness, which is where the
permission system lives. Your models can also move to a beefier machine on the
LAN without the harness noticing — point `HELENA_SERVER_URL` at it.

---

## Quick start

Copy-paste, in order, in your regular terminal (not inside HELENA):

```bash
git clone <this repo> && cd RogerCraig
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

```bash
ollama pull qwen2.5:7b-instruct   # required — a tool-calling model
ollama pull llava                  # optional — only if you want image support
```

```bash
./start.sh
```

```bash
helena
```

That's it — you're now at the `you ›` prompt, talking to HELENA. Everything
below `./start.sh` and `helena` is your **daily** routine once setup is done;
the two blocks above it only need to happen once.

When you're done for the day:

```bash
./stop.sh
```

If `helena` doesn't start after all this, see [Troubleshooting](#troubleshooting).

<details>
<summary>Advanced: running the server yourself, instead of via <code>start.sh</code></summary>

`start.sh` runs Ollama and `helena_server` as background processes for you.
If you'd rather run the server yourself — you want it long-lived, shared with
others on your LAN, or on a different machine than the harness — skip
`start.sh` and do this instead:

```bash
helena-server               # http://127.0.0.1:8080, docs at /docs
```

Then point the harness at it from anywhere (including a different machine):

```bash
HELENA_SERVER_URL=http://<host>:8080 helena
```

`helena` (with no server explicitly running) will also auto-start one for you
if it doesn't find one — `start.sh` and this are two ways to the same place.
</details>

---

## Command reference

Everything in this section is typed **inside HELENA**, at the `you ›` prompt —
not in your regular terminal. `/help` shows this same list live.

**Models**

| command | does |
|---|---|
| `/model` | show the current chat model |
| `/model <name>` | switch chat model, e.g. `/model llama3.1` |
| `/models` | list installed models |
| `/pull <name>` | download a model, e.g. `/pull qwen2.5:7b-instruct` |
| `/doctor` | check server, Ollama, and whether your model supports tool calling |

**Permissions & trust**

| command | does |
|---|---|
| `/mode` | show current permission mode |
| `/mode ask` \| `auto` \| `plan` \| `yolo` | switch mode — see [Permissions](#permissions) |
| `/permissions` | list saved allow/deny rules |
| `/permissions allow <rule>` | add a rule, e.g. `/permissions allow run_command(git:*)` |
| `/workspace` | show whether file tools are confined to this project |
| `/workspace unlock` | let file tools touch anything on this Mac, this session |
| `/workspace lock` | restore the confinement |
| `/trust` | `/mode yolo` + `/workspace unlock` in one shot — no prompts, no confinement |

**Files, search, images**

| command | does |
|---|---|
| `/read <path>` | load a file into context |
| `/search <query>` | web search |
| `/image <path> [question]` | ask a vision model about an image |

**Agents & sessions**

| command | does |
|---|---|
| `/tools` | list every tool the model can call |
| `/agents` | list the available subagents |
| `/agent <name> <task>` | run a subagent directly, e.g. `/agent explorer where is auth handled?` |
| `/session` | show session info |
| `/compact` | summarize the conversation so far to free up context |
| `/clear` | start a new conversation |
| `/cost` | token usage so far |

**Memory & project setup**

| command | does |
|---|---|
| `/memory` | show what's in `HELENA.md` |
| `/remember <fact>` | save a durable fact about you |
| `/reminders` | show reminders you've asked HELENA to set |
| `/init` | explore the project and write a starting `HELENA.md` |

**Other**

| command | does |
|---|---|
| `/help` | show this list, live |
| `/jobs` | list background jobs (including dev servers started with `run_dev_server`) |
| `/cd <path>` | change the working directory |
| `/stream` | toggle streaming output |
| `/exit` | quit |

**Voice** — see [Voice](#voice) for setup

| command | does |
|---|---|
| `/voice` | speak instead of typing — records until you press Enter, transcribes locally |
| `/say <text>` | speak text out loud right now, e.g. `/say testing one two three` |
| `/speak on` \| `off` | auto-speak every reply |
| `/voice-setup` | show exactly what's needed for both of the above |

## Using it

Once you're at the `you ›` prompt, you don't need slash commands for most of
this — just talk to it:

```
you › what does the agent loop in helena_harness do?
you › add a --json flag to the CLI and make the tests pass
you › run the dev server for this project
```

Slash commands are for control-plane stuff — switching models, changing
permission mode, running a subagent directly. A few combined in one line:

```
you › /mode plan
you › where is permission checking done?
you › /mode ask
```

**One-shot mode**, for scripts — this one IS your regular terminal, not HELENA:

```bash
helena -p "run the tests and summarize failures" --mode auto
git diff | helena -p "review this diff" --mode plan
```

## Tools

| tool | permission | what it does |
|---|---|---|
| `read_file` | read | file contents with line numbers, offset/limit for big files |
| `list_dir`, `find_files` | read | directory listing, glob search |
| `search_text` | read | regex content search across the tree (`grep -rn`) |
| `edit_file` | write | exact-string replacement; requires a prior read |
| `write_file`, `delete_path` | write | create/overwrite, remove a file or empty dir |
| `create_project` | write | create many files in one call — the right tool for scaffolding a new project, see below |
| `run_command` | execute | real shell execution, with timeouts and background jobs |
| `run_dev_server` | execute | starts a local dev server and reports the actual URL — see below |
| `check_job` | read | inspect or kill a background command (including a dev server) |
| `web_search` | network | DuckDuckGo results + instant answers |
| `fetch_url` | network | fetch a page and convert it to readable text |
| `analyze_image` | read | ask a multimodal model about an image or screenshot |
| `open_app`, `close_app` | execute | launch/quit a native app, or open a URL (macOS) |
| `todo_write` | — | the visible task list for multi-step work |
| `spawn_agent` | — | delegate to a subagent (below) |
| `get_weather`, `get_stock`, `add_reminder`, `remember`, `get_time` | mixed | the original HELENA assistant features, kept |

### Running a local dev server without guessing a port

`run_dev_server` exists because "run it" turned out to be the single most
common thing this got wrong — the model would either narrate success without
running anything, or start something with `run_command` and then have no real
way to tell you what port it landed on. This tool actually solves that:

* Auto-detects the right start command when you don't give it one — checks
  `package.json` scripts (`dev`, then `start`, then `serve`, using yarn/pnpm
  if a lockfile says to), `manage.py` (Django), a FastAPI/Flask `app.py` /
  `main.py` / `server.py`, or falls back to a static file server if it's just
  HTML.
* Runs it as a real background job (same machinery as `run_command
  background: true`), then watches the log for the URL the server itself
  prints, instead of assuming a port.
* Reports the actual working URL, or — if nothing showed up within
  `wait_seconds` (15s default) — says so plainly and gives you the job ID to
  check on manually with `check_job`, rather than either lying about a port
  or hanging forever.

`spawn_agent` is unrelated to this — it delegates a self-contained task to a
separate conversation, it doesn't keep a process alive. Two different things
that got conflated in practice; the system prompt is now explicit about the
distinction.

### Building a full-stack project, actually

The old failure mode here: ask for a full-stack app, get a wall of code
blocks in the reply and instructions to paste them into files yourself. The
model had file tools the whole time — it just defaulted to *showing* code
instead of *creating* it, especially once a task had enough files that
calling `write_file` over and over stopped feeling natural.

`create_project` is the fix: one call, a list of `{path, content}` pairs,
every file gets created (directories included) in a single tool call instead
of remembering to call `write_file` N separate times. The system prompt now
treats a code block in a reply as "not a deliverable" — if you ask for
something to be built, expect real files, not a copy-paste homework
assignment. Pair it with `run_command` (install dependencies) and
`run_dev_server` (actually start it, with the real URL) and "build me X and
run it" is now one turn's work end to end — no manual steps in between.

If you're on `yolo` mode (or ran `/trust`), none of this pauses for
confirmation either — see [Permissions](#permissions).

## Permissions

Every tool call is classified and checked before it runs. Four modes:

| mode | reads | file edits | commands |
|---|---|---|---|
| `ask` (default) | run | ask | ask |
| `auto` | run | run | ask |
| `plan` | run | **refused** | **refused** |
| `yolo` | run | run | run |

When asked, you get a panel showing exactly what will happen — the command, or a
diff of the edit — and four answers: yes once, yes and always allow this, yes for
this session, or no. "Always" writes a rule to `.helena/settings.json`:

```json
{
  "allow": ["run_command(pytest:*)", "read_file(*)", "write_file(src/**)"],
  "deny": ["run_command(git push:*)"],
  "mode": "auto"
}
```

Rules are `tool(pattern)`; `cmd:*` is a prefix match, `src/**` is a glob, a bare
tool name matches all its calls. Deny beats allow, always. Aliases (`Bash`,
`Read`, `Write`, `Edit`) work if you have muscle memory from elsewhere.

Two rails are not negotiable, in every mode including `yolo`:

* **Some commands are always refused** — `rm -rf /`, `mkfs`, writing to a raw
  block device, fork bombs. Commands are judged per segment with quoted
  strings removed, so `echo "rm -rf /"` and `git commit -m "remove rm -rf /
  from docs"` are correctly left alone.
* **Some are always confirmed** even in `auto` — `git push`, `sudo`, recursive
  deletes, piping a download into a shell, publishing a package.

File tools are also confined to the workspace directory by default. That's a
guard against a wandering model, not a sandbox: `run_command` can already
reach the rest of the filesystem regardless, because a terminal agent that
can't run your build is useless. Three ways to lift the file-tool confinement
specifically, in increasing scope:

* `/workspace unlock` — right now, this session only.
* `HELENA_ALLOW_OUTSIDE_WORKSPACE=true`, or `--allow-outside-workspace` at
  startup — every session.
* `/trust` — the fastest path to "just let it work": sets `yolo` mode *and*
  unlocks the workspace in one command, so nothing prompts and file tools can
  reach anywhere on the machine. The two hard-refused-command rails above
  still apply even here — that check never gets bypassed by any mode. Dial it
  back with `/mode ask` and `/workspace lock`.

If you want real isolation instead of trust, run the whole thing in a
container.

## Subagents

`spawn_agent` runs a task in a *separate* conversation with its own tool set and
returns only a summary. On a local model this is mostly about context: a search
that would take fifteen tool calls and 40k tokens of file dumps comes back as a
paragraph, which matters far more when the window is 8k than when it's a million.

| agent | tools | for |
|---|---|---|
| `explorer` | read-only | "where is X handled", broad code search |
| `researcher` | web | documentation, error messages, current information |
| `coder` | read + write + shell | a scoped, well-specified implementation |
| `reviewer` | read + shell | correctness review of code or a diff |
| `generalist` | everything but nesting | multi-step work that fits nothing else |

Subagents share the parent's permission engine — approvals surface to you, and a
subagent can never do something the parent couldn't. Nesting is capped
(`subagent_max_depth`, default 2).

Independent read-only tool calls in the same turn (a few file reads, a couple
of lookups) run concurrently via `asyncio.gather` rather than one at a time.
**So do independent `spawn_agent` calls** — this is the real speedup for
something like a full-stack build: a `coder` on the backend and a `coder` on
the frontend, dispatched in the same turn, actually run at the same time
rather than one after the other. Two subagents both hitting an interactive
permission prompt at once is handled correctly too — a lock around the
prompt itself serializes just that moment, not the underlying work, so they
queue cleanly instead of corrupting the terminal. Direct writes and direct
shell execution (not spawn_agent) stay strictly sequential, since two of
those really can conflict with each other in ways a subagent boundary
doesn't protect against — two edits to the same file racing, for instance.

Set `subagent_model` (or `HELENA_SUBAGENT_MODEL`) if you want subagents —
often read-heavy, rarely needing your biggest model — to run on something
smaller and faster than the main conversation. This compounds with the
concurrency above: several lighter-weight subagents running at once will
usually still beat one big model working through the same tasks in sequence.

## Fixing hallucinated tool use

If the model ever narrates doing something — "Opening VS Code... Done." — with
no real tool line above it, that's not a UI bug: it means no tool actually ran,
and the model made it up. This can happen with any local model, but is more
likely with smaller or non-tool-calling ones. Three things guard against it:

* The system prompt lists every real tool by name and explicitly forbids
  writing anything that imitates the interface's own tool-status display
  (arrow bullets, backtick pseudo function-call syntax) unless it's backed by
  an actual call that turn.
* The text-recovery fallback (for models that emit a tool call as plain JSON
  instead of using the real protocol) now validates the recovered arguments
  against that tool's actual schema before accepting the match — a call to
  `get_weather` with a `cmd` argument it doesn't define is rejected as not a
  real match, rather than silently executed with an argument the tool ignores.
* The REPL checks at startup whether your selected model is recognized as
  tool-calling capable and warns immediately if not, rather than letting you
  find out by watching it happen.

The most reliable fix, if you hit this, is switching to a model with solid
native tool-calling support (`qwen2.5:7b-instruct` is the default for a
reason) — `/doctor` shows which of your installed models qualify.

## Troubleshooting

**`helena` runs, but it's clearly not this version** (missing commands, old
behavior, `/help` doesn't match this README) — almost always a `PATH`
collision with something else called `helena`, most commonly a leftover
global install of the old JavaScript version. Check which one you're
actually running:

```bash
which helena
python -c "import helena_harness.repl as r; print(r.__file__)"
```

The second command tells you, definitively, which file your Python package
resolves to. If `which helena` points somewhere that isn't inside this
project's `.venv` (e.g. an `nvm`/`node_modules/.bin` path), that's the
problem — something earlier in your `PATH` is shadowing the real command.
Fastest fix if it's the old JS version:

```bash
npm unlink -g helena
```

Or bypass `PATH` entirely and run the module directly, which always works
regardless of what else is installed:

```bash
python -m helena_harness
```

**Edited the code, but `helena` doesn't reflect it** — re-run the install
from inside the correct, current project folder:

```bash
cd path/to/RogerCraig
source .venv/bin/activate
pip install -e ".[dev]"
```

**`/doctor` says the server or Ollama is unreachable** — confirm both are
actually running:

```bash
./start.sh
```

If you're running the server manually instead (see the "Advanced" note near
the top), check it separately:

```bash
curl -s localhost:8080/health
curl -s localhost:11434/api/tags   # Ollama directly
```

**The model narrates doing something it didn't actually do** — see
[Fixing hallucinated tool use](#fixing-hallucinated-tool-use) above; start
with `/doctor` to check whether your model genuinely supports tool calling.

## The server API

Interactive docs at `http://127.0.0.1:8080/docs`.

| endpoint | |
|---|---|
| `GET /health` | server + Ollama status, model count |
| `GET /v1/models` | installed models with inferred tool/vision capability |
| `POST /v1/models/pull` | download a model, SSE progress |
| `POST /v1/chat` | one completion; `stream: true` switches to SSE |
| `POST /v1/chat/stream` | SSE: `token` → `tool_calls` → `done` (with usage) |
| `POST /v1/vision` · `/v1/vision/upload` | images as base64 or multipart upload |
| `POST /v1/embeddings` | vectors from an embedding model |
| `/v1/sessions...` | create, list, read, append, rename, delete (sqlite) |

```bash
curl -sN localhost:8080/v1/chat/stream -H 'content-type: application/json' -d '{
  "messages": [{"role": "user", "content": "explain SSE in one sentence"}]
}'
```

Set `HELENA_API_TOKEN` to require `Authorization: Bearer <token>` on `/v1/*`
(`/health` stays open so a client can still diagnose a bad token). Unset — the
default — means no auth, which is right for a process bound to localhost.

## Voice

Two independent features — you can use either without the other.

**Talking to HELENA (speech-to-text)** is fully local and free: no account, no
API key, nothing leaves your machine. It runs on
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), which isn't
installed by default since it's meaningfully heavier than the rest of this
project.

```bash
pip install -e ".[voice]"
```

```
you › /voice
🎙️  Recording — press Enter when you're done talking.
[you talk, then press Enter]
Transcribing...
you (voice) › what's the weather like today
```

Whatever gets transcribed goes through the exact same path as typed text —
tool use, permission prompts, image-path detection, all of it behave
identically either way. If a request is ambiguous, HELENA asks a clarifying
question the same way it would if you'd typed it; nothing extra to set up for
that specifically, it's just how the agent already behaves.

**HELENA talking back (text-to-speech)** uses
[ElevenLabs](https://elevenlabs.io), and is the one part of this project that
isn't free or local — there's no local TTS yet that sounds like an actual
voice. It needs your own account:

1. Sign up at [elevenlabs.io](https://elevenlabs.io) and open **Settings → API Keys**.
2. `export ELEVENLABS_API_KEY=your-key-here` (put it in your shell profile — `~/.zshrc` or similar — so it persists across sessions).
3. Optional: browse the [voice library](https://elevenlabs.io/voice-library), copy a voice's ID, and `export ELEVENLABS_VOICE_ID=that-id`. Skip this and a stock voice ("Rachel") is used.
4. `/say testing one two three` to confirm it works.
5. `/speak on` to have every reply spoken automatically from then on, or leave it off and just use `/say` on demand.

`/voice-setup` shows this same checklist from inside HELENA, plus whether
each half is currently ready. Playback (`afplay`) is macOS-only right now,
same as the desktop-control tools.

Nothing about the ElevenLabs side touches the rest of HELENA — skip it
entirely and everything else in this README still works exactly as
described, at zero cost.

## Configuration

Everything has a working default. Layers, later wins:

```
~/.helena/settings.json          your defaults
<workspace>/.helena/settings.json  this project
HELENA_* environment variables   this launch
command-line flags               this run
```

`allow`/`deny` lists are merged across layers rather than overwritten, so a
project can add grants without discarding your global ones. See `.env.example`
for the server's variables, and `helena --help` for flags.

`HELENA.md` at the workspace root is loaded into the system prompt every turn —
put project conventions there. `/init` writes one by exploring the project.
(`CLAUDE.md` or `AGENTS.md` are used as fallbacks if you already keep one.)

## Choosing a model

Two levers dominate on local hardware:

* **Tool calling is required.** `qwen2.5`, `qwen3`, `llama3.1`/`3.2`/`3.3`,
  `mistral-nemo`, `gpt-oss`, and `command-r` all support it in Ollama.
  `/doctor` lists which of your installed models qualify. A model without it
  will talk about using tools instead of using them — the harness recovers
  tool calls emitted as plain JSON text, which papers over the gap, but only
  partly.
* **Context window is memory.** Ollama sizes its KV cache off `num_ctx`
  regardless of how short your conversation is, and agentic loops carry real
  tool output. 8192 is the default here; drop to 4096 if RAM is tight, raise it
  if you have headroom. Quantized tags (`:7b-instruct-q4_K_M`) cut memory
  roughly threefold for a modest quality cost.

## Development

```bash
pytest                    # 174 tests (+1 that skips without the optional voice extra), no Ollama required
python -m pyflakes helena_server helena_harness
```

The tests fake Ollama and nothing else: the agent-loop suite drives the real
harness client over ASGI into the real FastAPI app, so a green run means the
whole chain works — tool schemas out, tool calls back, permission gate,
execution, results fed to the next turn.

```
helena_server/     app.py routes · ollama.py client · store.py sqlite · schemas.py
helena_harness/    agent.py loop · repl.py terminal · permissions.py · tools/
tests/             server, permissions, file tools, shell/web, agent loop, config
```

## What happened to the JavaScript version

This replaces it. The original HELENA (Node + Ollama, `helena.js` and friends) was
a conversational assistant with weather, stocks, reminders, and macOS app control.
Those features are still here as tools; what's new is that it can now actually
work on your code — read, edit, run, verify — with a permission model that makes
that safe to leave running. The old files are in git history at commit `446d81c`.

## License

MIT
