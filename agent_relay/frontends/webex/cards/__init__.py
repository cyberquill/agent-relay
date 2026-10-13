"""Adaptive Cards v1.3 builders. Every submit carries `relay` (the action) so replies route cleanly,
and `nonce` (one use)."""

from __future__ import annotations

from agent_relay.frontends.webex.cards.approval import approval
from agent_relay.frontends.webex.cards.away_scope import ALL_WORKSPACES, away_scope
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
    is_hidden_model,
    message_bytes,
    minify,
)
from agent_relay.frontends.webex.cards.dashboard import OPS as DASHBOARD_OPS
from agent_relay.frontends.webex.cards.dashboard import dashboard
from agent_relay.frontends.webex.cards.model_switch import model_switch
from agent_relay.frontends.webex.cards.new_session import AppOption, new_details, new_model, new_session
from agent_relay.frontends.webex.cards.panic_confirm import CONFIRM_WINDOW_S, panic_confirm
from agent_relay.frontends.webex.cards.prompt import prompt
from agent_relay.frontends.webex.cards.question import question
from agent_relay.frontends.webex.cards.root import STATUS_ICON, root_markdown, session_label

__all__ = [
    "ALL_WORKSPACES",
    "CARD_LIMIT_BYTES",
    "CONFIRM_WINDOW_S",
    "DASHBOARD_OPS",
    "MAX_ANSWER_CHARS",
    "MAX_CHOICES",
    "MAX_PROMPT_CHARS",
    "SCHEMA",
    "STATUS_ICON",
    "VERSION",
    "AppOption",
    "approval",
    "away_scope",
    "card_fallback",
    "carry_fields",
    "choose",
    "dashboard",
    "fit_card",
    "is_hidden_model",
    "message_bytes",
    "minify",
    "model_switch",
    "new_details",
    "new_model",
    "new_session",
    "panic_confirm",
    "prompt",
    "question",
    "root_markdown",
    "session_label",
]
