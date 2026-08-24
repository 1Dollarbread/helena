# Wake-on-clap

Two claps wakes HELENA and speaks a debrief of the project: git state, recent
commits, TODOs, and a suggestion for what to work on next.

## 1. Install the voice extra

Clap detection reuses the same `sounddevice` + `numpy` dependency `/voice`
already needs.

```
cd path/to/RogerCraig
source .venv/bin/activate
pip install -e ".[voice]"
```

## 2. Test the listener by hand first

Before wiring it to login, confirm it actually hears your claps.

```
python extras/wakeup/clap_listener.py --workspace "$(pwd)"
```

Clap twice, about half a second apart. You should see:

```
[wake] clap 1…
[wake] clap 2 — waking HELENA
[debrief] asking HELENA…
```

Then, a few seconds later, it should speak the debrief out loud (macOS's
built-in `say` by default — no setup needed for this part).

If it doesn't trigger: check your Mac's mic input level (System Settings →
Sound → Input) — the claps need to visibly move that meter. If it triggers
on everything (typing, talking), open `extras/wakeup/clap_listener.py` and
raise `THRESHOLD` (currently `0.35`) in small steps, testing after each one.

Ctrl-C to stop.

## 3. (Optional) Use your real HELENA voice instead of macOS's `say`

If you've already set up `/say` (see the main README's Voice section —
`ELEVENLABS_API_KEY` in your shell profile), the debrief automatically uses
that voice instead. Nothing extra to configure — `debrief.py` checks for
`ELEVENLABS_API_KEY` at runtime and falls back to `say` if it's missing or
the request fails.

## 4. Wire it to login

```
cp extras/wakeup/com.helena.wake.plist ~/Library/LaunchAgents/com.helena.wake.plist
```

Edit the copy — replace `REPLACE_WITH_ABSOLUTE_PATH` with the real path to
this repo, e.g. `/Users/you/RogerCraig`:

```
sed -i '' "s|REPLACE_WITH_ABSOLUTE_PATH|$(pwd)|" ~/Library/LaunchAgents/com.helena.wake.plist
```

If your clone lives somewhere other than `~/RogerCraig`, also edit
`HELENA_REPO` at the top of `extras/wakeup/startup.sh` to match — that's the
path the Terminal window itself will `cd` into.

Load it:

```
launchctl load ~/Library/LaunchAgents/com.helena.wake.plist
```

## 5. Test the login path without actually logging out

```
launchctl start com.helena.wake
```

A new Terminal window should open on its own, `cd` into the repo, activate
the venv, and start listening — exactly what should happen next time you
log in. Clap twice to confirm end-to-end.

## Troubleshooting

**Nothing happens when you `launchctl start` it** — check the log:

```
cat /tmp/helena-wake.log
```

**Terminal opens but immediately closes / errors** — almost always the path
in the plist or `HELENA_REPO` in `startup.sh` doesn't match where you
actually cloned the repo. Re-run step 4's `sed` command with the correct
path, or edit both files by hand.

**It hears claps but the debrief comes back empty or errors** — HELENA's own
server isn't running. `debrief.py` shells out to `helena -p ...`, which
auto-starts the server the same way the terminal harness does, but Ollama
itself still needs to be installed and have `qwen2.5:7b-instruct` (or
whichever model you use) pulled — same requirement as the main README's
Quick start.

## Uninstall

```
launchctl unload ~/Library/LaunchAgents/com.helena.wake.plist
rm ~/Library/LaunchAgents/com.helena.wake.plist
```
