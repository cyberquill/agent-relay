"""Adaptive Cards v1.3 builders. Every submit carries `relay` (the action) so replies route cleanly."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.models.sessions import SessionRecord

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
VERSION = "1.3"
STATUS_ICON = {"active": "🟢", "running": "⏳", "idle": "💤", "parked": "🅿️", "ended": "⚫"}


def _card(body: list[JsonValue], actions: list[JsonValue]) -> dict[str, JsonValue]:
    return {"type": "AdaptiveCard", "$schema": SCHEMA, "version": VERSION, "body": body, "actions": actions}


def _text(text: str, *, weight: str | None = None, wrap: bool = True, subtle: bool = False) -> JsonValue:
    block: dict[str, JsonValue] = {"type": "TextBlock", "text": text, "wrap": wrap}
    if weight:
        block["weight"] = weight
    if subtle:
        block["isSubtle"] = True
    return block


def _submit(title: str, data: dict[str, JsonValue], style: str | None = None) -> JsonValue:
    action: dict[str, JsonValue] = {"type": "Action.Submit", "title": title, "data": data}
    if style:
        action["style"] = style
    return action


def approval(request_id: str, session_label: str, detail: str, deadline_min: int) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {"relay": "approval", "request_id": request_id}
    return _card(
        [
            _text("Approval needed", weight="Bolder"),
            _text(session_label, subtle=True),
            _text(detail),
            _text(f"Denied automatically in {deadline_min} min.", subtle=True),
        ],
        [
            _submit("Allow", {**base, "decision": "allow"}, "positive"),
            _submit("Allow for session", {**base, "decision": "allow_session"}),
            _submit("Deny", {**base, "decision": "deny"}, "destructive"),
        ],
    )


def question(
    request_id: str, session_label: str, text: str, options: tuple[str, ...]
) -> dict[str, JsonValue]:
    body: list[JsonValue] = [
        _text("Question", weight="Bolder"),
        _text(session_label, subtle=True),
        _text(text),
    ]
    if options:
        body.append(
            {
                "type": "Input.ChoiceSet",
                "id": "choice",
                "style": "expanded",
                "choices": [{"title": o, "value": o} for o in options],
            }
        )
    body.append(
        {"type": "Input.Text", "id": "custom", "isMultiline": True, "placeholder": "Or type an answer"}
    )
    base: dict[str, JsonValue] = {"relay": "question", "request_id": request_id}
    return _card(
        body,
        [_submit("Answer", {**base, "op": "answer"}, "positive"), _submit("Skip", {**base, "op": "skip"})],
    )


def choose(
    step: str, title: str, choices: list[tuple[str, str]], carry: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """One `/new` wizard step: pick an app, a workspace or a model."""
    return _card(
        [
            _text(title, weight="Bolder"),
            {
                "type": "Input.ChoiceSet",
                "id": "value",
                "style": "compact",
                "value": choices[0][0] if choices else "",
                "choices": [{"title": label, "value": value} for value, label in choices],
            },
        ],
        [_submit("Next", {"relay": "new", "step": step, **carry}, "positive")],
    )


def prompt(carry: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return _card(
        [
            _text("What should it do?", weight="Bolder"),
            {"type": "Input.Text", "id": "value", "isMultiline": True, "placeholder": "Prompt"},
        ],
        [_submit("Start", {"relay": "new", "step": "prompt", **carry}, "positive")],
    )


def session_label(session: SessionRecord) -> str:
    workspace = session.workspace or "?"
    name = workspace.rstrip("/").rsplit("/", 1)[-1] or workspace
    return f"{session.harness.value} · {name} · {session.model or 'default model'}"


def root_markdown(session: SessionRecord) -> str:
    icon = STATUS_ICON.get(session.status.value, "")
    title = f"\n{session.title}" if session.title else ""
    return f"**{session_label(session)} · {icon} {session.status.value}**{title}"


def card_fallback(card: dict[str, JsonValue]) -> str:
    """Text shown by clients that cannot render the card (and in notifications)."""
    body = card.get("body")
    if isinstance(body, list):
        for block in body:
            if (
                isinstance(block, dict)
                and block.get("type") == "TextBlock"
                and isinstance(block.get("text"), str)
            ):
                return str(block["text"])
    return "agent-relay"
