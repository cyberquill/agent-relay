"""Pull the agent's last reply out of a harness transcript file (JSON or JSON Lines)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from pydantic import JsonValue

MAX_READ_BYTES = 2_000_000
_TEXT_KEYS = ("content", "text", "message", "value")


def _is_tool_part(value: JsonValue) -> bool:
    if not isinstance(value, dict):
        return False
    return value.get("type") in ("tool_use", "tool_result") or value.get("kind") == "toolInvocation"


def _text_of(value: JsonValue) -> str | None:
    """The text of a message. A tool call inside it ends what came before: only the text after the
    last tool call counts, because that is the answer and the rest was narration."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if _is_tool_part(item):
                parts.clear()
                continue
            found = _text_of(item)
            if found:
                parts.append(found)
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


def _is_telemetry(node: dict[str, JsonValue]) -> bool:
    """A Copilot CLI event that is not a message of the conversation: `model.response`, `hook.start`,
    `session.*`. They carry `role: assistant` text of their own (the frustration classifier's answer),
    which must never be taken for the reply. Only `assistant.message` is both dotted and a reply."""
    kind = node.get("type")
    return isinstance(kind, str) and "." in kind and kind != "assistant.message"


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
        if _is_telemetry(node):
            return None
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


def _this_turn(entries: list[JsonValue]) -> list[JsonValue]:
    """The Copilot CLI entries after its newest `user.message`: an earlier turn's answer is not this one's."""
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if isinstance(entry, dict) and entry.get("type") == "user.message":
            return entries[index + 1 :]
    return entries


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
        return _last_assistant(_this_turn(entries))


REPLY_WAIT_S = 3.0
REPLY_POLL_S = 0.2


async def wait_for_reply(
    transcript: str | None, wait_s: float = REPLY_WAIT_S, poll_s: float = REPLY_POLL_S
) -> str | None:
    """The reply of the turn that just stopped. The Copilot CLI writes its events a moment after it runs
    the stop hook, so the final `assistant.message` may not be on disk yet: look again for up to `wait_s`
    while the file exists and shows no reply. A transcript that is not there is not waited for."""
    if not transcript:
        return None
    deadline = time.monotonic() + wait_s
    while True:
        found = await asyncio.to_thread(last_reply, transcript)
        if found is not None or not Path(transcript).exists() or time.monotonic() >= deadline:
            return found
        await asyncio.sleep(poll_s)
