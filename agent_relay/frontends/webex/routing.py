"""Deliver each session's events and posts through the Webex bot that owns its thread."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.core.registry import Registry
from agent_relay.frontends.surface import Attachment
from agent_relay.frontends.webex.api import Message
from agent_relay.frontends.webex.bots import bot_for_harness
from agent_relay.frontends.webex.renderer import WebexRenderer
from agent_relay.models.config import CONTROL, BotName
from agent_relay.models.events import Event
from agent_relay.models.sessions import SessionRecord


class BotRenderers:
    """One WebexRenderer per bot. As an event sink and a Surface it forwards to the session's own bot.

    A thread belongs to the space it was created in, so a session that has one keeps its bot: the one
    recorded on it, or the control bot for a thread made before there were several. A session with no
    thread yet goes to the bot that serves its app, or the control bot when that app has none.
    """

    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._renderers: dict[BotName, WebexRenderer] = {}

    def add(self, renderer: WebexRenderer) -> None:
        self._renderers[renderer.bot] = renderer

    @property
    def bots(self) -> list[BotName]:
        return list(self._renderers)

    def owner(self, session: SessionRecord) -> BotName:
        for bot in self._renderers:
            if bot == session.webex_bot:
                return bot
        if session.webex_thread_id:
            return CONTROL
        return bot_for_harness(session.harness, self._renderers)

    def renderer(self, bot: BotName) -> WebexRenderer:
        return self._renderers.get(bot) or self._renderers[CONTROL]

    def for_session(self, session: SessionRecord) -> WebexRenderer:
        return self.renderer(self.owner(session))

    async def handle(self, event: Event) -> None:
        session = await self._registry.get_session(event.session_key)
        target = self.for_session(session) if session is not None else self.renderer(CONTROL)
        await target.handle(event)

    async def post_in_thread(
        self,
        session: SessionRecord,
        markdown: str,
        card: dict[str, JsonValue] | None = None,
        file: Attachment | None = None,
    ) -> Message:
        return await self.for_session(session).post_in_thread(session, markdown, card=card, file=file)

    async def post_top(self, markdown: str, card: dict[str, JsonValue] | None = None) -> Message:
        return await self.renderer(CONTROL).post_top(markdown, card)

    async def refresh_root(self, session: SessionRecord) -> None:
        await self.for_session(session).refresh_root(session)

    async def thread_for(self, session: SessionRecord) -> str:
        return await self.for_session(session).thread_for(session)
