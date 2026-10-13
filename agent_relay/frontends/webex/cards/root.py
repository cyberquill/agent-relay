"""The thread's root message: one status line per session."""

from __future__ import annotations

from agent_relay.models.sessions import SessionRecord

STATUS_ICON = {"active": "🟢", "running": "⏳", "idle": "💤", "parked": "🅿️", "ended": "⚫"}


def session_label(session: SessionRecord) -> str:
    workspace = session.workspace or "?"
    name = workspace.rstrip("/").rsplit("/", 1)[-1] or workspace
    return f"{session.harness.value} · {name} · {session.model or 'default model'}"


def root_markdown(session: SessionRecord) -> str:
    icon = STATUS_ICON.get(session.status.value, "")
    title = f"\n{session.title}" if session.title else ""
    return f"**{session_label(session)} · {icon} {session.status.value}**{title}"
