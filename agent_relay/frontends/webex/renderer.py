"""Turn router events into Webex thread messages within the platform's limits.

- One thread per session: the root is an editable markdown status line (edit budget 8).
- `text.delta` is buffered, flushed at a paragraph boundary once ≥ flush_bytes, or after
  flush_idle_s of quiet; no message exceeds max_message_bytes.
- Tool events become batched one-liners. Final answers over the limit become `answer.md`.
- Pacing: at most one message per pace_s per thread.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import JsonValue

from agent_relay.core.registry import Registry
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.api import EditLimitError, Message, WebexApi, text_bytes
from agent_relay.log import get_logger, log_body
from agent_relay.models.config import RendererConfig
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
from agent_relay.models.sessions import SessionRecord

log = get_logger(__name__)

ROOM_KEY = "webex.room_id"
APPROVAL_OUTCOME = {
    "allow": "✅ Allowed",
    "allow_session": "✅ Allowed for this session",
    "deny": "❌ Denied",
    "desk": "🖥️ Away mode off; answer it at the desk",
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
    """Posts to the user's 1:1 room, learning its id from the first reply."""

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


@dataclass
class _Thread:
    session_key: str
    root_id: str
    text: str = ""
    tools: list[str] = field(default_factory=list)
    streamed: bool = False
    last_activity: float = 0.0
    root_edits: int = 0
    last_root: str = ""


class WebexRenderer:
    def __init__(
        self,
        outbox: Outbox,
        registry: Registry,
        config: RendererConfig,
        approval_deadline_s: int = 600,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._approval_deadline_min = max(1, approval_deadline_s // 60)
        self.outbox = outbox
        self._registry = registry
        self._config = config
        self._clock = clock
        self._pacer = Pacer(config.pace_s, clock, sleep)
        self._threads: dict[str, _Thread] = {}
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
        await self._registry.set_session_fields(session.session_key, webex_thread_id=message.id)
        self._threads[session.session_key] = _Thread(session.session_key, message.id, last_root=markdown)
        return message.id

    async def refresh_root(self, session: SessionRecord) -> None:
        """Edit the root status line, within the edit budget. Unchanged lines cost nothing."""
        root_id = await self.thread_for(session)
        thread = self._threads[session.session_key]
        markdown = cards.root_markdown(session)
        if markdown == thread.last_root or thread.root_edits >= self._config.root_edit_budget:
            return
        if await self.outbox.edit(root_id, markdown):
            thread.root_edits += 1
            thread.last_root = markdown
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
        match event:
            case SessionStarted():
                await self.refresh_root(session)
            case TextDelta(text=text):
                thread.text += text
                thread.streamed = True
                await self._flush(session, thread, final=False)
            case ToolStarted(name=name, detail=detail):
                thread.tools.append(f"🔧 {name}{f': {detail}' if detail else ''}")
            case ToolFinished(name=name, ok=ok, summary=summary):
                if not ok:
                    thread.tools.append(f"❌ {name}{f': {summary}' if summary else ''}")
            case TurnCompleted(text=text):
                if not thread.streamed and text:
                    thread.text += text
                await self._flush(session, thread, final=True)
                thread.streamed = False
            case SessionParked():
                await self._flush(session, thread, final=True)
                await self.post_in_thread(session, "🅿️ Parked. Reply here to continue, or `/back` to release.")
            case SessionReleased(reason=reason):
                await self.post_in_thread(session, f"▶️ Released ({reason}).")
            case ErrorEvent(message=message):
                await self._flush(session, thread, final=True)
                await self.post_in_thread(session, f"⚠️ {message}")
            case ApprovalRequested(request_id=request_id, tool=tool, detail=detail):
                await self._flush(session, thread, final=True)
                card = cards.approval(
                    request_id, cards.session_label(session), detail or tool, self._approval_deadline_min
                )
                posted = await self.post_in_thread(session, f"Approval needed: {detail or tool}", card=card)
                await self._registry.set_pending_message(request_id, posted.id)
            case QuestionAsked(request_id=request_id, question=question, options=options):
                await self._flush(session, thread, final=True)
                card = cards.question(request_id, cards.session_label(session), question, options)
                posted = await self.post_in_thread(session, f"Question: {question}", card=card)
                await self._registry.set_pending_message(request_id, posted.id)
            case ApprovalResolved(request_id=request_id, decision=decision, by=by):
                await self._close_card(request_id, APPROVAL_OUTCOME[decision], by)
            case QuestionAnswered(request_id=request_id, answer=answer):
                await self._close_card(request_id, f"💬 Answered: {answer}" if answer else "⏭️ Skipped", None)

    async def _flush(self, session: SessionRecord, thread: _Thread, final: bool) -> None:
        cfg = self._config
        if thread.tools:
            lines = "\n".join(thread.tools)
            thread.tools = []
            for piece in chunk_text(lines, cfg.max_message_bytes):
                await self.post_in_thread(session, piece)
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
            for piece in chunk_text(text, cfg.max_message_bytes):
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
        for piece in chunk_text(ready, cfg.max_message_bytes):
            await self.post_in_thread(session, piece)

    async def _close_card(self, request_id: str, outcome: str, by: str | None) -> None:
        """Edit the card's message once with its outcome."""
        request = await self._registry.get_pending(request_id)
        if request is None or request.webex_message_id is None:
            return
        suffix = f" ({by})" if by and by != "phone" else ""
        await self.outbox.edit(request.webex_message_id, f"{outcome}{suffix}")

    async def tick(self) -> None:
        """Flush threads that have been quiet for flush_idle_s. Called about once a second."""
        now = self._clock()
        for key, thread in list(self._threads.items()):
            if not (thread.text.strip() or thread.tools):
                continue
            if now - thread.last_activity < self._config.flush_idle_s:
                continue
            session = await self._registry.get_session(key)
            if session is None:
                continue
            async with self._lock(key):
                await self._flush(session, thread, final=True)
                thread.streamed = True
