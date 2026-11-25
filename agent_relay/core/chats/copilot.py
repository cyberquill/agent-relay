"""Copilot chats: `~/.copilot/session-state/<id>/` holds the Copilot CLI's sessions and those of the
VS Code Agent Host. `workspace.yaml` names the client, the folder and the title; `events.jsonl` is the
transcript (it can be tens of MB, so only its ends are read)."""

from __future__ import annotations

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
AGENT_HOST = "vscode-agent-host"
# The last event that is not hook or usage bookkeeping says whether the chat is mid-turn.
_BOOKKEEPING = ("hook.", "session.usage", "session.permissions", "session.model", "session.info")
_SETTLED = ("assistant.turn_end", "session.shutdown", "session.idle")


def read_yaml(file: Path) -> dict[str, str]:
    """The flat `key: value` lines of a `workspace.yaml`; nothing else is in it."""
    found: dict[str, str] = {}
    try:
        for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition(": ")
            if sep and key.isidentifier():
                found[key] = value.strip().strip("'\"")
    except OSError:
        return {}
    return found


def _type(event: JsonValue) -> str:
    kind = event.get("type") if isinstance(event, dict) else None
    return kind if isinstance(kind, str) else ""


def _content(event: JsonValue) -> str:
    data = event.get("data") if isinstance(event, dict) else None
    content = data.get("content") if isinstance(data, dict) else None
    return content if isinstance(content, str) else ""


def _settled(events: list[JsonValue]) -> bool:
    for event in reversed(events):
        kind = _type(event)
        if kind and not kind.startswith(_BOOKKEEPING):
            return kind in _SETTLED
    return False


def _name(meta: dict[str, str]) -> str:
    """The chat's name; a multi-line one is written as a YAML block (`|-`), which is no name here."""
    name = meta.get("name", "")
    return "" if name[:1] in ("|", ">") else name


def _modified(meta: dict[str, str], mtime: float) -> datetime:
    stamp = meta.get("updated_at", "")
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(mtime, UTC)


def _chat(folder: Path, since: float, now: float) -> DeskChat | None:
    events = folder / "events.jsonl"
    try:
        mtime = events.stat().st_mtime
    except OSError:
        return None
    if mtime < since:
        return None
    meta = read_yaml(folder / "workspace.yaml")
    cwd = meta.get("cwd", "")
    if not cwd:
        return None
    tail = t.tail_lines(events)
    first = next((t.user_text(_content(e)) for e in t.head_lines(events) if _type(e) == "user.message"), "")
    last = next((t.user_text(_content(e)) for e in reversed(tail) if _type(e) == "user.message"), "")
    host = meta.get("client_name") == AGENT_HOST or transcript.from_agent_host(str(events))
    return DeskChat(
        app=Harness.VSCODE_COPILOT if host else Harness.COPILOT_CLI,
        native_id=folder.name,
        workspace=cwd,
        title=t.safe(t.short(_name(meta) or first or last, t.TITLE_CHARS)) or "(no prompt)",
        modified=_modified(meta, mtime),
        last_prompt=t.safe(last) or None,
        last_reply=t.safe(transcript.last_reply(str(events))),
        running=now - mtime < RUNNING_WINDOW_S and not _settled(tail),
        transcript=str(events),
    )


def read(since: float, roots: Sequence[Path], now: float) -> list[DeskChat]:
    base = paths.copilot_session_state()
    if not base.is_dir():
        return []
    found: list[DeskChat] = []
    for folder in base.iterdir():
        chat = _chat(folder, since, now) if folder.is_dir() else None
        if chat is not None:
            found.append(chat)
    return found
