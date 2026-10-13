"""Fan-out to PWA browsers: raw adapter events plus the replies the command handlers post.

`PwaHub` is a router sink (every event, including token deltas) and a `Surface` (what `Handlers`
write to), so the PWA runs exactly the Webex command set. Every payload is scrubbed before it leaves.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from pydantic import JsonValue

from agent_relay.core.registry import Registry
from agent_relay.frontends import card_state, request_cards
from agent_relay.frontends.surface import Attachment
from agent_relay.frontends.webex import cards
from agent_relay.log import get_logger
from agent_relay.models.events import ApprovalRequested, Event, QuestionAnswered, QuestionAsked
from agent_relay.models.sessions import SessionRecord
from agent_relay.scrub import Scrubbable, scrub_obj

log = get_logger(__name__)

QUEUE_SIZE = 1_000
KEEPALIVE_S = 15.0

type Payload = dict[str, JsonValue]


def _clean(payload: Payload) -> Payload:
    return cast(Payload, scrub_obj(cast(Scrubbable, payload)))


def sse(payload: Payload) -> bytes:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


class PwaHub:
    def __init__(
        self, queue_size: int = QUEUE_SIZE, registry: Registry | None = None, approval_deadline_s: int = 600
    ) -> None:
        self._clients: set[asyncio.Queue[Payload]] = set()
        self._queue_size = queue_size
        self._registry = registry
        self._approval_deadline_s = approval_deadline_s

    @property
    def clients(self) -> int:
        return len(self._clients)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Payload]]:
        queue: asyncio.Queue[Payload] = asyncio.Queue(self._queue_size)
        self._clients.add(queue)
        try:
            yield queue
        finally:
            self._clients.discard(queue)

    def broadcast(self, payload: Payload) -> None:
        cleaned = _clean(payload)
        for queue in list(self._clients):
            if queue.full():
                queue.get_nowait()
                log.warning("a PWA client fell behind; dropped its oldest event")
            queue.put_nowait(cleaned)

    # router sink

    async def handle(self, event: Event) -> None:
        self.broadcast({"kind": "event", "event": event.model_dump(mode="json")})
        await self._request_card(event)

    async def _request_card(self, event: Event) -> None:
        """Approvals and questions reach the browser as server-built cards, with a one-use nonce."""
        registry = self._registry
        if registry is None:
            return
        if isinstance(event, QuestionAnswered):
            await card_state.drop_offer(registry, event.request_id)
            return
        if not isinstance(event, ApprovalRequested | QuestionAsked):
            return
        session = await registry.get_session(event.session_key)
        if session is None:
            return
        if isinstance(event, ApprovalRequested):
            markdown, card = await request_cards.approval_card(
                registry, session, event, self._approval_deadline_s
            )
        else:
            markdown, card = await request_cards.question_card(registry, session, event)
        self.broadcast(_message(session.session_key, markdown, card, None))

    # Surface

    async def post_in_thread(
        self,
        session: SessionRecord,
        markdown: str,
        card: dict[str, JsonValue] | None = None,
        file: Attachment | None = None,
    ) -> None:
        self.broadcast(_message(session.session_key, markdown, card, file))

    async def post_top(self, markdown: str, card: dict[str, JsonValue] | None = None) -> None:
        self.broadcast(_message(None, markdown, card, None))

    async def refresh_root(self, session: SessionRecord) -> None:
        self.broadcast(
            {"kind": "session", "session": session.session_key, "root": cards.root_markdown(session)}
        )

    async def thread_for(self, session: SessionRecord) -> str:
        await self.refresh_root(session)
        return session.session_key


def _message(
    session: str | None, markdown: str, card: dict[str, JsonValue] | None, file: Attachment | None
) -> Payload:
    payload: Payload = {"kind": "message", "session": session, "markdown": markdown, "card": card}
    if file is not None:
        name, body, mime = file
        payload["file"] = {"name": name, "mime": mime, "text": body.decode(errors="replace")}
    return payload
