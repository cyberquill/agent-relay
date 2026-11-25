"""Cursor chats: the plain-JSONL transcripts under `~/.cursor/projects/<slug>/agent-transcripts`.

The slug is the workspace path with every run of other characters turned into one `-`, so a folder
is found by slugging the registered workspaces, not by reversing a slug. A chat the Cursor CLI
keeps in `~/.cursor/chats` is a `cursor_cli` chat (the phone can load it over ACP); every other one
is a Cursor desktop chat, which only its hooks reach. Titles come from the first prompt: the app's
own chat names live in a database that can pass 10 GB, which this never opens.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core.chats import text as t
from agent_relay.core.chats.model import DeskChat
from agent_relay.hook import transcript
from agent_relay.models.events import Harness

RUNNING_WINDOW_S = 120.0
_SLUG = re.compile(r"[^A-Za-z0-9]+")


def slug(path: Path) -> str:
    return _SLUG.sub("-", str(path)).strip("-")


def _parts(entry: JsonValue) -> list[JsonValue]:
    message = entry.get("message") if isinstance(entry, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _text(entry: JsonValue) -> str:
    pieces = [p.get("text") for p in _parts(entry) if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(p for p in pieces if isinstance(p, str))


def _role(entry: JsonValue) -> str | None:
    role = entry.get("role") if isinstance(entry, dict) else None
    return role if isinstance(role, str) else None


def _settled(entries: list[JsonValue]) -> bool:
    """The chat ends on an assistant message with no tool call after its text."""
    if not entries or _role(entries[-1]) != "assistant":
        return False
    parts = _parts(entries[-1])
    return bool(parts) and isinstance(parts[-1], dict) and parts[-1].get("type") == "text"


def _chat(file: Path, workspace: str, app: Harness, now: float) -> DeskChat | None:
    try:
        modified = file.stat().st_mtime
    except OSError:
        return None
    head = t.head_lines(file)
    tail = t.tail_lines(file)
    first = next((t.user_text(_text(e)) for e in head if _role(e) == "user" and _text(e)), "")
    last = next((t.user_text(_text(e)) for e in reversed(tail) if _role(e) == "user" and _text(e)), "")
    return DeskChat(
        app=app,
        native_id=file.stem,
        workspace=workspace,
        title=t.safe(t.short(first or last, t.TITLE_CHARS)) or "(no prompt)",
        modified=datetime.fromtimestamp(modified, UTC),
        last_prompt=t.safe(last) or None,
        last_reply=t.safe(transcript.last_reply(str(file))),
        running=now - modified < RUNNING_WINDOW_S and not _settled(tail),
        transcript=str(file),
    )


def read(since: float, roots: Sequence[Path], now: float) -> list[DeskChat]:
    base = paths.cursor_projects()
    if not base.is_dir():
        return []
    workspaces = {slug(root): str(root) for root in roots}
    cli_ids = {p.name for p in paths.cursor_cli_chats().glob("*/*") if p.is_dir()}
    found: list[DeskChat] = []
    for project in base.iterdir():
        for file in project.glob("agent-transcripts/*/*.jsonl"):
            try:
                if file.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            app = Harness.CURSOR_CLI if file.stem in cli_ids else Harness.CURSOR
            chat = _chat(file, workspaces.get(project.name, ""), app, now)
            if chat is not None:
                found.append(chat)
    return found
