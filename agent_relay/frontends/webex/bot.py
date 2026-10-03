"""Webex front end: websocket activities in, access checks, exactly-once processing, catch-up."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import httpx

from agent_relay.core.registry import Registry
from agent_relay.core.service import RelayService
from agent_relay.frontends import actions
from agent_relay.frontends.webex.access import AccessControl
from agent_relay.frontends.webex.api import AttachmentAction, Message, WebexApi
from agent_relay.frontends.webex.handlers import Context, Handlers
from agent_relay.frontends.webex.renderer import Outbox, WebexRenderer
from agent_relay.frontends.webex.ws_client import Activity, Connector, WebexWsClient, default_connector
from agent_relay.log import get_logger
from agent_relay.models.config import RelayConfig

log = get_logger(__name__)

LAST_SEEN_KEY = "webex.last_seen"
SEEN_PREFIX = "webex.seen:"
CATCH_UP_PAGES = 10


class WebexFrontend:
    def __init__(
        self,
        api: WebexApi,
        registry: Registry,
        service: RelayService,
        renderer: WebexRenderer,
        handlers: Handlers,
        access: AccessControl,
        bot_person_id: str,
    ) -> None:
        self.api = api
        self._registry = registry
        self._service = service
        self.renderer = renderer
        self.handlers = handlers
        self._access = access
        self._bot_id = bot_person_id
        self._lock = asyncio.Lock()

    # exactly-once bookkeeping

    async def _claim(self, message_id: str) -> bool:
        """Mark a message processed. Returns False if it was already handled."""
        async with self._lock:
            if await self._registry.kv_get(f"{SEEN_PREFIX}{message_id}") is not None:
                return False
            await self._registry.kv_set(f"{SEEN_PREFIX}{message_id}", "1")
            return True

    async def _remember_last(self, message: Message) -> None:
        await self._registry.kv_set(LAST_SEEN_KEY, message.id)

    # inbound

    async def on_activity(self, activity: Activity) -> None:
        if activity.verb == "cardAction":
            await self.handle_action(await self.api.get_attachment_action(activity.message_id))
        else:
            await self.handle_message(await self.api.get_message(activity.message_id))

    async def handle_message(self, message: Message) -> None:
        if message.person_id == self._bot_id:
            return
        if not await self._claim(message.id):
            log.debug("duplicate message ignored")
            return
        verdict = await self._access.check_message(message.person_id, message.person_email, message.room_type)
        if verdict.leave_room:
            with contextlib.suppress(Exception):
                await self.api.leave_room(message.room_id, self._bot_id)
        if not verdict.allowed:
            log.info(f"ignored message: {verdict.reason}")
            return
        await self._remember_last(message)
        await self.renderer.outbox.remember_room(message.room_id)
        session = await self._registry.session_by_thread(message.parent_id) if message.parent_id else None
        if message.parent_id and session is None:
            await self.renderer.post_top(
                "That thread is not linked to a session any more. `/sessions` lists them."
            )
            return
        await self.handlers.text(Context(session), message.text or message.markdown)

    async def handle_action(self, action: AttachmentAction) -> None:
        if not await self._claim(action.id):
            return
        room = await self.api.get_room(action.room_id)
        verdict = await self._access.check_action(action.person_id, room.type)
        if verdict.leave_room:
            with contextlib.suppress(Exception):
                await self.api.leave_room(action.room_id, self._bot_id)
        if not verdict.allowed:
            log.info(f"ignored card submission: {verdict.reason}")
            return
        outcome = await actions.submit(self._service, self.handlers, action.inputs)
        request_id = action.inputs.get("request_id")
        if outcome == "already_answered" and isinstance(request_id, str):
            await self._already_resolved(request_id)
        elif outcome == "ignored":
            log.info("ignored a card submission with no relay action")

    async def _already_resolved(self, request_id: str) -> None:
        request = await self._registry.get_pending(request_id)
        if request is None:
            return
        session = await self._registry.get_session(request.session_key)
        if session is not None:
            await self.renderer.post_in_thread(session, "That request was already answered.")

    # catch-up after (re)connect

    async def catch_up(self) -> int:
        """Process messages that arrived while disconnected, oldest first. Returns how many."""
        room = await self.renderer.outbox.room_id()
        last = await self._registry.kv_get(LAST_SEEN_KEY)
        if room is None or last is None:
            return 0
        missed: list[Message] = []
        before: str | None = None
        for _ in range(CATCH_UP_PAGES):
            page = await self.api.list_messages(room, 50, before)
            if not page:
                break
            for message in page:
                if message.id == last:
                    break
                missed.append(message)
            else:
                before = page[-1].id
                continue
            break
        handled = 0
        for message in reversed(missed):
            if message.person_id == self._bot_id:
                continue
            if await self._registry.kv_get(f"{SEEN_PREFIX}{message.id}") is not None:
                continue
            await self.handle_message(message)
            handled += 1
        if handled:
            log.info(f"caught up on {handled} message(s) sent while disconnected")
        return handled

    async def ticker(self, interval: float = 1.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.renderer.tick()
            except Exception:
                log.exception("renderer tick failed")


async def build_frontend(
    token: str,
    config: RelayConfig,
    registry: Registry,
    service: RelayService,
    http: httpx.AsyncClient | None = None,
    connector: Connector = default_connector,
    doctor: Callable[[], Awaitable[str]] | None = None,
) -> tuple[WebexFrontend, WebexWsClient]:
    client = http or httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    api = WebexApi(client, config.webex.api_base)
    me = await api.me()
    outbox = Outbox(api, registry, config.webex.allowed_email)
    renderer = WebexRenderer(outbox, registry, config.renderer, config.approvals.deadline_s)
    handlers = Handlers(service, renderer, doctor=doctor)
    access = AccessControl(registry, config.webex.allowed_email, me.id)
    frontend = WebexFrontend(api, registry, service, renderer, handlers, access, me.id)
    ws = WebexWsClient(
        token, client, config.webex.u2c_url, frontend.on_activity, frontend.catch_up, connector=connector
    )
    return frontend, ws
