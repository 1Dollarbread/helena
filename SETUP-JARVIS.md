# JARVIS Features — Setup Guide

Covers two additions to HELENA:

1. **Web HUD** (`helena-web`) — a browser-based chat client with the same
   real tool execution and permission prompts as the terminal, styled as a
   dark HUD.
2. **Wake-on-clap** — two claps triggers a spoken debrief of the project's
   current state.

Follow this top to bottom. Every command is meant to be copy-pasted exactly
as written, in order.

---

## 0. Where these files go

If you already applied the earlier patch, skip to step 1 — these files
already exist in your clone:

```
helena_harness/webui.py
helena_harness/web_static/index.html
extras/wakeup/clap_listener.py
extras/wakeup/debrief.py
extras/wakeup/startup.sh
extras/wakeup/com.helena.wake.plist
```

plus a small addition to `pyproject.toml`'s `[project.scripts]` section
(adds a `helena-web` command) and `[tool.setuptools.package-data]` (so the
HTML file gets included).

**If `helena_harness/webui.py` is giving you the "Cannot call send once a
close message has been sent" error** — replace just that one file:

1. Download `webui.py` from the file list above this message.
2. In Finder, go to `helena/helena_harness/` in your cloned repo.
3. Drag the downloaded `webui.py` in, overwrite when prompted.

That's the only file that needed fixing. Everything else below is unchanged.

---

## 1. One-time environment setup

Skip any step you've already done (e.g. if `.venv` already exists).

```
cd ~/helena
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,voice]"
```

If `python3 -m venv .venv` fails, run `python3 --version` — you need 3.10 or
newer.

Every time you come back to work on this in a new terminal tab, you need to
run `source .venv/bin/activate` again before any `helena` command will work.

---

## 2. Set up the model server (skip if you've already done this before)

```
ollama pull qwen2.5:7b-instruct
./start.sh
```

Confirm it's healthy:

```
curl -s localhost:8080/health
```

---

## 3. Test the Web HUD

```
helena-web -C .
```

You'll see:

```
HELENA web HUD → http://127.0.0.1:8765  (workspace: /Users/you/helena)
```

Open that URL in your browser. Type a message and hit send or Enter.

**What to check:**
- Text streams in as HELENA replies.
- Ask it to do something that touches a file (e.g. "create a test.txt file
  with the word hello in it") — a permission card should pop up in the
  browser with four buttons: Yes once / Always allow / Allow this session /
  No.
- Click one — the action should complete and you'll see a tool line appear
  in the chat.
- The ring in the top-left should glow amber while it's "thinking" and cyan
  while it's replying.

**If something goes wrong:** watch the terminal window running `helena-web`,
not just the browser — real errors print there as a full Python traceback
now. If you hit one, copy the whole traceback back to me.

To stop it: `Ctrl-C` in that terminal.

**Options:**

```
helena-web -C ~/some/other/project    # point it at a different project
helena-web --port 9000                # different port
helena-web --mode auto                # skip permission prompts (careful)
```

---

## 4. Test wake-on-clap

### 4a. Run the listener by hand first

```
python extras/wakeup/clap_listener.py --workspace "$(pwd)"
```

Clap twice, about half a second apart, near your Mac's mic. You should see:

```
[wake] listening for two claps · threshold=0.35 window=1.2s
[wake] clap 1…
[wake] clap 2 — waking HELENA
[debrief] asking HELENA…
```

A few seconds later it should speak a summary out loud — macOS's built-in
voice by default (`say`), or your ElevenLabs voice if you already have
`ELEVENLABS_API_KEY` set up from the main README's Voice section.

**Tuning, if needed:**
- **Doesn't trigger at all** — check System Settings → Sound → Input and
  confirm the level meter visibly jumps when you clap. If it does but
  nothing happens, the mic HELENA is picking up may not be the default
  input device.
- **Triggers on everything** (typing, talking, background noise) — open
  `extras/wakeup/clap_listener.py` in any text editor, find the line
  `THRESHOLD = 0.35` near the top, and raise it (try `0.5`, then `0.6`) in
  small steps, saving and re-running the test each time.

Press `Ctrl-C` to stop it once it's working.

### 4b. Wire it to login

```
cp extras/wakeup/com.helena.wake.plist ~/Library/LaunchAgents/com.helena.wake.plist
sed -i '' "s|REPLACE_WITH_ABSOLUTE_PATH|$(pwd)|" ~/Library/LaunchAgents/com.helena.wake.plist
```

Open `extras/wakeup/startup.sh` in any text editor (TextEdit is fine) and
check the very first real line:

```
HELENA_REPO="${HELENA_REPO:-$HOME/RogerCraig}"
```

Change `$HOME/RogerCraig` to match wherever your clone actually lives — run
`pwd` in your terminal (while inside the repo) to see the exact path, e.g.:

```
HELENA_REPO="${HELENA_REPO:-/Users/waltj/helena}"
```

Save it, then load the LaunchAgent:

```
launchctl load ~/Library/LaunchAgents/com.helena.wake.plist
```

### 4c. Test the login path without logging out

```
launchctl start com.helena.wake
```

A new Terminal window should open by itself, `cd` into the repo, activate
the venv, and start listening. Clap twice to confirm it works end to end,
the same as step 4a.

### Troubleshooting

**Nothing happens when you `launchctl start` it:**

```
cat /tmp/helena-wake.log
```

That'll show whatever error stopped it — almost always a wrong path in
either the plist or `startup.sh`.

**Terminal opens and immediately closes:** same cause as above — the path
HELENA_REPO points to doesn't match your real clone location.

**It hears the claps but the debrief errors or comes back empty:** the
model server isn't running — repeat step 2 (`./start.sh`) in a normal
terminal first.

### Turning it off

```
launchctl unload ~/Library/LaunchAgents/com.helena.wake.plist
rm ~/Library/LaunchAgents/com.helena.wake.plist
```

---

## 5. Once everything above works: commit and push

Standard flow, nothing special about these files:

```
cd ~/helena
git add -A
git commit -m "Add web HUD and wake-on-clap"
git push
```
