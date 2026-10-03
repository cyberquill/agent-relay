"""What command handlers write to: the Webex renderer, or the PWA hub."""

from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue

from agent_relay.models.sessions import SessionRecord

type Attachment = tuple[str, bytes, str]


class Surface(Protocol):
    async def post_in_thread(
        self,
        session: SessionRecord,
        markdown: str,
        card: dict[str, JsonValue] | None = None,
        file: Attachment | None = None,
    ) -> object: ...

    async def post_top(self, markdown: str, card: dict[str, JsonValue] | None = None) -> object: ...

    async def refresh_root(self, session: SessionRecord) -> None: ...

    async def thread_for(self, session: SessionRecord) -> str: ...
