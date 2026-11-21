"""Adaptive Cards v1.3 builders. Every submit carries `relay` (the action) so replies route cleanly,
and `nonce` (one use)."""

from __future__ import annotations

from agent_relay.frontends.webex.cards.common import (
    CARD_LIMIT_BYTES,
    MAX_ANSWER_CHARS,
    MAX_CHOICES,
    MAX_PROMPT_CHARS,
    SCHEMA,
    VERSION,
    card_fallback,
    duration_text,
    fit_card,
    footer_line,
    is_hidden_model,
    message_bytes,
    minify,
)
from agent_relay.frontends.webex.cards.dashboard import OPS as DASHBOARD_OPS
from agent_relay.frontends.webex.cards.dashboard import dashboard
from agent_relay.frontends.webex.cards.model_switch import model_switch
from agent_relay.frontends.webex.cards.new_session import AGENT_LIMIT, attached_line, new_session
from agent_relay.frontends.webex.cards.panic_confirm import CONFIRM_WINDOW_S, panic_confirm
from agent_relay.frontends.webex.cards.question import question
from agent_relay.frontends.webex.cards.root import AWAY_ICON, STATUS_ICON, root_markdown, session_label

__all__ = [
    "AGENT_LIMIT",
    "AWAY_ICON",
    "CARD_LIMIT_BYTES",
    "CONFIRM_WINDOW_S",
    "DASHBOARD_OPS",
    "MAX_ANSWER_CHARS",
    "MAX_CHOICES",
    "MAX_PROMPT_CHARS",
    "SCHEMA",
    "STATUS_ICON",
    "VERSION",
    "attached_line",
    "card_fallback",
    "dashboard",
    "duration_text",
    "fit_card",
    "footer_line",
    "is_hidden_model",
    "message_bytes",
    "minify",
    "model_switch",
    "new_session",
    "panic_confirm",
    "question",
    "root_markdown",
    "session_label",
]
