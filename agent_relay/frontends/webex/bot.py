"""Webex front end: websocket activities in, access checks, exactly-once processing, catch-up."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import httpx

from agent_relay.core.registry import SEEN_PREFIX, Registry
from agent_relay.core.service import RelayService
from agent_relay.frontends import actions, commands
from agent_relay.frontends.webex.access import AccessControl
from agent_relay.frontends.webex.api import AttachmentAction, Message, WebexApi, WebexError
from agent_relay.frontends.webex.handlers import Context, Handlers
from agent_relay.frontends.webex.renderer import Outbox, WebexRenderer
from agent_relay.frontends.webex.ws_client import Activity, Connector, WebexWsClient, default_connector
from agent_relay.log import get_logger, log_body
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import utcnow

log = get_logger(__name__)

LAST_SEEN_KEY = "webex.last_seen"
CATCH_UP_PAGES = 10
TOP_LANE = "top"
IMMEDIATE_COMMANDS = frozenset({"panic", "stop"})  # never queued behind a slow handler
LOOKUP_ATTEMPTS = 3
LOOKUP_RETRY_S = 1.0
SERVER_ERROR = 500


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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api = api
        self._registry = registry
        self._service = service
        self.renderer = renderer
        self.handlers = handlers
        self._access = access
        self._bot_id = bot_person_id
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._lanes: dict[str, asyncio.Task[None]] = {}
        self._intake_tail: asyncio.Task[None] | None = None

    # exactly-once bookkeeping

    async def _claim(self, message_id: str) -> bool:
        """Mark a message processed. Returns False if it was already handled."""
        async with self._lock:
            if await self._registry.kv_get(f"{SEEN_PREFIX}{message_id}") is not None:
                return False
            await self._registry.kv_set(f"{SEEN_PREFIX}{message_id}", utcnow().isoformat())
            return True

    async def _unclaim(self, message_id: str) -> None:
        """Let a message or card tap that failed before it was handled be handled when it comes again."""
        with contextlib.suppress(Exception):
            await self._registry.kv_delete(f"{SEEN_PREFIX}{message_id}")

    async def _remember_last(self, message: Message) -> None:
        await self._registry.kv_set(LAST_SEEN_KEY, message.id)

    # tasks

    def _spawn(
        self, name: str, work: Callable[[], Awaitable[None]], after: asyncio.Task[None] | None = None
    ) -> asyncio.Task[None]:
        async def run() -> None:
            if after is not None:
                await asyncio.wait({after})
            await work()

        task = asyncio.create_task(run(), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _dispatch(self, lane: str | None, name: str, work: Callable[[], Awaitable[None]]) -> None:
        """Run `work` after whatever is already queued in its lane; no lane runs it at once."""
        if lane is None:
            self._spawn(name, work)
            return
        task = self._spawn(name, work, self._lanes.get(lane))
        self._lanes[lane] = task
        task.add_done_callback(lambda done: self._release(lane, done))

    def _release(self, lane: str, done: asyncio.Task[None]) -> None:
        if self._lanes.get(lane) is done:
            del self._lanes[lane]

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error(f"{task.get_name()} failed: {type(error).__name__}: {error}", exc_info=error)

    def _pending(self) -> list[asyncio.Task[None]]:
        return [t for t in self._tasks if not t.done()]

    async def drain(self) -> None:
        """Wait for every handler started so far, and any they start, to finish."""
        while pending := self._pending():
            await asyncio.gather(*pending, return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel outstanding handlers; the websocket client calls this when it stops."""
        for task in self._pending():
            task.cancel()
        await self.drain()
        self._lanes.clear()
        self._intake_tail = None

    async def _retried[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """A Webex lookup, tried again on a network error or a 5xx: a card tap is never replayed."""
        for attempt in range(1, LOOKUP_ATTEMPTS):
            try:
                return await call()
            except (httpx.TransportError, WebexError) as exc:
                if isinstance(exc, WebexError) and exc.status < SERVER_ERROR:
                    raise
                log.warning(f"Webex lookup failed ({type(exc).__name__}); retrying")
                await self._sleep(LOOKUP_RETRY_S * attempt)
        return await call()

    # inbound

    async def on_activity(self, activity: Activity) -> None:
        """Hand the activity to a task and return, so a slow handler never holds up the socket.

        Lookups run one after another, in arrival order. Handling is serial per thread, so two
        messages in one thread run in order while other threads, and `/panic`, do not wait.
        """
        self._intake_tail = self._spawn("webex-intake", lambda: self._intake(activity), self._intake_tail)

    async def _intake(self, activity: Activity) -> None:
        if activity.verb == "cardAction":
            action = await self._retried(lambda: self.api.get_attachment_action(activity.message_id))
            self._dispatch(f"card:{action.message_id}", "webex-card", lambda: self.handle_action(action))
            return
        message = await self._retried(lambda: self.api.get_message(activity.message_id))
        lane = None if self._is_immediate(message) else message.parent_id or TOP_LANE
        self._dispatch(lane, "webex-message", lambda: self.handle_message(message))

    @staticmethod
    def _is_immediate(message: Message) -> bool:
        parsed = commands.parse(message.text or message.markdown, in_thread=bool(message.parent_id))
        return isinstance(parsed, commands.Command) and parsed.name in IMMEDIATE_COMMANDS

    async def handle_message(self, message: Message) -> None:
        if message.person_id == self._bot_id:
            return
        if not await self._claim(message.id):
            log.debug("duplicate message ignored")
            return
        try:
            context = await self._admit_message(message)
        except BaseException:
            await self._unclaim(message.id)
            raise
        if context is not None:
            await self.handlers.text(context, message.text or message.markdown)

    async def _admit_message(self, message: Message) -> Context | None:
        """Access check and thread lookup; None when the message was refused or answered here."""
        verdict = await self._access.check_message(message.person_id, message.person_email, message.room_type)
        if verdict.leave_room:
            with contextlib.suppress(Exception):
                await self.api.leave_room(message.room_id, self._bot_id)
        if not verdict.allowed:
            log.info(f"ignored message: {verdict.reason}")
            return None
        log_body(log, "webex in", message.text)
        await self._remember_last(message)
        await self.renderer.outbox.remember_room(message.room_id)
        session = await self._registry.session_by_thread(message.parent_id) if message.parent_id else None
        if message.parent_id and session is None:
            await self.renderer.post_top(
                "That thread is not linked to a session any more. `/sessions` lists them."
            )
            return None
        return Context(session)

    async def handle_action(self, action: AttachmentAction) -> None:
        if not await self._claim(action.id):
            return
        try:
            allowed = await self._admit_action(action)
        except BaseException:
            await self._unclaim(action.id)
            raise
        if not allowed:
            return
        outcome = await actions.submit(self._service, self.handlers, action.inputs)
        request_id = action.inputs.get("request_id")
        if outcome == "already_answered" and isinstance(request_id, str):
            await self._already_resolved(request_id)
        elif outcome == "ignored":
            log.info("ignored a card submission with no relay action")

    async def _admit_action(self, action: AttachmentAction) -> bool:
        room = await self._retried(lambda: self.api.get_room(action.room_id))
        verdict = await self._access.check_action(action.person_id, room.type)
        if verdict.leave_room:
            with contextlib.suppress(Exception):
                await self.api.leave_room(action.room_id, self._bot_id)
        if not verdict.allowed:
            log.info(f"ignored card submission: {verdict.reason}")
        return verdict.allowed

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
        token,
        client,
        config.webex.u2c_url,
        frontend.on_activity,
        frontend.catch_up,
        connector=connector,
        on_close=frontend.aclose,
    )
    return frontend, ws
