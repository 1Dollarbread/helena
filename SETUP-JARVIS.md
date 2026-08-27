# Web HUD + wake-on-clap — testing guide

Setup itself is now `./setup.sh` (see the main [README](./README.md)) — it
installs both of these and, if you say yes, wires up the wake-on-clap login
item with no manual path-editing. This file is what's left: how to confirm
each one actually works once `setup.sh` has run.

---

## Web HUD

```bash
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
- The sidebar (☰ to toggle) should list past sessions once you have more
  than one — click one to switch, "+ NEW" to start fresh.
- Click the 🔔 once to enable push notifications, then background the tab
  and ask it to do something — you should get an OS notification when it
  finishes.
- From another terminal, `helena --attach` should join the exact same
  conversation you have open in the browser — anything you type in either
  one shows up in both.

**If something goes wrong:** watch the terminal window running `helena-web`,
not just the browser — real errors print there as a full Python traceback.

**Options:**

```bash
helena-web -C ~/some/other/project    # point it at a different project
helena-web --port 9000                # different port
helena-web --mode auto                # skip permission prompts (careful)
```

---

## Wake-on-clap

If you said yes to this in `setup.sh`, it's already installed as a login
item — `launchctl list | grep helena` should show `com.helena.wake`. To test
without logging out:

```bash
launchctl start com.helena.wake
```

A new Terminal window should open by itself, `cd` into the repo, activate
the venv, and start listening:

```
[wake] listening for two claps · threshold=0.35 refractory_s=0.15 clap_window_s=1.2 cooldown_s=4.0
```

Clap twice, about half a second apart, near your Mac's mic:

```
[wake] clap 1…
[wake] clap 2 — waking HELENA
[debrief] asking HELENA…
```

A few seconds later it should speak a summary out loud — macOS's built-in
voice by default (`say`), or your ElevenLabs voice if you have
`ELEVENLABS_API_KEY` set (see the main README's Voice section).

**Tuning** — from inside HELENA, not by editing any file:

```
/wake-config                     # show current values
/wake-config set threshold 0.5   # less sensitive
/wake-config set cooldown_s 6
/wake-config reset
```

A running listener picks up the change within a couple of seconds, no
restart needed.

- **Doesn't trigger at all** — check System Settings → Sound → Input and
  confirm the level meter visibly jumps when you clap. If it does but
  nothing happens, the mic HELENA is picking up may not be the default
  input device.
- **Triggers on everything** (typing, talking, background noise) — raise
  the threshold with `/wake-config set threshold 0.5` (or higher).

### Troubleshooting the login item

**Nothing happens when you `launchctl start` it:**

```bash
cat /tmp/helena-wake.log
```

That'll show whatever error stopped it. If the repo moved since you ran
`setup.sh`, re-run `./setup.sh --wake` to regenerate the login item with the
new path.
