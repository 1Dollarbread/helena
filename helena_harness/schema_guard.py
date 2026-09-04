"""Strict-ish JSON-schema validation for tool call arguments.

Ollama's structured-output support (`format`) and the OpenAI-style tool
protocol both describe a tool's arguments with a JSON Schema (`Tool.parameters`
in tools/base.py), but nothing enforced it before a call reached `tool.run()`.
A native tool_call from an OpenAI-compatible model, and a text-recovered one
from `Agent._inline_tool_calls`, both went straight into execution on the
strength of "the tool name matched" — a missing required field, a string
where an integer was declared, or a value outside an enum surfaced as a raw
Python exception (or, worse, silently did something the model didn't intend)
instead of a clear, actionable error the model could read and correct.

This is deliberately not a full JSON Schema implementation — just the parts
Helena's own tools actually use (type, required, enum, items, properties).
It's forgiving about the single most common thing a small local model gets
wrong — stringifying a number or boolean — by coercing those in place, and
strict about everything that actually indicates the model invented an
argument or skipped a required one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .tools.base import Tool

_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _coerce_scalar(value: Any, expected: str) -> tuple[Any, bool]:
    """Try to coerce `value` to `expected`'s JSON type. Returns (value, ok)."""
    if expected == "integer" and isinstance(value, str):
        try:
            return int(value.strip()), True
        except ValueError:
            return value, False
    if expected == "number" and isinstance(value, str):
        try:
            return float(value.strip()), True
        except ValueError:
            return value, False
    if expected == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True, True
        if lowered in ("false", "no", "0"):
            return False, True
        return value, False
    return value, False


def validate_args(tool: "Tool", args: dict[str, Any]) -> list[str]:
    """Validate `args` against `tool.parameters`, coercing in place where safe.

    Returns a list of human-readable problems — empty means the call is clean
    (and `args` may have been mutated with coerced values, e.g. "3" -> 3).
    Non-empty means the call should NOT run; feed the problems back to the
    model verbatim so it can correct the exact thing that was wrong.
    """
    if not isinstance(args, dict):
        return [f"Arguments must be a JSON object, got {type(args).__name__}."]

    schema = tool.parameters or {}
    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = schema.get("required") or []
    problems: list[str] = []

    for field_name in required:
        if field_name not in args or args[field_name] is None:
            problems.append(f"Missing required field `{field_name}`.")

    if properties:
        unknown = sorted(set(args) - set(properties))
        if unknown:
            allowed = ", ".join(sorted(properties)) or "(none)"
            problems.append(
                f"Unrecognized argument(s) {unknown} — this tool only accepts: {allowed}."
            )

    for key, value in list(args.items()):
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        enum = spec.get("enum")

        if enum is not None and value not in enum:
            problems.append(f"`{key}` must be one of {enum!r}, got {value!r}.")
            continue

        if expected in _TYPE_MAP and not isinstance(value, _TYPE_MAP[expected]):
            if expected in ("integer", "number") and isinstance(value, bool):
                problems.append(f"`{key}` must be a {expected}, got a boolean.")
                continue
            coerced, ok = _coerce_scalar(value, expected)
            if ok:
                args[key] = coerced
            else:
                problems.append(f"`{key}` must be a {expected}, got {value!r}.")
            continue

        if expected == "array" and isinstance(value, list):
            item_spec = spec.get("items")
            if isinstance(item_spec, dict) and item_spec.get("type") in _TYPE_MAP:
                item_type = item_spec["type"]
                for i, item in enumerate(value):
                    if not isinstance(item, _TYPE_MAP[item_type]):
                        problems.append(f"`{key}[{i}]` must be a {item_type}, got {item!r}.")
                        break

    return problems
