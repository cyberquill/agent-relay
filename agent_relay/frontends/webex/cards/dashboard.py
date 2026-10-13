"""`/sessions` as a card: pick one session, then Status, Diff, Stop, Back or Away on it."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import JsonValue

from agent_relay.frontends.apps import app_emoji
from agent_relay.frontends.webex.cards.common import MAX_CHOICES, card, choice_set, submit, text
from agent_relay.frontends.webex.cards.root import STATUS_ICON
from agent_relay.models.sessions import SessionRecord

OPS = ("status", "diff", "stop", "back", "away")


def session_choice(session: SessionRecord) -> tuple[str, str]:
    workspace = session.workspace or "?"
    name = session.title or workspace.rstrip("/").rsplit("/", 1)[-1] or workspace
    icon = STATUS_ICON.get(session.status.value, "")
    return session.session_key, f"{icon} {app_emoji(session.harness)} {name}".strip()


def dashboard(sessions: Sequence[SessionRecord], *, nonce: str) -> dict[str, JsonValue]:
    choices = [session_choice(s) for s in sessions]
    body: list[JsonValue] = [
        text(f"Sessions ({len(sessions)})", weight="Bolder"),
        choice_set("session", choices, "compact", choices[0][0] if choices else None),
    ]
    if len(choices) > MAX_CHOICES:
        body.append(
            text(f"Showing {MAX_CHOICES} of {len(choices)}. Reply in a thread for the rest.", subtle=True)
        )
    actions: list[JsonValue] = [
        submit(
            op.capitalize(),
            {"relay": "sessions", "op": op, "nonce": nonce},
            "destructive" if op == "stop" else None,
        )
        for op in OPS
    ]
    return card(body, actions)
