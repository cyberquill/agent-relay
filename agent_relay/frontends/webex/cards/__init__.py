"""Adaptive Cards v1.3 builders. Every submit carries `relay` (the action) so replies route cleanly,
and `nonce` (one use)."""

from __future__ import annotations

from agent_relay.frontends.webex.cards.approval import approval
from agent_relay.frontends.webex.cards.choose import choose
from agent_relay.frontends.webex.cards.common import (
    CARD_LIMIT_BYTES,
    MAX_ANSWER_CHARS,
    MAX_CHOICES,
    MAX_PROMPT_CHARS,
    SCHEMA,
    VERSION,
    card_fallback,
    carry_fields,
    fit_card,
    message_bytes,
    minify,
)
from agent_relay.frontends.webex.cards.prompt import prompt
from agent_relay.frontends.webex.cards.question import question
from agent_relay.frontends.webex.cards.root import STATUS_ICON, root_markdown, session_label

__all__ = [
    "CARD_LIMIT_BYTES",
    "MAX_ANSWER_CHARS",
    "MAX_CHOICES",
    "MAX_PROMPT_CHARS",
    "SCHEMA",
    "STATUS_ICON",
    "VERSION",
    "approval",
    "card_fallback",
    "carry_fields",
    "choose",
    "fit_card",
    "message_bytes",
    "minify",
    "prompt",
    "question",
    "root_markdown",
    "session_label",
]
