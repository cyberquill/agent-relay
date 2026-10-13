"""The approval and question cards for a request, with their one-use nonce, as both front ends send them.

Each returns `(markdown, card)`. The markdown line is what a phone notification shows instead of
"shared a card", so it carries the tool or the question.
"""

from __future__ import annotations

import math

from pydantic import JsonValue

from agent_relay.core.registry import Registry
from agent_relay.frontends import card_state
from agent_relay.frontends.webex import cards
from agent_relay.hook.shims import abbreviate
from agent_relay.models.events import ApprovalRequested, QuestionAsked, utcnow
from agent_relay.models.sessions import SessionRecord

TOOL_CHARS = 80
QUESTION_CHARS = 200
DETAIL_CHARS = 1_500


def remaining_text(seconds: float) -> str:
    whole = max(0, math.ceil(seconds))
    if whole < 60:
        return f"{whole}s"
    hours, minutes = divmod(math.ceil(whole / 60), 60)
    if hours == 0:
        return f"{minutes}m"
    return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"


async def _seconds_left(registry: Registry, request_id: str, default_s: int) -> float:
    request = await registry.get_pending(request_id)
    if request is None:
        return float(default_s)
    return (request.deadline - utcnow()).total_seconds()


async def approval_card(
    registry: Registry, session: SessionRecord, event: ApprovalRequested, default_deadline_s: int
) -> tuple[str, dict[str, JsonValue]]:
    left = remaining_text(await _seconds_left(registry, event.request_id, default_deadline_s))
    markdown = f"🔐 Approval needed: {abbreviate(event.tool or 'a tool', TOOL_CHARS)} · auto-deny in {left}"
    detail = abbreviate(event.detail or event.tool, DETAIL_CHARS)
    nonce = await card_state.issue_nonce(registry)
    return markdown, cards.approval(event.request_id, cards.session_label(session), detail, left, nonce=nonce)


async def question_card(
    registry: Registry, session: SessionRecord, event: QuestionAsked
) -> tuple[str, dict[str, JsonValue]]:
    markdown = f"❓ Question: {abbreviate(' '.join(event.question.split()), QUESTION_CHARS)}"
    await card_state.store_offer(registry, event.request_id, event.options)
    nonce = await card_state.issue_nonce(registry)
    return markdown, cards.question(
        event.request_id, cards.session_label(session), event.question, event.options, nonce=nonce
    )
