"""Runs after two claps: gathers what changed, asks HELENA to summarize it,
and speaks the answer out loud.

Deliberately does NOT let the model reach for tools here (`--mode plan`) —
this fires unattended off a clap, so nothing should be able to write, run a
command, or need a permission prompt nobody's there to answer. All the real
context (git status, recent commits, HELENA.md, TODOs) is gathered up front
in plain subprocess calls and handed to the model as text; the model's only
job is to summarize it into something worth saying out loud.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEBRIEF_PROMPT = """You just woke up. Give a spoken debrief: 4-6 sentences, \
plain prose (no markdown, no bullet points, no headers — this gets read \
aloud by a TTS engine). Cover, in this order: (1) what branch/state the repo \
is in and whether anything's uncommitted, (2) the most recent real progress \
from the commit log, (3) any TODOs or obviously unfinished work you can see, \
(4) one clear suggestion for what to work on next. Be direct and brief, like \
a colleague catching someone up in the hallway — not a report.

--- gathered context ---
{context}
"""


def run(cmd: list[str], cwd: Path) -> str:
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001 - this is best-effort context, never fatal
        return f"(unavailable: {exc})"


def gather_context(workspace: Path) -> str:
    parts = []
    parts.append("git status:\n" + run(["git", "status", "--short", "--branch"], workspace))
    parts.append("last 8 commits:\n" + run(["git", "log", "-8", "--oneline"], workspace))
    parts.append("uncommitted diff stat:\n" + run(["git", "diff", "--stat"], workspace))

    helena_md = workspace / "HELENA.md"
    if helena_md.exists():
        parts.append("HELENA.md:\n" + helena_md.read_text()[:2000])

    todos = run(
        ["grep", "-rn", "--include=*.py", "--include=*.js", "--include=*.ts",
         "-e", "TODO", "-e", "FIXME", "."],
        workspace,
    )
    if todos and not todos.startswith("(unavailable"):
        parts.append("TODO/FIXME hits (first 20 lines):\n" + "\n".join(todos.splitlines()[:20]))

    return "\n\n".join(parts)


def ask_helena(workspace: Path, prompt: str) -> str:
    result = subprocess.run(
        ["helena", "-p", prompt, "--mode", "plan", "-C", str(workspace)],
        capture_output=True, text=True, timeout=180,
    )
    text = result.stdout.strip()
    return text or "I couldn't reach the model server for a debrief — check ./start.sh."


def speak(text: str) -> None:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if api_key:
        spoke = _speak_elevenlabs(text, api_key)
        if spoke:
            return
    # Always-available fallback: macOS's built-in `say`, zero setup required.
    subprocess.run(["say", text])


def _speak_elevenlabs(text: str, api_key: str) -> bool:
    try:
        import asyncio
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from helena_harness import tts

        async def _run() -> None:
            audio = await tts.synthesize(text, api_key, os.environ.get("ELEVENLABS_VOICE_ID"))
            await tts.play(audio)

        asyncio.run(_run())
        return True
    except Exception as exc:  # noqa: BLE001 - fall back to `say` on any failure
        print(f"[debrief] ElevenLabs TTS failed ({exc}), falling back to `say`.", file=sys.stderr)
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(Path.cwd()))
    args = parser.parse_args()
    workspace = Path(args.workspace).expanduser()

    context = gather_context(workspace)
    prompt = DEBRIEF_PROMPT.format(context=context)
    print("[debrief] asking HELENA…")
    text = ask_helena(workspace, prompt)
    print(f"[debrief] {text}")
    speak(text)


if __name__ == "__main__":
    main()
