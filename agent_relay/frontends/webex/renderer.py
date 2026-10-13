"""Turn router events into Webex thread messages within the platform's limits.

- One thread per session: the root is an editable markdown status line (edit budget 8).
- `text.delta` is buffered, flushed at a paragraph boundary once ≥ flush_bytes, or after
  flush_idle_s of quiet; no message exceeds max_message_bytes.
- Tool events feed one progress message per turn, edited in place at most once per
  progress_edit_interval_s and one edit short of Webex's cap until the closing edit; a spent message
  is replaced by a fresh one.
- A turn ends with a completion card. Final answers over the limit become `answer.md`.
- Pacing: at most one message per pace_s per thread.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import JsonValue

from agent_relay.core.registry import Registry
from agent_relay.core.service.queue import HOOKED, mode_of
from agent_relay.core.service.sessions import Changes, git_changes
from agent_relay.frontends import card_state, request_cards
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.api import MAX_EDITS, EditLimitError, Message, WebexApi, text_bytes
from agent_relay.frontends.webex.bots import scoped
from agent_relay.frontends.webex.cards import completion as completion_card
from agent_relay.frontends.webex.links import neutralise_links
from agent_relay.hook.shims import abbreviate
from agent_relay.log import get_logger, log_body
from agent_relay.models.config import CONTROL, BotName, QueueMode, RendererConfig
from agent_relay.models.events import (
    ApprovalRequested,
    ApprovalResolved,
    ErrorEvent,
    Event,
    QuestionAnswered,
    QuestionAsked,
    SessionParked,
    SessionReleased,
    SessionStarted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

ROOM_KEY = "webex.room_id"
TOOL_DETAIL_CHARS = 300
PROGRESS_LINES = 10
# One edit stays spare under Webex's cap for the closing "done" edit.
PROGRESS_EDITS = MAX_EDITS - 1
ROOT_POLL_S = 5.0
ROOT_CLOSING_RESERVE = 2
# Events after which the root status line is looked at again, besides a turn's first event.
ROOT_MILESTONES = (TurnCompleted, SessionParked, SessionReleased)
APPROVAL_OUTCOME = {
    "allow": "✅ Allowed",
    "allow_session": "✅ Allowed for this session",
    "deny": "❌ Denied",
    "desk": "🖥️ Away mode off; answer it at the desk",
}
CLOSED_OUTCOME = {
    "ended": "🏁 The session ended; request closed",
    "stopped": "⏹️ Stopped; request closed",
    "abandoned": "🔌 The agent went away; request closed",
}


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
    """Posts to the user's 1:1 room with one bot, learning its id from the first reply."""

    def __init__(self, api: WebexApi, registry: Registry, allowed_email: str, bot: BotName = CONTROL) -> None:
        self.api = api
        self.bot = bot
        self._registry = registry
        self._email = allowed_email
        self._room_id: str | None = None
        self._room_key = scoped(ROOM_KEY, bot)

    async def room_id(self) -> str | None:
        if self._room_id is None:
            self._room_id = await self._registry.kv_get(self._room_key)
        return self._room_id

    async def remember_room(self, room_id: str) -> None:
        if room_id and room_id != self._room_id:
            self._room_id = room_id
            await self._registry.kv_set(self._room_key, room_id)

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


@dataclass
class _Thread:
    session_key: str
    root_id: str
    text: str = ""
    streamed: bool = False
    last_activity: float = 0.0
    root_edits: int = 0
    last_root: str = ""
    # The turn in progress.
    turn_started: float | None = None
    working: bool = False
    tool_count: int = 0
    progress_lines: list[str] = field(default_factory=list)
    progress_id: str | None = None
    progress_edits: int = 0
    progress_at: float = 0.0
    progress_dirty: bool = False
    status_checked: float = float("-inf")

    def reset_turn(self) -> None:
        self.turn_started = None
        self.working = False
        self.tool_count = 0
        self.progress_lines = []
        self.progress_id = None
        self.progress_edits = 0
        self.progress_dirty = False


