"""Permission engine.

Every tool call is described as a `PermissionRequest` and checked here before
it runs. Four modes:

    ask    reads run freely; anything that writes, deletes, or executes asks
    auto   reads and file edits run freely; commands still ask
    plan   read-only — mutations are refused, so the model can survey and
           propose without touching anything
    yolo   everything is allowed except the hard-blocked commands below

Rules are strings shaped like the tool call they authorize:

    run_command(git status:*)   any command starting with "git status"
    read_file(*)                any read
    write_file(src/**)          writes under src/
    web_search                  the whole tool, no argument filter
    *                           everything (equivalent to yolo, but explicit)

Deny always beats allow. Familiar aliases (Bash, Read, Write, Edit) are
accepted so rules copied from other agent tools keep working.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from .config import Config


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class Action(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    NONE = "none"


# Familiar names from other agent CLIs → this harness's tool names.
ALIASES = {
    "bash": "run_command",
    "shell": "run_command",
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "grep": "search_text",
    "glob": "find_files",
    "websearch": "web_search",
    "webfetch": "fetch_url",
    "task": "spawn_agent",
}

# A command line is judged per *segment* (split on ; && || |) with quoted
# strings removed first, so the check looks at what would actually execute.
# `echo "rm -rf /"` and `git commit -m "remove rm -rf / from docs"` are not
# destructive, and treating them as if they were teaches people to reflexively
# approve prompts — which is worse than not checking at all.
QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
SEGMENT_RE = re.compile(r"\|\||&&|;|\||\n")
# Wrappers that don't change what the real command is.
PREFIX = r"^(?:sudo\s+(?:-\w+\s+)*|env\s+\w+=\S+\s+|time\s+|nohup\s+)*"

# Never run these, in any mode. Not a security boundary against a determined
# attacker — a model can always phrase things differently — but it does stop
# the realistic case: a model that pattern-matched its way into a catastrophic
# one-liner, or a prompt-injected file talking it into one.
HARD_BLOCKED = [
    (PREFIX + r"rm\s+(?:-[a-zA-Z]*\s+)*(?:-rf|-fr|-r\s+-f)\s+(?:/|/\*|~|~/|\$HOME)(?:\s|$)",
     "recursive delete of / or $HOME"),
    (PREFIX + r"mkfs(\.\w+)?\b", "filesystem format"),
    (PREFIX + r"dd\b[^;]*\bof=/dev/(?:sd|nvme|disk|hd)", "raw write to a block device"),
    (r">\s*/dev/(?:sd|nvme|disk|hd)\w*", "raw write to a block device"),
    (PREFIX + r"chmod\s+(?:-[a-zA-Z]+\s+)*777\s+/(?:\s|$)", "world-writable root"),
    (PREFIX + r"(?:shutdown|reboot|halt)\b", "power state change"),
]

# Checked against the whole command line, because the pipeline *is* the danger.
HARD_BLOCKED_WHOLE = [
    (r":\s*\(\s*\)\s*\{.*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
]

# Allowed, but always confirmed by a human first — even in auto mode.
RISKY = [
    (PREFIX + r"rm\s+-[a-zA-Z]*r", "recursive delete"),
    (PREFIX + r"git\s+push\b", "pushes to a remote"),
    (PREFIX + r"git\s+reset\s+--hard\b", "discards local changes"),
    (PREFIX + r"git\s+clean\b", "deletes untracked files"),
    (r"^sudo\b", "runs as root"),
    (PREFIX + r"(?:npm|pnpm|yarn)\s+publish\b", "publishes a package"),
    (PREFIX + r"twine\s+upload\b", "publishes a package"),
    (PREFIX + r"docker\s+(?:rm|rmi|system\s+prune)", "removes container resources"),
    (PREFIX + r"kill(?:all)?\s+-9\b", "force-kills processes"),
    (PREFIX + r"(?:apt|apt-get|yum|brew|pacman)\s+(?:install|remove|upgrade)", "changes system packages"),
]

RISKY_WHOLE = [
    (r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh", "pipes a download into a shell"),
]


@dataclass
class PermissionRequest:
    tool: str
    action: Action
    key: str = ""            # what a rule pattern is matched against
    preview: str = ""        # one line shown to the user
    detail: str = ""         # optional extra context (a diff, a command)
    agent: str = "helena"    # which agent is asking (subagents included)


@dataclass
class PermissionResult:
    decision: Decision
    reason: str = ""
    rule: str = ""
    danger: str = ""


@dataclass
class ParsedRule:
    tool: str
    pattern: str | None

    def matches(self, tool: str, key: str) -> bool:
        if self.tool != "*" and self.tool != tool:
            return False
        if self.pattern in (None, "*"):
            return True
        pattern = self.pattern
        if pattern.endswith(":*"):
            return key.startswith(pattern[:-2])
        if any(ch in pattern for ch in "*?[]"):
            # Match both the literal key and its path-ish form, so
            # write_file(src/**) works whether the model passed "src/a.py"
            # or an absolute path ending in it.
            return fnmatch.fnmatch(key, pattern) or fnmatch.fnmatch(key, f"*/{pattern}")
        return key == pattern


def parse_rule(raw: str) -> ParsedRule:
    raw = raw.strip()
    m = re.match(r"^([\w*]+)\s*\((.*)\)$", raw, re.DOTALL)
    if m:
        tool, pattern = m.group(1), m.group(2).strip()
    else:
        tool, pattern = raw, None
    tool = ALIASES.get(tool.lower(), tool.lower())
    return ParsedRule(tool=tool, pattern=pattern)


def command_segments(command: str) -> list[str]:
    """The individual commands in a line, with quoted text removed."""
    without_quotes = QUOTED_RE.sub(" ", command)
    return [seg.strip() for seg in SEGMENT_RE.split(without_quotes) if seg.strip()]


def classify_command(command: str) -> tuple[str | None, str | None]:
    """Return (hard_block_reason, risky_reason) for a shell command."""
    whole = QUOTED_RE.sub(" ", command)
    segments = command_segments(command)

    for pattern, reason in HARD_BLOCKED_WHOLE:
        if re.search(pattern, whole):
            return reason, None
    for segment in segments:
        for pattern, reason in HARD_BLOCKED:
            if re.search(pattern, segment):
                return reason, None

    for pattern, reason in RISKY_WHOLE:
        if re.search(pattern, whole):
            return None, reason
    for segment in segments:
        for pattern, reason in RISKY:
            if re.search(pattern, segment):
                return None, reason
    return None, None


# Callback the UI installs: given a request, return one of
# "once" | "always" | "session" | "no".
AskCallback = Callable[[PermissionRequest, PermissionResult], Awaitable[str]]


class PermissionEngine:
    def __init__(self, config: Config, ask: AskCallback | None = None) -> None:
        self.config = config
        self.ask_callback = ask
        self.session_allow: set[tuple[str, str]] = set()
        self.denied_this_session: list[str] = []
        self._stats: dict[str, int] = {"allowed": 0, "denied": 0, "asked": 0}
        # Subagents share this same engine (see ToolContext.child), and with
        # concurrent spawn_agent calls now allowed, more than one tool call
        # could hit an "ask" decision at the same moment. Only one interactive
        # prompt can sensibly be on screen at a time, so this serializes just
        # the prompt itself — the tool work around it still runs concurrently.
        self._ask_lock = asyncio.Lock()

    # --- rule evaluation ---------------------------------------------------

    @property
    def mode(self) -> str:
        return self.config.mode

    def set_mode(self, mode: str) -> None:
        self.config.mode = mode

    def _rules(self, kind: str) -> list[ParsedRule]:
        raw = self.config.allow if kind == "allow" else self.config.deny
        return [parse_rule(r) for r in raw]

    def evaluate(self, req: PermissionRequest) -> PermissionResult:
        """Pure decision function — no prompting, no side effects."""
        # 1. Hard blocks: never negotiable.
        if req.action is Action.EXECUTE:
            blocked, risky = classify_command(req.key)
            if blocked:
                return PermissionResult(
                    Decision.DENY,
                    reason=f"Refused: this command performs a {blocked}. "
                           "Run it yourself if you really mean to.",
                    danger=blocked,
                )
        else:
            risky = None

        # 2. Explicit deny rules.
        for rule in self._rules("deny"):
            if rule.matches(req.tool, req.key):
                return PermissionResult(
                    Decision.DENY,
                    reason=f"Blocked by deny rule for {req.tool}.",
                    rule=self._render(rule),
                )

        # 3. Plan mode: nothing may change the world.
        if self.mode == "plan" and req.action in (Action.WRITE, Action.EXECUTE):
            return PermissionResult(
                Decision.DENY,
                reason="Plan mode is read-only. Propose the change instead, "
                       "or leave plan mode with /mode ask.",
            )

        # 4. Explicit allow rules and session grants.
        for rule in self._rules("allow"):
            if rule.matches(req.tool, req.key):
                return PermissionResult(
                    Decision.ALLOW, reason="Matched an allow rule.", rule=self._render(rule)
                )
        if (req.tool, req.key) in self.session_allow or (req.tool, "*") in self.session_allow:
            return PermissionResult(Decision.ALLOW, reason="Allowed earlier this session.")

        # 5. Mode defaults.
        if self.mode == "yolo":
            return PermissionResult(Decision.ALLOW, reason="yolo mode.")
        if req.action in (Action.READ, Action.NONE):
            return PermissionResult(Decision.ALLOW, reason="Read-only.")
        if req.action is Action.NETWORK:
            return PermissionResult(
                Decision.ALLOW if self.mode in ("auto", "plan") else Decision.ASK,
                reason="Network read.",
            )
        if risky:
            return PermissionResult(Decision.ASK, reason=f"This {risky}.", danger=risky)
        if self.mode == "auto" and req.action is Action.WRITE:
            return PermissionResult(Decision.ALLOW, reason="auto mode allows edits.")
        return PermissionResult(Decision.ASK, reason="Needs your OK.")

    @staticmethod
    def _render(rule: ParsedRule) -> str:
        return f"{rule.tool}({rule.pattern})" if rule.pattern else rule.tool

    # --- interactive path --------------------------------------------------

    async def check(self, req: PermissionRequest) -> PermissionResult:
        """Evaluate, prompting the user when the answer is 'ask'."""
        result = self.evaluate(req)
        if result.decision is not Decision.ASK:
            self._stats["allowed" if result.decision is Decision.ALLOW else "denied"] += 1
            if result.decision is Decision.DENY:
                self.denied_this_session.append(f"{req.tool}: {req.preview}")
            return result

        if self.ask_callback is None:
            # Non-interactive (a parallel subagent, a piped stdin): refuse
            # rather than silently escalating.
            self._stats["denied"] += 1
            return PermissionResult(
                Decision.DENY,
                reason="Needed approval but no one is at the keyboard. "
                       "Pre-approve it with /permissions allow, or use --mode auto.",
            )

        self._stats["asked"] += 1
        async with self._ask_lock:
            answer = await self.ask_callback(req, result)
        if answer == "always":
            rule = self.suggest_rule(req)
            self.config.add_rule("allow", rule)
            self._stats["allowed"] += 1
            return PermissionResult(Decision.ALLOW, reason=f"Always allowed ({rule}).", rule=rule)
        if answer == "session":
            self.session_allow.add((req.tool, req.key))
            self._stats["allowed"] += 1
            return PermissionResult(Decision.ALLOW, reason="Allowed for this session.")
        if answer == "once":
            self._stats["allowed"] += 1
            return PermissionResult(Decision.ALLOW, reason="Allowed once.")
        self._stats["denied"] += 1
        self.denied_this_session.append(f"{req.tool}: {req.preview}")
        return PermissionResult(Decision.DENY, reason="You declined this one.")

    def suggest_rule(self, req: PermissionRequest) -> str:
        """The rule an 'always allow' answer should create."""
        if req.action is Action.EXECUTE:
            # Grant the verb, not the exact invocation: "git status:*" rather
            # than one frozen command line nobody will ever match again.
            parts = req.key.split()
            prefix = " ".join(parts[:2]) if len(parts) > 1 else (parts[0] if parts else req.key)
            return f"{req.tool}({prefix}:*)"
        if req.action is Action.WRITE and req.key:
            return f"{req.tool}({req.key})"
        return req.tool

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)


@dataclass
class ToolPermission:
    """Declared by each tool: how its calls should be classified."""

    action: Action = Action.NONE
    key_arg: str | None = None      # which argument becomes the rule key
    fields: list[str] = field(default_factory=list)
