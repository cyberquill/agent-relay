"""`/chats` as a card: one block per desk chat with a Continue button; the numbers match `/takeover N`."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.core.chats import ChatPage, DeskChat, ago
from agent_relay.core.chats import text as chat_text
from agent_relay.frontends.apps import app_emoji
from agent_relay.frontends.webex.cards.common import action_set, card, submit, text

RUNNING_ICON = "⏳"


def heading(chat: DeskChat, number: int) -> str:
    folder = chat.workspace.rstrip("/").rsplit("/", 1)[-1] or chat.workspace
    running = f" · {RUNNING_ICON} running" if chat.running else ""
    return f"{number}. {app_emoji(chat.app)} {folder} · {ago(chat.modified)}{running}"


def snippet(chat: DeskChat) -> str:
    """The last message, first and last 100 characters, so the right chat is easy to tell apart."""
    return chat_text.ends(chat.last_reply) if chat.last_reply else ""


def prompt_line(chat: DeskChat) -> str:
    return f"You: {chat_text.short(chat.last_prompt, chat_text.PROMPT_CHARS)}" if chat.last_prompt else ""


def footer(page: ChatPage) -> str:
    notes: list[str] = []
    if page.more:
        notes.append(f"{page.more} more: narrow with `/chats 2d`, `/chats cursor` or `/chats <word>`.")
    if page.outside_workspaces:
        notes.append(f"{page.outside_workspaces} in folders that are not workspaces (`/ws` adds one).")
    return " ".join(notes)


def chats(page: ChatPage, *, nonce: str) -> dict[str, JsonValue]:
    body: list[JsonValue] = [
        text(f"Desk chats · last {page.days} day{'' if page.days == 1 else 's'}", weight="Bolder")
    ]
    for number, chat in enumerate(page.chats, 1):
        lines: list[JsonValue] = [text(heading(chat, number), weight="Bolder"), text(chat.title)]
        for line in (prompt_line(chat), snippet(chat)):
            if line:
                lines.append(text(line, subtle=True))
        lines.append(
            action_set([submit("Continue", {"relay": "chats", "pick": str(number), "nonce": nonce})])
        )
        body.append({"type": "Container", "separator": True, "spacing": "Medium", "items": lines})
    if note := footer(page):
        body.append(text(note, subtle=True))
    return card(body, [])
