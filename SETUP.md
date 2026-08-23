# Setup — start here

This is the only file you need to read to get HELENA running. Everything
else in this repo (`README.md`) is reference material for later — skip it
for now.

Do the steps in order. Each one has exactly one command block to copy-paste.
If a step fails, stop and look at [If something goes wrong](#if-something-goes-wrong)
at the bottom before moving on.

## What you're installing

Three separate things, in this order:

1. **Ollama** — runs the AI model itself, on your Mac, for free.
2. **HELENA** — this repo. The assistant that talks to Ollama and does things
   for you (edit files, run commands, etc).
3. **barehands** *(optional)* — the hand-tracking control board. Skip this
   entirely if you just want to talk to HELENA in a terminal for now; add it
   later whenever you want.

---

## Step 1 — Install Ollama

If you already have it, skip to Step 2.

Go to [ollama.com](https://ollama.com), download it, open it once. That's
the whole install — it runs quietly in the background from then on.

## Step 2 — Get a model

Open **Terminal** (Applications → Utilities → Terminal) and paste this:

```bash
ollama pull qwen2.5:7b-instruct
```

This downloads a few gigabytes — it'll take a few minutes depending on your
internet. Wait for it to finish before moving on.

## Step 3 — Put this folder somewhere permanent

Wherever you unzipped this, move the whole folder somewhere you won't
delete it — your home folder or a `~/projects` folder is fine. Then, in
Terminal:

```bash
cd ~/path/to/this/folder
```

(Drag the folder into the Terminal window after typing `cd ` — it'll fill in
the path for you.)

## Step 4 — Set up Python

Paste this whole block at once:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

If this errors out, see [If something goes wrong](#if-something-goes-wrong).

## Step 5 — Start it

```bash
./start.sh
```

Then:

```bash
helena
```

You should see a banner and a `you ›` prompt. **This is HELENA working.**
Type something and hit Enter.

When you're done for the day, `/exit` to leave HELENA, then `./stop.sh` in
that same terminal to shut everything down.

---

## Every time after today

You don't need to repeat Steps 1-4. Every time you want to use HELENA:

```bash
cd ~/path/to/this/folder
source .venv/bin/activate
./start.sh
helena
```

Consider making a note of that four-line block somewhere — it's the entire
daily routine.

---

## Optional: voice

Two independent things, do either or both whenever you want:

**Talk to HELENA out loud** (free, no account):
```bash
pip install -e ".[voice]"
```
Then inside HELENA, type `/voice`.

**HELENA talks back out loud** (needs a free ElevenLabs account — the only
non-free, non-local piece of this whole project):
Inside HELENA, type `/voice-setup` and follow what it prints exactly.

## Optional: barehands (hand-tracking control board)

Skip this until you've used HELENA in a plain terminal a few times and want
the hands-in-the-air version.

Inside HELENA:
```
/barehands-setup
```

It downloads and starts barehands for you — no extra installs needed for
that part. It'll print a URL to open in Chrome when it's done.

**Then apply the visual patch that's already in this folder:**

```bash
cd ~/barehands
cp stage.html stage.html.backup
cp ~/path/to/this/folder/extras/barehands/stage.html stage.html
```

This makes tapping much more forgiving (the original settings were tuned
tighter than webcam tracking can reliably hit) and gives you a Stark-style
HUD background option instead of your own face on the projector — open
`http://127.0.0.1:8794/stage.html?bg=stark` instead of the plain URL to use
it. Full details on what this changed: `extras/barehands/BAREHANDS-VISUAL-PATCH.md`.

---

## If something goes wrong

**`python3 -m venv` or `pip install` errors, or says "not a Python project"**
— you're very likely not inside the right folder. Run `pwd` and check it
prints the path to *this* folder (the one with `pyproject.toml` in it —
run `ls pyproject.toml` to check; if that errors, you're in the wrong place).

**`helena` says "No HELENA server"** — Ollama isn't running, or `./start.sh`
didn't finish. Try `./start.sh` again and read what it prints.

**`helena: command not found`** — Step 4 didn't finish successfully, or you
opened a new terminal window without re-running `source .venv/bin/activate`
in it first (that line only applies to the terminal tab you ran it in).

**Anything else** — once HELENA is running at all, type `/doctor` and it
will tell you exactly what is and isn't working. That's the fastest way to
diagnose almost everything past this point. `README.md`'s Troubleshooting
section covers specific messages in detail.
