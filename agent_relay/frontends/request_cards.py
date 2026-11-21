"""The question card for a request, with its one-use nonce, as both front ends send it.

It returns `(markdown, card)`. The markdown line is what a phone notification shows instead of
"shared a card", so it carries the question.
"""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.core.registry import Registry
from agent_relay.frontends import card_state
from agent_relay.frontends.webex import cards
from agent_relay.hook.shims import abbreviate
from agent_relay.models.events import QuestionAsked
from agent_relay.models.sessions import SessionRecord

QUESTION_CHARS = 200


async def question_card(
    registry: Registry, session: SessionRecord, event: QuestionAsked
) -> tuple[str, dict[str, JsonValue]]:
    markdown = f"❓ Question: {abbreviate(' '.join(event.question.split()), QUESTION_CHARS)}"
    await card_state.store_offer(registry, event.request_id, event.options)
    nonce = await card_state.issue_nonce(registry)
    return markdown, cards.question(
        event.request_id, cards.session_label(session), event.question, event.options, nonce=nonce
    )
