"""Phone-started chats in Cursor's desktop composer, driven over CDP. Off by default (`cdp.enabled`).

The composer is one shared surface, so only the most recent phone chat is driven: older ones are
continued at the desk. A Cursor version without a passing `relay selftest cursor-cdp` is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from agent_relay.adapters.base import (
    Adapter,
    AdapterUnavailableError,
    EventPublisher,
    ModelInfo,
    StartRequest,
    UnsupportedError,
)
from agent_relay.adapters.cursor_cdp.connect import Connection, Connector, Launcher, connect, fetch_version
from agent_relay.adapters.cursor_cdp.driver import Composer, Snapshot, TurnTracker
from agent_relay.adapters.cursor_cdp.selectors import SelectorSet, load_selectors
from agent_relay.adapters.cursor_cdp.selftest import gate, last_report
from agent_relay.core.registry import Registry
from agent_relay.log import get_logger
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.config import CdpConfig
from agent_relay.models.events import ErrorEvent, Event, Harness, Origin, TextDelta, TurnCompleted
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

ACTIVE_KEY = "cursor_cdp.active"
OPEN_WAIT_S = 20.0
START_DEADLINE_S = 20.0
NOT_STARTED_TEXT = "Cursor didn't start the turn; try again."
STOPPED_TEXT = "Stopped from the phone."
OLDER_CHAT = (
    "Only the latest phone-started Cursor composer chat can be driven from the phone; "
    "continue this one at the desk."
)


@dataclass
class _Turn:
    session_key: str
    workspace: str | None
    composer: Composer
    tracker: TurnTracker
    stopped: bool = False
    finishing: asyncio.Task[None] | None = field(default=None, repr=False)
    watchdog: asyncio.Task[None] | None = field(default=None, repr=False)


class CursorCdpAdapter(Adapter):
    harness = Harness.CURSOR_CDP
    capabilities = Capabilities(cold_start=True, model_select=True, token_stream=True, cancel=True)

    def __init__(
        self,
        registry: Registry,
        config: CdpConfig,
        selectors: SelectorSet | None = None,
        launcher: Launcher | None = None,
        connector: Connector = connect,
        open_wait: float = OPEN_WAIT_S,
        start_deadline: float = START_DEADLINE_S,
    ) -> None:
        self._registry = registry
        self._port = config.port
        self._selectors = selectors or load_selectors()
        self._launcher = launcher or Launcher()
        self._connector = connector
        self._open_wait = open_wait
        self._start_deadline = start_deadline
        self._conn: Connection | None = None
        self._composers: dict[int, Composer] = {}
        self._chosen: dict[int, str] = {}
        self._turn: _Turn | None = None
        self._lock = asyncio.Lock()
        self._events: asyncio.Queue[Event] = asyncio.Queue()

    # events

    async def run(self, publisher: EventPublisher) -> None:
        while True:
            await publisher.publish(await self._events.get())

    def _emit(self, event: Event) -> None:
        self._events.put_nowait(event)

    # readiness

    async def health(self) -> tuple[bool, str]:
        version = await fetch_version(self._port)
        if version is None:
            return False, f"Cursor is not running with the debug port 127.0.0.1:{self._port}"
        reason = gate(await last_report(self._registry), version, self._selectors)
        if reason is not None:
            return False, reason
        return True, f"Cursor {version} on 127.0.0.1:{self._port}; the self-test passed"

    async def _connection(self) -> Connection:
        if self._conn is not None and self._conn.alive:
            return self._conn
        if self._conn is not None:
            await self._conn.close()
        self._composers.clear()
        self._chosen.clear()
        self._conn = await self._connector(self._port)
        return self._conn

    async def _ready(self, workspace: str | None) -> Composer:
        version = await self._launcher.ensure(self._port)
        reason = gate(await last_report(self._registry), version, self._selectors)
        if reason is not None:
            raise AdapterUnavailableError(reason)
        conn = await self._connection()
        page = await conn.workbench(self._selectors, workspace)
        if page is None and workspace is not None:
            await self._launcher.open_folder(workspace)
            page = await self._wait_for_page(conn, workspace)
        if page is None:
            raise AdapterUnavailableError(
                "No Cursor window shows the chat composer; open Cursor's chat pane."
            )
        return await self._composer_for(page)

    async def _wait_for_page(self, conn: Connection, workspace: str) -> Page | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._open_wait
        while loop.time() < deadline:
            page = await conn.workbench(self._selectors, workspace)
            if page is not None:
                return page
            await asyncio.sleep(0.5)
        return None

    async def _composer_for(self, page: Page) -> Composer:
        existing = self._composers.get(id(page))
        if existing is not None:
            return existing
        composer = Composer(page, self._selectors)

        def _on_snapshot(snap: Snapshot) -> None:
            self._on_snapshot(composer, snap)

        await composer.observe(_on_snapshot)
        self._composers[id(page)] = composer
        return composer

    # turns

    def _on_snapshot(self, composer: Composer, snap: Snapshot) -> None:
        turn = self._turn
        if turn is None or turn.composer is not composer or turn.finishing is not None:
            return
        delta = turn.tracker.feed(snap)
        if delta:
            self._emit(
                TextDelta(
                    harness=self.harness, session_key=turn.session_key, workspace=turn.workspace, text=delta
                )
            )
        if turn.tracker.done:
            turn.finishing = asyncio.get_running_loop().create_task(self._finish(turn))

    async def _begin(self, composer: Composer, session: SessionRecord, text: str) -> None:
        if session.model and self._chosen.get(id(composer.page)) != session.model:
            await composer.choose_model(session.model)
            self._chosen[id(composer.page)] = session.model
        baseline = await composer.snapshot()
        turn = _Turn(session.session_key, session.workspace, composer, TurnTracker(baseline))
        self._turn = turn
        await self.report_status(session.session_key, SessionStatus.RUNNING, "adapter report")
        try:
            await composer.submit(text)
        except (PlaywrightError, AdapterUnavailableError):
            self._turn = None
            await self.report_status(session.session_key, SessionStatus.IDLE, "adapter report")
            raise
        turn.watchdog = asyncio.get_running_loop().create_task(self._watch(turn))

    async def _watch(self, turn: _Turn) -> None:
        """A send click that Cursor ignores leaves no stop button and no reply; free the composer."""
        await asyncio.sleep(self._start_deadline)
        async with self._lock:
            if self._turn is not turn or turn.finishing is not None:
                return
            try:
                if turn.tracker.began(await turn.composer.snapshot()):
                    return
            except PlaywrightError:
                log.warning(f"could not read the composer to check {turn.session_key} started")
            self._turn = None
            with contextlib.suppress(PlaywrightError, AdapterUnavailableError):
                await turn.composer.clear()
            await self.report_status(turn.session_key, SessionStatus.IDLE, "adapter report")
            self._emit(
                ErrorEvent(
                    harness=self.harness,
                    session_key=turn.session_key,
                    workspace=turn.workspace,
                    message=NOT_STARTED_TEXT,
                )
            )

    async def _finish(self, turn: _Turn) -> None:
        if turn.watchdog is not None:
            turn.watchdog.cancel()
        async with self._lock:
            if self._turn is turn:
                self._turn = None
            reply = turn.tracker.text.strip()
            if turn.stopped:
                reply = f"{reply}\n\n{STOPPED_TEXT}" if reply else STOPPED_TEXT
            await self.report_status(turn.session_key, SessionStatus.IDLE, "adapter report")
            self._emit(
                TurnCompleted(
                    harness=self.harness,
                    session_key=turn.session_key,
                    workspace=turn.workspace,
                    text=reply or None,
                )
            )
            queued = await self._registry.pop_queued(turn.session_key, ("steer", "prompt"))
            session = await self._registry.get_session(turn.session_key)
            if not queued or session is None:
                return
            try:
                await self._begin(turn.composer, session, "\n\n".join(q.text for q in queued))
            except (PlaywrightError, AdapterUnavailableError) as exc:
                message = exc.message if isinstance(exc, PlaywrightError) else str(exc)
                self._emit(
                    ErrorEvent(
                        harness=self.harness,
                        session_key=turn.session_key,
                        workspace=turn.workspace,
                        message=message,
                    )
                )

    # contract

    async def list_models(self) -> list[ModelInfo]:
        async with self._lock:
            if self._turn is not None:
                raise AdapterUnavailableError("Cursor's composer is busy; list models when the turn ends.")
            labels = await (await self._ready(None)).models()
        return [ModelInfo(id=label, label=label) for label in labels]

    async def start_session(
        self, request: StartRequest, attachments: Sequence[Attachment] = ()
    ) -> SessionRecord:
        request = request.model_copy(update={"prompt": self.prompt_with_files(request.prompt, attachments)})
        async with self._lock:
            if self._turn is not None:
                raise AdapterUnavailableError("Cursor's composer is busy with another phone chat.")
            composer = await self._ready(request.workspace)
            await composer.new_chat()
            key = f"{self.harness.value}:phone-{uuid.uuid4().hex}"
            record = await self._registry.create_session(
                SessionRecord(
                    session_key=key,
                    harness=self.harness,
                    harness_id=key.split(":", 1)[1],
                    workspace=request.workspace,
                    model=request.model,
                    origin=Origin.PHONE,
                    status=SessionStatus.RUNNING,
                    title=request.prompt[:80] or None,
                )
            )
            await self._registry.kv_set(ACTIVE_KEY, key)
            try:
                await self._begin(composer, record, request.prompt)
            except Exception:
                await self.report_status(key, SessionStatus.ENDED, "adapter report")
                raise
        log.info(f"opened a Cursor composer chat for {key}")
        stored = await self._registry.get_session(key)
        return stored or record

    async def send_prompt(
        self, session: SessionRecord, text: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        text = self.prompt_with_files(text, attachments)
        async with self._lock:
            if await self._registry.kv_get(ACTIVE_KEY) != session.session_key:
                raise AdapterUnavailableError(OLDER_CHAT)
            if self._turn is not None:
                raise UnsupportedError(self.harness, "steering a running Cursor composer turn")
            await self._begin(await self._ready(session.workspace), session, text)

    async def set_model(self, session: SessionRecord, model: str) -> None:
        await self._registry.update_session_fields(session.session_key, model=model)

    async def cancel(self, session: SessionRecord) -> None:
        turn = self._turn
        if turn is None or turn.session_key != session.session_key:
            raise AdapterUnavailableError("This Cursor composer chat is not running.")
        turn.stopped = True
        if not await turn.composer.stop():
            turn.stopped = False
            raise AdapterUnavailableError("Cursor shows no stop button for this turn.")

    async def aclose(self) -> None:
        turn = self._turn
        if turn is not None and turn.watchdog is not None:
            turn.watchdog.cancel()
        if turn is not None and turn.finishing is not None:
            await asyncio.gather(turn.finishing, return_exceptions=True)
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
