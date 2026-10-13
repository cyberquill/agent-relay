"""/digest: every session in one message, grouped by what needs you."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from agent_relay.frontends.apps import app_emoji
from agent_relay.frontends.commands import Command
from agent_relay.frontends.registry import CommandSpec, register
from agent_relay.hook.shims import abbreviate
from agent_relay.models.events import utcnow
from agent_relay.models.sessions import SessionRecord, SessionStatus

if TYPE_CHECKING:
    from agent_relay.frontends.webex.handlers import Context, Handlers

RECENT = timedelta(hours=24)
PER_SECTION = 8
TITLE_CHARS = 60
NAME_CHARS = 40


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)}m ago"
    return f"{minutes // 60}h ago"


def _line(session: SessionRecord, note: str = "") -> str:
    workspace = (session.workspace or "?").rstrip("/").rsplit("/", 1)[-1] or "?"
    title = f" · {abbreviate(' '.join(session.title.split()), TITLE_CHARS)}" if session.title else ""
    where = f"{session.harness.value} · {abbreviate(workspace, NAME_CHARS)}"
    return f"- {app_emoji(session.harness)} {where}{title}{note}"


def _section(name: str, icon: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    shown = lines[:PER_SECTION]
    if len(lines) > PER_SECTION:
        shown.append(f"- … and {len(lines) - PER_SECTION} more")
    return [f"**{icon} {name} ({len(lines)})**", *shown, ""]


async def _digest(h: Handlers, ctx: Context, cmd: Command) -> None:
    registry = h.service.registry
    waiting: dict[str, list[str]] = {}
    for request in await registry.open_pending():
        waiting.setdefault(request.session_key, []).append(request.kind)
    now = utcnow()
    groups: dict[str, list[str]] = {"blocked": [], "running": [], "parked": [], "idle": [], "ended": []}
    for session in await registry.list_sessions(include_ended=True):
        kinds = waiting.get(session.session_key)
        if kinds:
            parts = [
                f"{n} open {word}{'' if n == 1 else 's'}"
                for n, word in ((kinds.count("approval"), "approval"), (kinds.count("question"), "question"))
                if n
            ]
            groups["blocked"].append(_line(session, f" — {', '.join(parts)}"))
        elif session.status is SessionStatus.RUNNING:
            groups["running"].append(_line(session))
        elif session.status is SessionStatus.PARKED:
            groups["parked"].append(_line(session))
        elif session.status is SessionStatus.ENDED:
            if now - session.updated <= RECENT:
                groups["ended"].append(_line(session, f" — ended {_ago(now - session.updated)}"))
        else:
            groups["idle"].append(_line(session))
    lines = [
        *_section("Blocked", "🔐", groups["blocked"]),
        *_section("Running", "⏳", groups["running"]),
        *_section("Parked", "🅿️", groups["parked"]),
        *_section("Idle", "💤", groups["idle"]),
        *_section("Recently ended", "⚫", groups["ended"]),
    ]
    await h.reply(ctx, "\n".join(lines).strip() or "No sessions right now.")


register(
    CommandSpec(
        "digest",
        _digest,
        "sessions grouped by blocked, running, parked, idle and recently ended",
        scope="top",
        max_args=0,
        order=19,
    )
)
