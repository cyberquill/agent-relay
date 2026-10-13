"""The approval card: allow once, for the session or the workspace, deny, or deny with a reason."""

from __future__ import annotations

import math

from pydantic import JsonValue

from agent_relay.core.service.approvals import REASON_MAX_CHARS
from agent_relay.frontends.webex.cards.common import SCHEMA, VERSION, card, submit, text
from agent_relay.models.config import ApprovalConfig

DEFAULT_GRANT_S = ApprovalConfig().workspace_grant_s


def grant_label(seconds: int) -> str:
    """`4 h`, `1 h 30 min` or `45 min`: the length of a workspace grant for a button title."""
    minutes = max(1, math.ceil(seconds / 60))
    hours, rest = divmod(minutes, 60)
    if hours == 0:
        return f"{rest} min"
    return f"{hours} h" if rest == 0 else f"{hours} h {rest} min"


def _deny_with_reason(base: dict[str, JsonValue]) -> JsonValue:
    reason: dict[str, JsonValue] = {
        "type": "Input.Text",
        "id": "reason",
        "isMultiline": True,
        "placeholder": "Tell the agent why (optional)",
        "maxLength": REASON_MAX_CHARS,
    }
    sub: dict[str, JsonValue] = {
        "type": "AdaptiveCard",
        "$schema": SCHEMA,
        "version": VERSION,
        "body": [reason],
        "actions": [submit("Deny", {**base, "decision": "deny"}, "destructive")],
    }
    return {"type": "Action.ShowCard", "title": "Deny with reason…", "card": sub}


def approval(
    request_id: str,
    session_label: str,
    detail: str,
    deadline: str,
    *,
    nonce: str,
    grant_s: int = DEFAULT_GRANT_S,
) -> dict[str, JsonValue]:
    """`deadline` is the time left (`10m`); `detail` is monospace so markdown in it stays literal."""
    base: dict[str, JsonValue] = {"relay": "approval", "request_id": request_id, "nonce": nonce}
    return card(
        [
            text("Approval needed", weight="Bolder"),
            text(session_label, subtle=True),
            text(detail, mono=True),
            text(f"Auto-deny in {deadline}.", subtle=True),
        ],
        [
            submit("Allow once", {**base, "decision": "allow"}, "positive"),
            submit("Allow for this session", {**base, "decision": "allow_session"}),
            submit(
                f"Allow this tool in this workspace for {grant_label(grant_s)}",
                {**base, "decision": "allow_workspace"},
            ),
            submit("Deny", {**base, "decision": "deny"}, "destructive"),
            _deny_with_reason(base),
        ],
    )
