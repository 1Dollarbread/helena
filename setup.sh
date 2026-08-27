#!/usr/bin/env bash
# One-time setup for H.E.L.E.N.A. Safe to re-run — every step checks whether
# it's already done and skips it, so this is also how you pick up new
# optional pieces (like push notifications) later without redoing anything.
#
# Collapses what used to be spread across the README's Quick Start and
# SETUP-JARVIS.md into three things you actually have to run:
#
#   ./setup.sh      (this — once, or again any time you want to add a piece)
#   ./start.sh      (each time you sit down to use HELENA)
#   helena           (or helena-web)
#
# Flags (all optional — with none, you'll be asked interactively):
#   --minimal        core only, skip voice/push/wake entirely, no prompts
#   --everything     voice + push + wake, no prompts
#   --no-wake        skip the wake-on-clap login item
#   --wake           install the wake-on-clap login item, no prompt
#   --with-vision    also pull the llava vision model (~4.5GB)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MINIMAL=0
EVERYTHING=0
WAKE=""       # "" = ask, "yes", "no"
WITH_VISION=0
for arg in "$@"; do
  case "$arg" in
    --minimal) MINIMAL=1 ;;
    --everything) EVERYTHING=1 ;;
    --wake) WAKE=yes ;;
    --no-wake) WAKE=no ;;
    --with-vision) WITH_VISION=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
skip() { echo "  · $*"; }
warn() { echo "  ⚠ $*" >&2; }

ask_yes_no() {
  # Only prompts in an interactive terminal; otherwise takes the default,
  # so `./setup.sh --everything` or piping into a script never hangs.
  local prompt="$1" default="$2"
  if [ ! -t 0 ]; then echo "$default"; return; fi
  local suffix="[y/N]"; [ "$default" = "yes" ] && suffix="[Y/n]"
  read -r -p "$prompt $suffix " reply
  reply="${reply:-$default}"
  case "$reply" in y|Y|yes|Yes) echo yes ;; *) echo no ;; esac
}

echo "H.E.L.E.N.A setup"
echo "================="

# --- 1. Python + venv ------------------------------------------------------

say "Checking Python"
PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON" ]; then
  echo "error: no python3 on PATH. Install Python 3.10+ first (https://python.org)." >&2
  exit 1
fi
PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
ok "found $PYTHON ($PY_VERSION)"

if [ -d .venv ]; then
  skip "virtualenv at .venv already exists"
else
  say "Creating virtualenv at .venv"
  "$PYTHON" -m venv .venv
  ok "created"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# --- 2. Install --------------------------------------------------------

say "Installing HELENA (core)"
pip install -q -e ".[dev]"
ok "core installed"

if [ "$MINIMAL" = 1 ]; then
  WANT_VOICE=no; WANT_PUSH=no
elif [ "$EVERYTHING" = 1 ]; then
  WANT_VOICE=yes; WANT_PUSH=yes
else
  WANT_VOICE="$(ask_yes_no 'Install voice extras (wake-on-clap debrief speech, mic listening)?' yes)"
  WANT_PUSH="$(ask_yes_no 'Install push-notification extras (browser alerts when a turn finishes)?' yes)"
fi

if [ "$WANT_VOICE" = yes ]; then
  say "Installing voice extra"
  if pip install -q -e ".[voice]"; then
    ok "voice extra installed"
  else
    warn "voice extra failed to install — usually a missing system audio library (portaudio)."
    warn "Wake-on-clap and spoken debriefs won't work until this is resolved; everything else is unaffected."
  fi
elif [ "$MINIMAL" = 1 ]; then
  skip "skipping voice extra (--minimal)"
else
  skip "skipping voice extra"
fi

if [ "$WANT_PUSH" = yes ]; then
  say "Installing push extra"
  if pip install -q -e ".[push]"; then
    ok "push extra installed"
  else
    warn "push extra failed to install — the web HUD will work fine, just without background notifications."
  fi
elif [ "$MINIMAL" = 1 ]; then
  skip "skipping push extra (--minimal)"
else
  skip "skipping push extra"
fi

# --- 3. Models + server stack -----------------------------------------

if command -v ollama >/dev/null 2>&1; then
  ok "ollama found"
  if ollama list 2>/dev/null | grep -q '^qwen2.5:7b-instruct'; then
    skip "qwen2.5:7b-instruct already pulled"
  else
    say "Pulling qwen2.5:7b-instruct (required, tool-calling model — this can take a while)"
    ollama pull qwen2.5:7b-instruct
    ok "model pulled"
  fi
  if [ "$WITH_VISION" = 1 ]; then
    if ollama list 2>/dev/null | grep -q '^llava'; then
      skip "llava already pulled"
    else
      say "Pulling llava (vision model, ~4.5GB)"
      ollama pull llava
      ok "model pulled"
    fi
  fi
else
  warn "ollama not found on PATH — install it from https://ollama.com, then re-run ./setup.sh"
fi

# --- 4. Wake-on-clap (macOS only) ---------------------------------------

PLIST_DEST="$HOME/Library/LaunchAgents/com.helena.wake.plist"
if [ "$(uname -s)" != "Darwin" ]; then
  skip "skipping wake-on-clap (macOS only)"
elif [ "$WANT_VOICE" != yes ]; then
  skip "skipping wake-on-clap (needs the voice extra — say yes to that to enable this)"
else
  if [ -z "$WAKE" ]; then
    WAKE="$(ask_yes_no 'Install wake-on-clap as a login item (clap twice for a spoken project debrief)?' no)"
  fi
  if [ "$WAKE" = yes ]; then
    say "Installing wake-on-clap login item"
    mkdir -p "$HOME/Library/LaunchAgents"
    # The plist gets HELENA_REPO baked in as an environment variable rather
    # than anyone hand-editing extras/wakeup/startup.sh or sed-ing a path
    # into a copy of it — this is the only manual step that used to exist
    # here, and it's exactly the kind of thing worth a script doing once
    # instead of a person doing by hand every clone.
    sed -e "s|REPLACE_WITH_ABSOLUTE_PATH|$SCRIPT_DIR|g" \
        -e "s|<key>RunAtLoad</key>|<key>EnvironmentVariables</key><dict><key>HELENA_REPO</key><string>$SCRIPT_DIR</string></dict><key>RunAtLoad</key>|" \
        extras/wakeup/com.helena.wake.plist > "$PLIST_DEST"
    launchctl unload "$PLIST_DEST" >/dev/null 2>&1 || true
    launchctl load "$PLIST_DEST"
    ok "installed — active from your next login, or run: launchctl start com.helena.wake"
    echo "    (tuning: once HELENA is running, use /wake-config instead of editing any file)"
  else
    skip "skipping wake-on-clap"
  fi
fi

# --- done ------------------------------------------------------------------

echo
echo "Setup complete. From here:"
echo "  1. ./start.sh     — start Ollama + the HELENA server"
echo "  2. helena          — terminal agent   (or: helena-web  — browser HUD)"
echo "  3. ./stop.sh      — shut the stack down when you're done"
echo
echo "Re-run ./setup.sh any time — it only does what isn't already done."
