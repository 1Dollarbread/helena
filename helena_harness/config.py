"""Harness configuration.

Two layers, both optional, project wins over user:

    ~/.helena/settings.json              your defaults everywhere
    <workspace>/.helena/settings.json    this project's overrides

Environment variables (HELENA_*) beat both, because they're the most explicit
thing a user can do at launch time. Permission rules are the exception: allow
and deny lists from every layer are unioned rather than overwritten, so a
project can add grants without silently discarding your global ones.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

USER_DIR = Path(os.path.expanduser("~")) / ".helena"
USER_SETTINGS = USER_DIR / "settings.json"
PROJECT_DIRNAME = ".helena"
MEMORY_FILENAME = "HELENA.md"

VALID_MODES = ("ask", "auto", "plan", "yolo")


@dataclass
class Config:
    # Server
    server_url: str = "http://127.0.0.1:8080"
    api_token: str = ""
    auto_start_server: bool = True

    # Models
    model: str = ""            # empty means "whatever the server's default is"
    vision_model: str = ""
    embed_model: str = ""      # empty means the server's own default (nomic-embed-text)
    subagent_model: str = ""   # empty means "same as `model`" — set this to something
                                # smaller/faster to cut RAM and latency on subagent work,
                                # which is often read-heavy and doesn't need the biggest model
    num_ctx: int = 8192
    temperature: float = 0.4   # lower than chat default: agents should be literal

    # Cross-project memory — a growing set of embedded notes about the user,
    # searched by similarity and recalled before every turn. See memory.py.
    memory_enabled: bool = True
    memory_top_k: int = 5      # how many recalled memories get injected per turn

    # Loop
    max_iterations: int = 30
    max_tool_output_chars: int = 24_000
    history_messages: int = 60          # trimmed before each request
    subagent_max_depth: int = 2
    # After a turn where files were changed but nothing verified them (no
    # run_verification call), force one extra iteration nudging the model to
    # verify before the turn actually ends. See Agent.send in agent.py. This
    # is the concrete enforcement behind "after changing code, verify it" —
    # a small local model won't always act on that instruction unprompted.
    auto_verify: bool = True

    # Permissions
    mode: str = "ask"
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    allow_outside_workspace: bool = False
    command_timeout: int = 120

    # UI
    stream: bool = True
    show_tool_output: bool = True
    theme: str = "cyan"

    # Persona
    name: str = "HELENA"

    # Voice — optional, off by default. STT (speech-to-text, /voice) runs
    # fully locally via faster-whisper, no key needed, just an extra install
    # (pip install -e ".[voice]"). TTS (text-to-speech, HELENA's spoken voice)
    # is the one part of this project that isn't free/local — ElevenLabs
    # needs your own API key. See the README's Voice section for setup.
    voice_input_model: str = "base"        # faster-whisper model size
    speak_replies: bool = False            # auto-speak every reply via ElevenLabs
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # "Rachel", a stock ElevenLabs voice

    # Bare hands — optional, off by default. Wires in barehands
    # (github.com/jaredrhod/barehands), a hand-tracked glass control board:
    # HELENA gets hands and eyes on it (board_command / board_state /
    # board_stage_media), and the board's on-screen ring mirrors HELENA's own
    # live state (idle/listening/thinking/speaking) automatically, the same
    # mechanism barehands.md documents for wiring in an assistant. Empty
    # barehands_path means "not set up" — /barehands-setup clones, configures,
    # and starts it in one step. Everything here talks to 127.0.0.1 only.
    barehands_path: str = ""
    barehands_port: int = 8794

    # Populated at load time; not written back to disk.
    workspace: Path = field(default_factory=Path.cwd)

    # --- persistence -------------------------------------------------------

    @property
    def project_dir(self) -> Path:
        return self.workspace / PROJECT_DIRNAME

    @property
    def project_settings_path(self) -> Path:
        return self.project_dir / "settings.json"

    @property
    def memory_path(self) -> Path:
        return self.workspace / MEMORY_FILENAME

    @property
    def transcript_dir(self) -> Path:
        return self.project_dir / "sessions"

    @property
    def barehands_url(self) -> str:
        return f"http://127.0.0.1:{self.barehands_port}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("workspace", None)
        return data

    @classmethod
    def load(cls, workspace: Path | None = None) -> "Config":
        ws = Path(workspace or Path.cwd()).resolve()
        cfg = cls(workspace=ws)

        for path in (USER_SETTINGS, ws / PROJECT_DIRNAME / "settings.json"):
            cfg._merge_file(path)

        cfg._merge_env()
        if cfg.mode not in VALID_MODES:
            cfg.mode = "ask"
        return cfg

    def _merge_file(self, path: Path) -> None:
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            # A broken settings file shouldn't stop the harness from starting;
            # the user gets told about it at startup by `doctor`.
            return
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if key in ("allow", "deny") and isinstance(value, list):
                merged = getattr(self, key) + [v for v in value if v not in getattr(self, key)]
                setattr(self, key, merged)
            elif hasattr(self, key) and key != "workspace":
                setattr(self, key, value)

    def _merge_env(self) -> None:
        env_map = {
            "HELENA_SERVER_URL": "server_url",
            "HELENA_API_TOKEN": "api_token",
            "HELENA_MODEL": "model",
            "HELENA_SUBAGENT_MODEL": "subagent_model",
            "HELENA_VISION_MODEL": "vision_model",
            "HELENA_EMBED_MODEL": "embed_model",
            "HELENA_MODE": "mode",
            "HELENA_ELEVENLABS_VOICE_ID": "elevenlabs_voice_id",
            "HELENA_BAREHANDS_PATH": "barehands_path",
        }
        for env_key, attr in env_map.items():
            val = os.environ.get(env_key)
            if val:
                setattr(self, attr, val)
        # ElevenLabs' own SDKs and docs use plain ELEVENLABS_API_KEY — checked
        # as a fallback so a key already set for other tools just works here
        # too, without needing a HELENA-specific duplicate. HELENA_ prefixed
        # wins if you've deliberately set both.
        self.elevenlabs_api_key = (
            os.environ.get("HELENA_ELEVENLABS_API_KEY")
            or os.environ.get("ELEVENLABS_API_KEY")
            or self.elevenlabs_api_key
        )
        for env_key, attr in (
            ("HELENA_NUM_CTX", "num_ctx"),
            ("HELENA_MAX_ITERATIONS", "max_iterations"),
            ("HELENA_BAREHANDS_PORT", "barehands_port"),
            ("HELENA_MEMORY_TOP_K", "memory_top_k"),
        ):
            val = os.environ.get(env_key)
            if val and val.isdigit():
                setattr(self, attr, int(val))
        for env_key, attr in (
            ("HELENA_ALLOW_OUTSIDE_WORKSPACE", "allow_outside_workspace"),
            ("HELENA_SPEAK_REPLIES", "speak_replies"),
            ("HELENA_MEMORY_ENABLED", "memory_enabled"),
            ("HELENA_AUTO_VERIFY", "auto_verify"),
        ):
            val = os.environ.get(env_key)
            if val is not None:
                setattr(self, attr, val.strip().lower() in ("1", "true", "yes", "on"))

    def save_project(self) -> Path:
        """Persist the current settings as this project's overrides."""
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.project_settings_path.write_text(
            json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        return self.project_settings_path

    def save_user(self) -> Path:
        USER_DIR.mkdir(parents=True, exist_ok=True)
        USER_SETTINGS.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return USER_SETTINGS

    def add_rule(self, kind: str, rule: str, scope: str = "project") -> None:
        """Add an allow/deny rule and persist it to the chosen scope."""
        target = self.allow if kind == "allow" else self.deny
        if rule not in target:
            target.append(rule)
        path = self.project_settings_path if scope == "project" else USER_SETTINGS
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
        rules = existing.setdefault(kind, [])
        if rule not in rules:
            rules.append(rule)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    def read_memory(self) -> str:
        """Project instructions the model should always see (HELENA.md)."""
        for candidate in (self.memory_path, self.workspace / "CLAUDE.md", self.workspace / "AGENTS.md"):
            if candidate.is_file():
                try:
                    return candidate.read_text("utf-8")[:12_000]
                except OSError:
                    continue
        return ""
