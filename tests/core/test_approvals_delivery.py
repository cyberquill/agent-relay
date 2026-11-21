"""An answer that never reached the app is not an answer: the question reopens and the card can be retried."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from agent_relay.core.approvals import SWEEP_GIVE_UP
from agent_relay.core.registry import Registry
from agent_relay.core.service import DeliveryError
from agent_relay.daemon import Core
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import Harness, Origin, QuestionAnswered
from agent_relay.models.sessions import PendingKind, PendingRequest, SessionRecord
from tests.fakes.adapters import EmittingAdapter

SESSION = SessionRecord(
    session_key="opencode:s1",
    harness=Harness.OPENCODE,
    harness_id="s1",
    workspace="/w",
    origin=Origin.PHONE,
)

type Flaky = tuple[Core, "FlakyAdapter", list[QuestionAnswered]]


class FlakyAdapter(EmittingAdapter):
    """Fails to answer until `broken` is cleared."""

    def __init__(self) -> None:
        super().__init__(count=0)
        self.broken = True

    async def answer_question(self, session: SessionRecord, request_id: str, answer: str | None) -> None:
        if self.broken:
            raise ConnectionError("socket closed")
        await super().answer_question(session, request_id, answer)


@pytest.fixture
async def flaky(registry: Registry) -> AsyncIterator[Flaky]:
    core = Core(RelayConfig(), registry)
    adapter = FlakyAdapter()
    core.router.add_adapter(adapter)
    await registry.create_session(SESSION)
    resolved: list[QuestionAnswered] = []

    class Sink:
        async def handle(self, event: object) -> None:
            if isinstance(event, QuestionAnswered):
                resolved.append(event)

    core.router.subscribe(Sink())
    yield core, adapter, resolved
    await core.router.stop()


async def _pending(
    core: Core,
    request_id: str,
    kind: PendingKind = "question",
    deadline: datetime | None = None,
    source: str = "adapter",
) -> PendingRequest:
    request = PendingRequest(
        request_id=request_id,
        kind=kind,
        session_key=SESSION.session_key,
        payload={"source": source, "native_id": request_id, "question": "Which DB?"},
        deadline=deadline or datetime.now(UTC) + timedelta(hours=1),
    )
    await core.registry.add_pending(request)
    return request


async def test_a_question_that_never_reached_the_app_is_reopened(flaky: Flaky) -> None:
    core, adapter, resolved = flaky
    original = await _pending(core, "q1")
    with pytest.raises(DeliveryError) as err:
        await core.service.answer_question("q1", "pg")
    assert str(err.value) == "Couldn't reach OpenCode: socket closed. Tap again or answer at the desk."
    reopened = await core.registry.get_pending("q1")
    assert reopened is not None
    assert (reopened.status, reopened.answer, reopened.deadline) == ("open", None, original.deadline)
    await core.router.join()
    assert resolved == [], "the phone must not be told it was answered"
    adapter.broken = False
    assert await core.service.answer_question("q1", "pg"), "the card can be answered again"
    await core.router.join()
    assert adapter.answers == [("q1", "pg")]
    assert [r.answer for r in resolved] == ["pg"]


async def test_an_adapter_that_is_not_connected_is_a_delivery_error(registry: Registry) -> None:
    core = Core(RelayConfig(), registry)
    await registry.create_session(SESSION)
    await _pending(core, "q1")
    with pytest.raises(DeliveryError, match="OpenCode: it is not connected"):
        await core.service.answer_question("q1", None)
    assert [p.request_id for p in await registry.open_pending()] == ["q1"]
    await core.router.stop()


async def test_hook_held_questions_do_not_depend_on_an_adapter(flaky: Flaky) -> None:
    core, adapter, _ = flaky
    await _pending(core, "h1", source="hook")
    assert await core.service.answer_question("h1", "pg")
    assert adapter.answers == []
    assert await core.registry.open_pending() == []


async def test_sweep_keeps_going_past_a_question_it_cannot_deliver(flaky: Flaky) -> None:
    core, adapter, _ = flaky
    past = datetime.now(UTC) - timedelta(seconds=5)
    await _pending(core, "stuck", deadline=past)
    await _pending(core, "hook", deadline=past, source="hook")
    assert await core.broker.sweep() == 1
    assert [p.request_id for p in await core.registry.open_pending()] == ["stuck"]
    adapter.broken = False
    assert await core.broker.sweep() == 1
    assert adapter.answers == [("stuck", None)]


async def test_sweep_gives_up_on_an_app_that_stays_unreachable(flaky: Flaky) -> None:
    core, _, _ = flaky
    await _pending(core, "gone", deadline=datetime.now(UTC) - SWEEP_GIVE_UP - timedelta(seconds=5))
    assert await core.broker.sweep() == 1
    assert await core.registry.open_pending() == []
    closed = await core.registry.get_pending("gone")
    assert closed is not None and closed.status == "expired"
