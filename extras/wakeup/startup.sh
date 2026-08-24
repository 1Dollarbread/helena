#!/bin/bash
# Run at login by com.helena.wake.plist. Opens a visible Terminal window
# running the clap listener, so you can see it's alive and read its output
# — a listener with no window would be invisible if it silently died.
#
# HELENA_REPO must point at the cloned repo (edit below, or export it in
# your shell profile before installing the LaunchAgent).

HELENA_REPO="${HELENA_REPO:-$HOME/RogerCraig}"

osascript <<EOF
tell application "Terminal"
    activate
    do script "cd '$HELENA_REPO' && source .venv/bin/activate && python extras/wakeup/clap_listener.py --workspace '$HELENA_REPO'"
end tell
EOF
