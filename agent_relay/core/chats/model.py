"""A desk chat as the phone sees it in the `/chats` list."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agent_relay.models.events import Harness

# How the phone carries on with a chat:
#   drive    the app's own API addresses the chat by id, so the next turn is sent from the phone
#   arm      the chat is running at the desk: its Stop hook parks it and the phone's reply continues it
#   handoff  a new phone session in the same workspace that reads the chat's transcript
type TakeMode = Literal["drive", "arm", "handoff"]


class DeskChat(BaseModel):
    """One chat found in an app's own store. `app` is the harness whose hooks the chat reports as."""

    model_config = ConfigDict(frozen=True)

    app: Harness
    native_id: str
    workspace: str
    title: str
    modified: datetime
    last_prompt: str | None = None
    last_reply: str | None = None
    running: bool = False
    # Where the transcript lives (for a handoff to read); empty when the app keeps it in a database.
    transcript: str = ""

    @property
    def ref(self) -> str:
        """The offer entry for this chat: stable across two reads of the store."""
        return f"{self.app.value}:{self.native_id}"


class ChatPage(BaseModel):
    """What `/chats` shows: the newest chats first, and how many the filters left out."""

    model_config = ConfigDict(frozen=True)

    chats: list[DeskChat]
    outside_workspaces: int = 0
    days: int = 7
    more: int = 0
