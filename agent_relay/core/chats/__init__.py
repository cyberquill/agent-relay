"""Desk chats found in each app's own store: what the phone can pick up with `/chats`."""

from __future__ import annotations

from agent_relay.core.chats.catalog import (
    APP_WORDS,
    DEFAULT_DAYS,
    LISTED,
    MAX_DAYS,
    ChatCatalog,
    ago,
)
from agent_relay.core.chats.model import ChatPage, DeskChat, TakeMode

__all__ = [
    "APP_WORDS",
    "DEFAULT_DAYS",
    "LISTED",
    "MAX_DAYS",
    "ChatCatalog",
    "ChatPage",
    "DeskChat",
    "TakeMode",
    "ago",
]
