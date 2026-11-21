"""Turn router events into Webex thread messages within the platform's limits.

- One thread per session: the root is an editable markdown status line (edit budget 8), edited
  silently as the session moves (⏳ running, ✅ done, 🅿️ parked, ▶️ released; 🏝️ while away mode is on).
- A turn is one message: when it completes, its final answer (the last assistant message, not the
  narration between tool calls) and a footer `⏱ 3m · 🔧 14 · 📝 4 files (+120 −8)`. Text deltas are
  buffered and tool events only count; nothing is posted while the turn runs.
- A long or rich answer is one message too: a short preview and a self-contained `answer.html`.
- A desk session gets its thread at its first final result, question or error, not when it registers.
- Questions are cards that are deleted once answered. Approvals never reach the phone.
- Pacing: at most one message per pace_s per thread.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from pydantic import JsonValue

from agent_relay.core import away
from agent_relay.core.registry import Registry
from agent_relay.core.service import APP_LABELS
from agent_relay.core.service.queue import mode_of
from agent_relay.core.service.sessions import Changes, git_changes
from agent_relay.frontends import card_state, request_cards, rich
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.api import EditLimitError, Message, WebexApi, WebexError, text_bytes
from agent_relay.frontends.webex.links import neutralise_links
from agent_relay.log import get_logger, log_body
from agent_relay.models.config import QueueMode, RendererConfig
from agent_relay.models.events import (
    ApprovalRequested,
    ApprovalResolved,
    ErrorEvent,
    Event,
    Origin,
    QuestionAnswered,
    QuestionAsked,
    SessionParked,
    SessionReleased,
    SessionStarted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    utcnow,
)
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

ROOM_KEY = "webex.room_id"
ROOT_POLL_S = 5.0
ROOT_CLOSING_RESERVE = 2
# Events after which the root status line is looked at again, besides a turn's first work.
ROOT_MILESTONES = (TurnCompleted, SessionParked, SessionReleased, ErrorEvent)
CLOSED_OUTCOME = {
    "ended": "🏁 The session ended; request closed",
    "stopped": "⏹️ Stopped; request closed",
    "abandoned": "🔌 The agent went away; request closed",
}
NO_TEXT = "✅ Done."


def chunk_text(text: str, max_bytes: int) -> list[str]:
    """Split at paragraph, then line, then word boundaries; never exceed max_bytes per chunk."""
    chunks: list[str] = []
    rest = text
    while text_bytes(rest) > max_bytes:
        window = rest.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = len(window)
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n ")
    if rest.strip():
        chunks.append(rest)
    return [c for c in chunks if c.strip()]


def answer_meta(session: SessionRecord, footer: str) -> rich.AnswerMeta:
    """What the HTML header of an answer or a diff says about the session."""
    workspace = (session.workspace or "").rstrip("/").rsplit("/", 1)[-1]
    return rich.AnswerMeta(
        app=APP_LABELS.get(session.harness, session.harness.value),
        workspace=workspace or "?",
        model=session.model,
        when=utcnow(),
        footer=footer,
    )


def _worth_root_edit(status: SessionStatus, remaining: int) -> bool:
    """Whether the root may spend one of its `remaining` edits showing `status`."""
    if remaining <= 0:
        return False
    if remaining <= ROOT_CLOSING_RESERVE:  # keep room for the edit that shows the turn ended
        return status in (SessionStatus.IDLE, SessionStatus.ENDED) or (
            remaining == ROOT_CLOSING_RESERVE and status is not SessionStatus.RUNNING
        )
    return True


class Pacer:
    """Token bucket: one message per `interval` seconds per thread."""

    def __init__(
        self,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}

    async def wait(self, key: str) -> None:
        now = self._clock()
        ready = self._next.get(key, now)
        if ready > now:
            await self._sleep(ready - now)
            now = ready
        self._next[key] = now + self._interval


class Outbox:
    """Posts to the user's 1:1 room with the bot, learning its id from the first reply."""

    def __init__(self, api: WebexApi, registry: Registry, allowed_email: str) -> None:
        self.api = api
        self._registry = registry
        self._email = allowed_email
        self._room_id: str | None = None

    async def room_id(self) -> str | None:
        if self._room_id is None:
            self._room_id = await self._registry.kv_get(ROOM_KEY)
        return self._room_id

    async def remember_room(self, room_id: str) -> None:
        if room_id and room_id != self._room_id:
            self._room_id = room_id
            await self._registry.kv_set(ROOM_KEY, room_id)

    async def post(
        self,
        markdown: str,
        parent_id: str | None = None,
        card: dict[str, JsonValue] | None = None,
        file: tuple[str, bytes, str] | None = None,
    ) -> Message:
        room = await self.room_id()
        log_body(log, "webex out", markdown)
        message = await self.api.create_message(
            markdown,
            room_id=room,
            to_person_email=None if room else self._email,
            parent_id=parent_id,
            card=card,
            file=file,
        )
        await self.remember_room(message.room_id)
        return message

    async def edit(self, message_id: str, markdown: str) -> bool:
        room = await self.room_id()
        if room is None:
            return False
        try:
            await self.api.edit_message(message_id, room, markdown)
        except EditLimitError:
            return False
        return True

    async def delete(self, message_id: str) -> bool:
        """Delete one of the bot's messages. A failure (already gone, no right) is logged, never raised."""
        try:
            await self.api.delete_message(message_id)
        except (WebexError, httpx.HTTPError, OSError) as exc:
            log.debug(f"could not delete message {message_id}: {type(exc).__name__}")
            return False
        return True


