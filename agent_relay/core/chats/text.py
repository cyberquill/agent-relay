"""Shaping chat text for a phone-sized list: wrappers off, whitespace squashed, ends kept."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import JsonValue

from agent_relay.scrub import scrub_text

END_CHARS = 100
PROMPT_CHARS = 80
TITLE_CHARS = 80
ELLIPSIS = " … "
READ_TAIL_BYTES = 1_500_000
READ_HEAD_BYTES = 262_144

_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
_WRAPPER = re.compile(
    r"<(timestamp|attached_files|open_and_recently_viewed_files|system_reminder)>.*?</\1>", re.S
)
_FENCE = re.compile(r"```[^\n]*\n?")
_SPACE = re.compile(r"\s+")


def squash(text: str) -> str:
    """One line: code fences dropped, every run of whitespace a single space."""
    return _SPACE.sub(" ", _FENCE.sub(" ", text)).strip()


def ends(text: str, size: int = END_CHARS) -> str:
    """The first and last `size` characters with an ellipsis between, or all of it when short."""
    one = squash(text)
    if len(one) <= 2 * size + len(ELLIPSIS):
        return one
    return f"{one[:size].rstrip()}{ELLIPSIS}{one[-size:].lstrip()}"


def short(text: str, size: int) -> str:
    one = squash(text)
    return one if len(one) <= size else f"{one[: size - 1].rstrip()}…"


def user_text(raw: str) -> str:
    """What the person typed: the `<user_query>` of a Cursor prompt, else the text without app wrappers."""
    found = _USER_QUERY.search(raw)
    return found.group(1) if found else _WRAPPER.sub("", raw).strip()


def safe(text: str | None) -> str | None:
    """Scrubbed, so a secret in an old chat never reaches the phone or a log."""
    return scrub_text(text) if text else text


def tail_lines(file: Path, limit: int = READ_TAIL_BYTES) -> list[JsonValue]:
    """The JSON lines at the end of a file; a line cut by the window is dropped."""
    try:
        size = file.stat().st_size
        with file.open("rb") as handle:
            cut = size > limit
            if cut:
                handle.seek(size - limit)
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()[1:] if cut else raw.splitlines()
    return _parsed(lines)


def head_lines(file: Path, limit: int = READ_HEAD_BYTES) -> list[JsonValue]:
    """The JSON lines at the start of a file; the line cut by the window is dropped."""
    try:
        with file.open("rb") as handle:
            raw = handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if len(raw) >= limit and lines:
        lines = lines[:-1]
    return _parsed(lines)


def _parsed(lines: list[str]) -> list[JsonValue]:
    found: list[JsonValue] = []
    for line in lines:
        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return found