class WebexRenderer:
    def __init__(
        self,
        outbox: Outbox,
        registry: Registry,
        config: RendererConfig,
        approval_deadline_s: int = 600,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        *,
        default_queue_mode: QueueMode = "queue",
    ) -> None:
        self._default_queue_mode = default_queue_mode
        self._approval_deadline_s = approval_deadline_s
        self.outbox = outbox
        self._registry = registry
        self._config = config
        self._clock = clock
        self._pacer = Pacer(config.pace_s, clock, sleep)
        self._threads: dict[str, _Thread] = {}
        self._creating: dict[str, asyncio.Lock] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def bot(self) -> BotName:
        return self.outbox.bot

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
            self._threads[session.session_key] = _Thread(
                session.session_key,
                root_id,
                root_edits=edits if isinstance(edits, int) else 0,
                last_root=cards.root_markdown(session),
            )
            return root_id
        await self._pacer.wait("root")
        markdown = cards.root_markdown(session)
        message = await self.outbox.post(markdown)
        await self._registry.update_session_fields(
            session.session_key, webex_thread_id=message.id, webex_bot=self.bot
        )
        self._threads[session.session_key] = _Thread(session.session_key, message.id, last_root=markdown)
        return message.id

    async def refresh_root(self, session: SessionRecord) -> None:
        """Edit the root status line, within the edit budget. Unchanged lines cost nothing.

        The queue count rides along when the line is edited anyway; a queue change alone never edits it.
        Edits are kept for milestones: `running` is skipped when few remain, and the last one is only
        spent on `idle` or `ended`.
        """
        root_id = await self.thread_for(session)
        thread = self._threads[session.session_key]
        base = cards.root_markdown(session)
        remaining = self._config.root_edit_budget - thread.root_edits
        if base == thread.last_root or not _worth_root_edit(session.status, remaining):
            return
        waiting = len(await self._registry.list_queued(session.session_key))
        mode = mode_of(session, self._default_queue_mode)
        if await self.outbox.edit(root_id, cards.root_markdown(session, waiting, mode)):
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
            await self.post_top(f"⚠️ {event.message}")
            return
        session = await self._registry.get_session(event.session_key)
        if session is None:
            log.debug(f"no session for {event.type} {event.session_key}; not rendered")
            return
        async with self._lock(session.session_key):
            await self._render(session, event)

    async def _render(self, session: SessionRecord, event: Event) -> None:
        await self.thread_for(session)
        thread = self._threads[session.session_key]
        thread.last_activity = self._clock()
        if thread.turn_started is None and isinstance(event, SessionStarted | TextDelta | ToolStarted):
            thread.turn_started = thread.last_activity
        first_work = isinstance(event, TextDelta | ToolStarted) and not thread.working
        thread.working = thread.working or first_work
        match event:
            case SessionStarted():
                await self.refresh_root(session)
            case TextDelta(text=text):
                thread.text += text
                thread.streamed = True
                await self._progress(session, thread)
                await self._flush(session, thread, final=False)
            case ToolStarted(name=name, detail=detail):
                shown = f": {abbreviate(detail, TOOL_DETAIL_CHARS)}" if detail else ""
                thread.tool_count += 1
                await self._note_tool(session, thread, f"🔧 {name}{shown}")
            case ToolFinished(name=name, ok=ok, summary=summary):
                if not ok:
                    await self._note_tool(session, thread, f"❌ {name}{f': {summary}' if summary else ''}")
            case TurnCompleted(text=text):
                try:
                    await self._progress(session, thread, done=True)
                    if not thread.streamed and text:
                        thread.text += text
                    await self._flush(session, thread, final=True)
                    await self._post_completion(session, thread)
                finally:
                    thread.streamed = False
                    thread.reset_turn()
            case SessionParked():
                await self._progress(session, thread)
                await self._flush(session, thread, final=True)
                await self.post_in_thread(session, "🅿️ Parked. Reply here to continue, or `/back` to release.")
            case SessionReleased(reason=reason):
                await self.post_in_thread(session, f"▶️ Released ({reason}).")
            case ErrorEvent(message=message):
                await self._flush(session, thread, final=True)
                await self.post_in_thread(session, f"⚠️ {message}")
            case ApprovalRequested() as asked:
                await self._progress(session, thread)
                await self._flush(session, thread, final=True)
                markdown, card = await request_cards.approval_card(
                    self._registry, session, asked, self._approval_deadline_s
                )
                posted = await self.post_in_thread(session, markdown, card=card)
                await self._registry.set_pending_message(asked.request_id, posted.id)
            case QuestionAsked() as asked_question:
                await self._progress(session, thread)
                await self._flush(session, thread, final=True)
                safe = asked_question.model_copy(
                    update={"question": neutralise_links(asked_question.question)}
                )
                markdown, card = await request_cards.question_card(self._registry, session, safe)
                posted = await self.post_in_thread(session, markdown, card=card)
                await self._registry.set_pending_message(asked_question.request_id, posted.id)
            case ApprovalResolved(request_id=request_id, decision=decision, by=by):
                if by in CLOSED_OUTCOME:
                    await self._close_card(request_id, CLOSED_OUTCOME[by], None)
                else:
                    await self._close_card(request_id, APPROVAL_OUTCOME[decision], by)
            case QuestionAnswered(request_id=request_id, answer=answer, by=by):
                await card_state.drop_offer(self._registry, request_id)
                if by in CLOSED_OUTCOME:
                    await self._close_card(request_id, CLOSED_OUTCOME[by], None)
                else:
                    await self._close_card(
                        request_id, f"💬 Answered: {answer}" if answer else "⏭️ Skipped", None
                    )
        if first_work or isinstance(event, ROOT_MILESTONES):
            await self.refresh_root(await self._current(session))

    async def _current(self, session: SessionRecord) -> SessionRecord:
        """The session as stored now; its status may have moved while the event was handled."""
        return await self._registry.get_session(session.session_key) or session

    async def _flush(self, session: SessionRecord, thread: _Thread, final: bool) -> None:
        cfg = self._config
        if not thread.text.strip():
            thread.text = ""
            return
        if final:
            text, thread.text = thread.text, ""
            if text_bytes(text) > cfg.max_message_bytes and not thread.streamed:
                await self.post_in_thread(
                    session,
                    "📎 The full answer is attached.",
                    file=("answer.md", text.encode("utf-8"), "text/markdown"),
                )
                return
            for piece in chunk_text(neutralise_links(text), cfg.max_message_bytes):
                await self.post_in_thread(session, piece)
            return
        if text_bytes(thread.text) < cfg.flush_bytes:
            return
        boundary = thread.text.rfind("\n\n")
        if boundary <= 0 and text_bytes(thread.text) < cfg.max_message_bytes:
            return
        if boundary <= 0:
            boundary = len(thread.text)
        ready, thread.text = thread.text[:boundary], thread.text[boundary:].lstrip("\n")
        for piece in chunk_text(neutralise_links(ready), cfg.max_message_bytes):
            await self.post_in_thread(session, piece)

    async def _note_tool(self, session: SessionRecord, thread: _Thread, line: str) -> None:
        thread.progress_lines.append(line)
        thread.progress_dirty = True
        await self._progress(session, thread)

    def _progress_text(self, thread: _Thread, now: float, done: bool) -> str:
        calls = thread.tool_count
        head = ["✅ Done" if done else "⏳ Working", f"{calls} tool call{'' if calls == 1 else 's'}"]
        if thread.turn_started is not None:
            head.append(completion_card.duration_text(now - thread.turn_started))
        lines = thread.progress_lines[-PROGRESS_LINES:]
        while True:
            older = len(thread.progress_lines) - len(lines)
            body = [f"**{' · '.join(head)}**", *([f"… {older} earlier"] if older else []), *lines]
            text = neutralise_links("\n".join(body))
            if text_bytes(text) <= self._config.max_message_bytes or len(lines) <= 1:
                return text
            lines = lines[1:]

    async def _progress(self, session: SessionRecord, thread: _Thread, done: bool = False) -> None:
        """Show the turn's tool calls in its one progress message.

        Edits are held to progress_edit_interval_s, except the closing one (`done`), which may use the
        edit kept spare under Webex's cap. When the message's edits are spent a new message takes over
        rather than going quiet.
        """
        message_id = thread.progress_id
        if not thread.progress_dirty and not (done and message_id is not None):
            return
        now = self._clock()
        if (
            not done
            and message_id is not None
            and now - thread.progress_at < self._config.progress_edit_interval_s
        ):
            return
        text = self._progress_text(thread, now, done)
        limit = MAX_EDITS if done else PROGRESS_EDITS
        if message_id is not None and thread.progress_edits < limit:
            if await self.outbox.edit(message_id, text):
                thread.progress_edits += 1
                thread.progress_at = self._clock()
                thread.progress_dirty = False
                return
            thread.progress_edits = MAX_EDITS
        posted = await self.post_in_thread(session, text)
        thread.progress_id = posted.id
        thread.progress_edits = 0
        thread.progress_at = self._clock()
        thread.progress_dirty = False

    async def _post_completion(self, session: SessionRecord, thread: _Thread) -> None:
        """The card that ends a turn: duration, tool calls, what changed, and what to do next."""
        changes: Changes | None = await git_changes(session.workspace)
        files = changes.files if changes else ()
        seconds = self._clock() - thread.turn_started if thread.turn_started is not None else None
        stats = completion_card.stats_line(
            seconds,
            thread.tool_count,
            len(files),
            changes.added if changes else 0,
            changes.removed if changes else 0,
        )
        top = [
            (f.path, "binary" if f.added is None else f"+{f.added} −{f.removed or 0}")
            for f in files[: completion_card.TOP_FILES]
        ]
        can_back = session.extra.get(HOOKED) is True
        quick = self._config.quick_reply
        ops = ["diff", "continue", *(["back"] if can_back else []), *(["send"] if quick else [])]
        nonces = {op: await card_state.issue_nonce(self._registry) for op in ops}
        card = completion_card.completion(
            session.session_key,
            stats,
            top,
            max(0, len(files) - completion_card.TOP_FILES),
            nonces=nonces,
            can_back=can_back,
            quick_reply=quick,
        )
        markdown = f"✅ Turn complete · {stats}"
        await self.post_in_thread(session, markdown, card=cards.fit_card(markdown, card))

    async def _close_card(self, request_id: str, outcome: str, by: str | None) -> None:
        """Edit the card's message once with its outcome."""
        request = await self._registry.get_pending(request_id)
        if request is None or request.webex_message_id is None:
            return
        suffix = f" ({by})" if by and by != "phone" else ""
        await self.outbox.edit(request.webex_message_id, f"{outcome}{suffix}")

    async def tick(self) -> None:
        """Show pending tool calls once their interval is up, flush text that has gone quiet for
        flush_idle_s, and notice status changes no event announced (a stop, an app ending a session).
        Called about once a second."""
        now = self._clock()
        for key, thread in list(self._threads.items()):
            has_text = bool(thread.text.strip())
            polled = now - thread.status_checked >= ROOT_POLL_S
            if not (has_text or thread.progress_dirty or polled):
                continue
            session = await self._registry.get_session(key)
            if session is None:
                continue
            async with self._lock(key):
                if polled:
                    thread.status_checked = now
                    await self.refresh_root(session)
                await self._progress(session, thread)
                if has_text and now - thread.last_activity >= self._config.flush_idle_s:
                    await self._flush(session, thread, final=True)
                    thread.streamed = True