@dataclass
class _Thread:
    session_key: str
    root_id: str
    root_edits: int = 0
    last_root: str = ""
    status_checked: float = float("-inf")


@dataclass
class _Turn:
    """The turn in progress, kept apart from the thread: a desk session has none until its first post."""

    started: float | None = None
    tools: int = 0
    working: bool = False
    segment: str = ""  # the text since the last tool call: what the final answer falls back to


class WebexRenderer:
    def __init__(
        self,
        outbox: Outbox,
        registry: Registry,
        config: RendererConfig,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        *,
        default_queue_mode: QueueMode = "queue",
    ) -> None:
        self._default_queue_mode = default_queue_mode
        self.outbox = outbox
        self._registry = registry
        self._config = config
        self._clock = clock
        self._pacer = Pacer(config.pace_s, clock, sleep)
        self._threads: dict[str, _Thread] = {}
        self._turns: dict[str, _Turn] = {}
        self._has_threads = asyncio.Event()
        self._creating: dict[str, asyncio.Lock] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # threads

    async def thread_for(self, session: SessionRecord) -> str:
        """The root message id of the session's thread, creating it on first use."""
        existing = self._threads.get(session.session_key)
        if existing is not None:
            return existing.root_id
        lock = self._creating.setdefault(session.session_key, asyncio.Lock())
        async with lock:
            return await self._open_thread(session)

    async def _open_thread(self, session: SessionRecord) -> str:
        existing = self._threads.get(session.session_key)
        if existing is not None:
            return existing.root_id
        if session.webex_thread_id:
            root_id = session.webex_thread_id
            edits = session.extra.get("root_edits")
            self._track(
                _Thread(
                    session.session_key,
                    root_id,
                    root_edits=edits if isinstance(edits, int) else 0,
                    last_root=cards.root_markdown(
                        session, away=await away.is_away(self._registry, session.workspace)
                    ),
                )
            )
            return root_id
        await self._pacer.wait("root")
        markdown = cards.root_markdown(session, away=await away.is_away(self._registry, session.workspace))
        message = await self.outbox.post(markdown)
        await self._registry.update_session_fields(session.session_key, webex_thread_id=message.id)
        self._track(_Thread(session.session_key, message.id, last_root=markdown))
        return message.id

    def _track(self, thread: _Thread) -> None:
        self._threads[thread.session_key] = thread
        self._has_threads.set()

    def has_thread(self, session: SessionRecord) -> bool:
        return session.session_key in self._threads or bool(session.webex_thread_id)

    async def refresh_root(self, session: SessionRecord) -> None:
        """Edit the root status line, within the edit budget. Unchanged lines cost nothing.

        The queue count rides along when the line is edited anyway; a queue change alone never edits it.
        Edits are kept for milestones: `running` is skipped when few remain, and the last one is only
        spent on `idle` or `ended`.
        """
        root_id = await self.thread_for(session)
        thread = self._threads[session.session_key]
        is_away = await away.is_away(self._registry, session.workspace)
        base = cards.root_markdown(session, away=is_away)
        remaining = self._config.root_edit_budget - thread.root_edits
        if base == thread.last_root or not _worth_root_edit(session.status, remaining):
            return
        waiting = len(await self._registry.list_queued(session.session_key))
        mode = mode_of(session, self._default_queue_mode)
        if await self.outbox.edit(root_id, cards.root_markdown(session, waiting, mode, is_away)):
            thread.root_edits += 1
            thread.last_root = base
            await self._registry.set_session_extra(session.session_key, "root_edits", thread.root_edits)

    async def post_in_thread(
        self,
        session: SessionRecord,
        markdown: str,
        card: dict[str, JsonValue] | None = None,
        file: tuple[str, bytes, str] | None = None,
    ) -> Message:
        root_id = await self.thread_for(session)
        await self._pacer.wait(session.session_key)
        return await self.outbox.post(markdown, parent_id=root_id, card=card, file=file)

    async def post_top(self, markdown: str, card: dict[str, JsonValue] | None = None) -> Message:
        await self._pacer.wait("root")
        return await self.outbox.post(markdown, card=card)

    async def edit(self, message_id: str, markdown: str) -> bool:
        return await self.outbox.edit(message_id, markdown)

    # event sink

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def handle(self, event: Event) -> None:
        if isinstance(event, ErrorEvent) and event.session_key.startswith("adapter:"):
            log.warning(f"adapter error, not posted to the phone: {event.message}")
            return
        session = await self._registry.get_session(event.session_key)
        if session is None:
            log.debug(f"no session for {event.type} {event.session_key}; not rendered")
            return
        async with self._lock(session.session_key):
            await self._render(session, event)

    async def _render(self, session: SessionRecord, event: Event) -> None:
        key = session.session_key
        now = self._clock()
        turn = self._turns.get(key) or _Turn()
        if isinstance(event, SessionStarted | TextDelta | ToolStarted):
            self._turns[key] = turn
            if turn.started is None:
                turn.started = now
        first_work = isinstance(event, TextDelta | ToolStarted) and not turn.working
        turn.working = turn.working or first_work
        match event:
            case SessionStarted():
                if session.origin is Origin.PHONE or self.has_thread(session):
                    await self.thread_for(session)
                    await self.refresh_root(session)
            case TextDelta(text=text):
                turn.segment += text
            case ToolStarted():
                turn.tools += 1
                turn.segment = ""
            case TurnCompleted(text=text):
                try:
                    await self._post_final(session, turn, text)
                finally:
                    self._turns.pop(key, None)
            case ErrorEvent(message=message):
                self._turns.pop(key, None)
                await self.post_in_thread(session, f"⚠️ {message}")
            case QuestionAsked() as asked:
                turn.segment = ""
                safe = asked.model_copy(update={"question": neutralise_links(asked.question)})
                markdown, card = await request_cards.question_card(self._registry, session, safe)
                posted = await self.post_in_thread(session, markdown, card=card)
                await self._registry.set_pending_message(asked.request_id, posted.id)
            case QuestionAnswered(request_id=request_id, answer=answer, by=by):
                await card_state.drop_offer(self._registry, request_id)
                outcome = CLOSED_OUTCOME.get(by) or (f"💬 Answered: {answer}" if answer else "⏭️ Skipped")
                await self._close_card(request_id, outcome)
            case (
                ToolFinished()
                | ApprovalRequested()
                | ApprovalResolved()
                | SessionParked()
                | SessionReleased()
            ):
                pass  # counted, ignored or shown only by the root line: nothing is posted
        if self.has_thread(session) and (first_work or isinstance(event, ROOT_MILESTONES)):
            await self.refresh_root(await self._current(session))

    async def _current(self, session: SessionRecord) -> SessionRecord:
        """The session as stored now; its status may have moved while the event was handled."""
        return await self._registry.get_session(session.session_key) or session

    # the final message

    async def _footer(self, session: SessionRecord, turn: _Turn) -> str:
        changes: Changes | None = await git_changes(session.workspace)
        seconds = self._clock() - turn.started if turn.started is not None else None
        return cards.footer_line(
            seconds,
            turn.tools,
            len(changes.files) if changes else 0,
            changes.added if changes else 0,
            changes.removed if changes else 0,
        )

    async def _post_final(self, session: SessionRecord, turn: _Turn, completed_text: str | None) -> None:
        """The turn's one message: the last assistant message and the footer, or a preview and answer.html."""
        cfg = self._config
        text = (completed_text or "").strip() or turn.segment.strip()
        footer = await self._footer(session, turn)
        if text and rich.wants_html(text, mode=cfg.html_threshold, max_bytes=cfg.max_message_bytes):
            page = await self._html_answer(session, text, footer)
            if page is not None:
                preview, attachment = page
                await self.post_in_thread(session, f"{preview}\n\n{footer}".strip(), file=attachment)
                return
        body = neutralise_links(text) if text else NO_TEXT
        room = cfg.max_message_bytes - text_bytes(footer) - 2
        pieces = chunk_text(body, max(room, 1))
        if not pieces:
            pieces = [NO_TEXT]
        pieces[-1] = f"{pieces[-1]}\n\n{footer}" if footer else pieces[-1]
        for piece in pieces:
            await self.post_in_thread(session, piece)

    async def _html_answer(
        self, session: SessionRecord, text: str, footer: str
    ) -> tuple[str, tuple[str, bytes, str]] | None:
        try:
            answer = rich.render_answer(text, answer_meta(session, footer))
        except Exception:
            log.exception("could not render answer.html; sending the answer as text")
            return None
        return answer.preview, (answer.filename, answer.html, answer.content_type)

    async def _close_card(self, request_id: str, outcome: str) -> None:
        """Delete the question card once it is closed; if Webex will not, edit it to say how it ended."""
        request = await self._registry.get_pending(request_id)
        if request is None or request.webex_message_id is None:
            return
        if await self.outbox.delete(request.webex_message_id):
            return
        try:
            await self.outbox.edit(request.webex_message_id, outcome)
        except (WebexError, httpx.HTTPError) as exc:
            log.debug(f"question card {request_id} could not be closed: {type(exc).__name__}")

    # housekeeping

    async def tick(self) -> None:
        """Notice status changes no event announced (a stop, an app ending a session) and forget
        threads that have settled. Called about once a second."""
        now = self._clock()
        for key, thread in list(self._threads.items()):
            if now - thread.status_checked < ROOT_POLL_S:
                continue
            session = await self._registry.get_session(key)
            if session is None:
                continue
            async with self._lock(key):
                thread.status_checked = now
                await self.refresh_root(session)
                if await self._settled(session, thread):
                    self._threads.pop(key, None)
                    self._turns.pop(key, None)
        if not self._threads:
            self._has_threads.clear()

    async def wait_for_threads(self) -> None:
        """Return once there is a thread for `tick` to look after."""
        await self._has_threads.wait()

    async def _settled(self, session: SessionRecord, thread: _Thread) -> bool:
        """An ended session with nothing left to show; a later event reopens its thread from the record."""
        if session.status is not SessionStatus.ENDED:
            return False
        remaining = self._config.root_edit_budget - thread.root_edits
        shown = cards.root_markdown(session, away=await away.is_away(self._registry, session.workspace))
        return thread.last_root == shown or not _worth_root_edit(session.status, remaining)
