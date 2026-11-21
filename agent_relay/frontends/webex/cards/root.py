"""The thread's root message: one status line per session, edited silently as the session moves."""

from __future__ import annotations

from agent_relay.frontends.apps import app_emoji
from agent_relay.models.sessions import SessionRecord

STATUS_ICON = {"active": "▶️", "running": "⏳", "idle": "✅", "parked": "🅿️", "ended": "⚫"}
AWAY_ICON = "🏝️"


def session_label(session: SessionRecord) -> str:
    workspace = session.workspace or "?"
    name = workspace.rstrip("/").rsplit("/", 1)[-1] or workspace
    return f"{session.harness.value} · {name} · {session.model or 'default model'}"


def root_markdown(
    session: SessionRecord, queued: int = 0, queue_mode: str = "queue", away: bool = False
) -> str:
    """The status line; `queued` messages waiting add `N queued · <mode>`, away mode adds 🏝️."""
    icon = STATUS_ICON.get(session.status.value, "")
    title = f"\n{session.title}" if session.title else ""
    waiting = f" · {queued} queued · {queue_mode}" if queued else ""
    island = f" · {AWAY_ICON} away" if away else ""
    status = f"{session_label(session)} · {icon} {session.status.value}{island}{waiting}"
    return f"{app_emoji(session.harness)} **{status}**{title}"
