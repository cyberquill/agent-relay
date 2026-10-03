"""Pull the agent's last reply out of a harness transcript file (JSON or JSON Lines)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue

MAX_READ_BYTES = 2_000_000
_TEXT_KEYS = ("content", "text", "message", "value")


def _text_of(value: JsonValue) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, list):
        parts = [t for t in (_text_of(v) for v in value) if t]
        return "\n".join(parts) if parts else None
    if isinstance(value, dict):
        if value.get("type") in ("tool_use", "tool_result", "thinking", "reasoning"):
            return None
        for key in _TEXT_KEYS:
            if key in value:
                found = _text_of(value[key])
                if found:
                    return found
    return None


def _is_assistant(node: dict[str, JsonValue]) -> bool:
    role = node.get("role")
    kind = node.get("type")
    return role == "assistant" or (
        isinstance(kind, str) and kind in ("assistant", "assistant.message", "response")
    )


def _last_assistant(node: JsonValue) -> str | None:
    """Depth-first from the end, so the most recent assistant entry wins. A `response` key is the
    VS Code chat-session shape (`requests[].response[].value`)."""
    if isinstance(node, list):
        for item in reversed(node):
            found = _last_assistant(item)
            if found:
                return found
        return None
    if isinstance(node, dict):
        if "response" in node:
            text = _text_of(node["response"])
            if text:
                return text
        if _is_assistant(node):
            for key in ("data", *_TEXT_KEYS):
                if key in node:
                    text = _text_of(node[key])
                    if text:
                        return text
        for value in reversed(list(node.values())):
            if isinstance(value, dict | list):
                found = _last_assistant(value)
                if found:
                    return found
    return None


def last_reply(transcript: str | None) -> str | None:
    if not transcript:
        return None
    file = Path(transcript)
    try:
        size = file.stat().st_size
        with file.open("rb") as handle:
            if size > MAX_READ_BYTES:
                handle.seek(size - MAX_READ_BYTES)
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    try:
        return _last_assistant(json.loads(raw))
    except json.JSONDecodeError:
        entries: list[JsonValue] = []
        for line in raw.splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return _last_assistant(entries)
