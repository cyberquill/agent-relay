"""OpenCode adapter: the managed `opencode serve` plus any instance the plugin registered.

Every server's `/global/event` stream is read by its own supervised task. Events are rendered only for
sessions the relay knows (started from the phone, or registered while away mode is on), so nothing
reaches the phone while you are at the desk. Approvals that the plugin already got from the phone are
answered here without a second card.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence

import httpx

from agent_relay.adapters.base import (
    Adapter,
    AdapterUnavailableError,
    Decision,
    EventPublisher,
    ModelInfo,
    StartRequest,
)
from agent_relay.adapters.opencode import mapping as m
from agent_relay.adapters.opencode.client import OpenCodeClient, OpenCodeError, ServerEvent
from agent_relay.core import away
from agent_relay.core.hooks import APPROVED_CALL_KEY, OPENCODE_SERVER_PREFIX
from agent_relay.core.registry import Registry
from agent_relay.core.service import RelayError
from agent_relay.log import get_logger
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.config import OpenCodeConfig
from agent_relay.models.events import (
    ApprovalRequested,
    ApprovalResolved,
    ErrorEvent,
    Event,
    Harness,
    Origin,
    QuestionAnswered,
    QuestionAsked,
    SessionStarted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

SERVER_EXTRA = "server"
IGNORE_TTL_S = 5.0
RESCAN_S = 5.0

type Sleep = Callable[[float], Awaitable[None]]


class OpenCodeAdapter(Adapter):
    harness = Harness.OPENCODE
    capabilities = Capabilities(
        cold_start=True,
        model_select=True,
        token_stream=True,
        approve=True,
        question=True,
        cancel=True,
        wake_idle=True,
        attach_files=True,
    )

    def __init__(
        self,
        config: OpenCodeConfig,
        registry: Registry,
        password: str | None,
        http: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        backoff: tuple[float, float] = (1.0, 60.0),
    ) -> None:
        self._config = config
        self._registry = registry
        self._http = http or httpx.AsyncClient()
        self._owns_http = http is None
        self._sleep = sleep
        self._backoff = backoff
        auth = httpx.BasicAuth(config.username, password) if password else None
        self.primary = OpenCodeClient(self._http, config.url, auth)
        self._clients: dict[str, OpenCodeClient] = {self.primary.base_url: self.primary}
        self._streams: dict[str, asyncio.Task[None]] = {}
        self._ignored: dict[str, float] = {}
        self._question_counts: dict[str, int] = {}
        self._publisher: EventPublisher | None = None

    # discovery and streaming

    def client_for(self, url: str) -> OpenCodeClient:
        key = url.rstrip("/")
        if key not in self._clients:
            self._clients[key] = OpenCodeClient(self._http, key)
        return self._clients[key]

    async def server_urls(self) -> list[str]:
        registered = await self._registry.kv_prefix(OPENCODE_SERVER_PREFIX)
        urls = [self.primary.base_url]
        for key in registered:
            url = key.removeprefix(OPENCODE_SERVER_PREFIX).rstrip("/")
            if url and url not in urls:
                urls.append(url)
        return urls

    async def run(self, publisher: EventPublisher) -> None:
        self._publisher = publisher
        try:
            while True:
                for url in await self.server_urls():
                    task = self._streams.get(url)
                    if task is None or task.done():
                        self._streams[url] = asyncio.create_task(
                            self._follow(self.client_for(url), publisher), name=f"opencode:{url}"
                        )
                await self._sleep(RESCAN_S)
        finally:
            for task in self._streams.values():
                task.cancel()
            await asyncio.gather(*self._streams.values(), return_exceptions=True)
            self._streams.clear()

    async def _follow(self, client: OpenCodeClient, publisher: EventPublisher) -> None:
        """Read one server's events forever, reconnecting quietly with backoff."""
        low, high = self._backoff
        delay = low
        while True:
            mapper = m.EventMapper()
            try:
                async for event in client.events():
                    delay = low
                    await self.consume(client, mapper, event, publisher)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, OpenCodeError) as exc:
                log.debug(f"opencode {client.base_url} stream: {exc}")
            except Exception:
                log.exception(f"opencode {client.base_url} stream failed")
            if client is not self.primary and not await client.healthy():
                await self._registry.kv_delete(f"{OPENCODE_SERVER_PREFIX}{client.base_url}")
                await self._registry.kv_delete(f"{OPENCODE_SERVER_PREFIX}{client.base_url}/")
                log.info(f"opencode {client.base_url} is gone; forgotten")
                return
            await self._sleep(delay)
            delay = min(delay * 2, high)

    # event handling

    async def plugin_active(self, client: OpenCodeClient) -> bool:
        """The plugin reports tool calls through relay-hook, so the adapter does not repeat them."""
        registered = await self._registry.kv_prefix(OPENCODE_SERVER_PREFIX)
        return any(k.removeprefix(OPENCODE_SERVER_PREFIX).rstrip("/") == client.base_url for k in registered)

    async def resolve(
        self, sid: str, directory: str | None, client: OpenCodeClient, title: str | None = None
    ) -> SessionRecord | None:
        found = await self._registry.find_session(self.harness, sid)
        if found is not None:
            return found
        now = time.monotonic()
        if self._ignored.get(sid, 0.0) > now:
            return None
        if not await away.is_away(self._registry, directory):
            self._ignored[sid] = now + IGNORE_TTL_S
            return None
        record = await self._registry.create_session(
            SessionRecord(
                session_key=f"{self.harness.value}:{sid}",
                harness=self.harness,
                harness_id=sid,
                workspace=directory,
                title=title,
                status=SessionStatus.RUNNING,
                origin=Origin.DESK,
                extra={SERVER_EXTRA: client.base_url},
            )
        )
        await self._emit(SessionStarted, record, title=title)
        return record

    async def _emit[E: Event](self, kind: type[E], session: SessionRecord, **fields: object) -> None:
        assert self._publisher is not None
        event = kind(
            harness=self.harness, session_key=session.session_key, workspace=session.workspace, **fields
        )
        await self._publisher.publish(event)

    async def consume(
        self, client: OpenCodeClient, mapper: m.EventMapper, event: ServerEvent, publisher: EventPublisher
    ) -> None:
        self._publisher = publisher
        for record in mapper.map(event):
            title = record.title if isinstance(record, m.Created) else None
            session = await self.resolve(record.sid, event.directory, client, title)
            if session is not None:
                await self._apply(client, session, record)

    async def _apply(self, client: OpenCodeClient, session: SessionRecord, record: m.Record) -> None:
        match record:
            case m.Delta(text=text):
                await self._emit(TextDelta, session, text=text)
            case m.ToolStart(call_id=call_id, name=name, detail=detail):
                if not await self.plugin_active(client):
                    await self._emit(ToolStarted, session, tool_id=call_id, name=name, detail=detail)
            case m.ToolEnd(call_id=call_id, name=name, ok=ok, summary=summary):
                if not await self.plugin_active(client):
                    await self._emit(
                        ToolFinished, session, tool_id=call_id, name=name, ok=ok, summary=summary
                    )
            case m.Busy():
                if session.status is not SessionStatus.RUNNING:
                    await self.report_status(session.session_key, SessionStatus.RUNNING, "adapter report")
            case m.Idle(text=text):
                await self.report_status(session.session_key, SessionStatus.IDLE, "adapter report")
                await self._emit(TurnCompleted, session, text=text)
            case m.Failed(message=message):
                await self._emit(ErrorEvent, session, message=message)
            case m.PermissionAsk():
                await self._permission(client, session, record)
            case m.PermissionDone(request_id=request_id, decision=decision):
                if await self._still_open(request_id):
                    await self._emit(
                        ApprovalResolved, session, request_id=request_id, decision=decision, by="desk"
                    )
            case m.QuestionAsk(request_id=request_id, question=question, options=options, count=count):
                self._question_counts[request_id] = count
                await self._emit(
                    QuestionAsked, session, request_id=request_id, question=question, options=options
                )
            case m.QuestionDone(request_id=request_id, answer=answer):
                self._question_counts.pop(request_id, None)
                if await self._still_open(request_id):
                    await self._emit(QuestionAnswered, session, request_id=request_id, answer=answer)
            case m.Created():
                return

    async def _still_open(self, request_id: str) -> bool:
        pending = await self._registry.get_pending(request_id)
        return pending is not None and pending.status == "open"

    async def _permission(self, client: OpenCodeClient, session: SessionRecord, ask: m.PermissionAsk) -> None:
        if ask.call_id:
            prior = await self._registry.kv_get(APPROVED_CALL_KEY.format(call_id=ask.call_id))
            if prior in ("allow", "deny"):
                decision: Decision = "allow" if prior == "allow" else "deny"
                await client.reply_permission(session.harness_id, ask.request_id, decision, session.workspace)
                log.info(f"opencode permission {ask.request_id} answered from the earlier phone decision")
                return
        await self._emit(
            ApprovalRequested, session, request_id=ask.request_id, tool=ask.tool, detail=ask.detail
        )

    # the Adapter contract

    def _client_of(self, session: SessionRecord) -> OpenCodeClient:
        server = session.extra.get(SERVER_EXTRA)
        return self.client_for(server) if isinstance(server, str) and server else self.primary

    async def health(self) -> tuple[bool, str]:
        if await self.primary.healthy():
            return True, f"opencode serve at {self.primary.base_url}"
        return False, f"opencode serve is not reachable at {self.primary.base_url}"

    async def list_models(self) -> list[ModelInfo]:
        try:
            return await self.primary.models()
        except (httpx.HTTPError, OpenCodeError) as exc:
            raise AdapterUnavailableError(f"OpenCode models unavailable: {exc}") from exc

    async def start_session(
        self, request: StartRequest, attachments: Sequence[Attachment] = ()
    ) -> SessionRecord:
        title = request.prompt.strip().splitlines()[0][:80] if request.prompt.strip() else "phone session"
        try:
            sid = await self.primary.create_session(request.workspace, title)
        except (httpx.HTTPError, OpenCodeError) as exc:
            raise AdapterUnavailableError(
                f"opencode serve is not reachable at {self.primary.base_url}: {exc}"
            ) from exc
        record = await self._registry.create_session(
            SessionRecord(
                session_key=f"{self.harness.value}:{sid}",
                harness=self.harness,
                harness_id=sid,
                workspace=request.workspace,
                model=request.model,
                title=title,
                status=SessionStatus.RUNNING,
                origin=Origin.PHONE,
                extra={SERVER_EXTRA: self.primary.base_url},
            )
        )
        await self.primary.prompt(sid, request.workspace, request.prompt, request.model, attachments)
        return record

    async def send_prompt(
        self, session: SessionRecord, text: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        await self._client_of(session).prompt(
            session.harness_id, session.workspace, text, session.model, attachments
        )

    async def wake(self, session: SessionRecord, text: str) -> None:
        await self.send_prompt(session, text)

    async def set_model(self, session: SessionRecord, model: str) -> None:
        known = {info.id for info in await self.list_models()}
        if model not in known:
            raise RelayError(f"OpenCode has no model `{model}`. Try `/models`.")
        log.info(f"{session.session_key} will use {model} from the next prompt")

    async def cancel(self, session: SessionRecord) -> None:
        await self._client_of(session).abort(session.harness_id, session.workspace)

    async def answer_approval(
        self, session: SessionRecord, request_id: str, decision: Decision, reason: str | None = None
    ) -> None:
        await self._client_of(session).reply_permission(
            session.harness_id, request_id, decision, session.workspace, message=reason
        )

    async def answer_question(self, session: SessionRecord, request_id: str, answer: str | None) -> None:
        count = self._question_counts.pop(request_id, 1)
        answers: list[list[str]] | None = None
        if answer is not None:
            lines = [line.strip() for line in answer.splitlines() if line.strip()] if count > 1 else [answer]
            answers = [[a] for a in lines[:count]] + [[] for _ in range(count - len(lines[:count]))]
        await self._client_of(session).reply_question(request_id, answers, session.workspace)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
