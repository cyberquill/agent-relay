"""The approval broker: adapter-held tool permissions are allowed at once, questions keep their
card and deadline, and the sweep closes what ran out."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import ApprovalRequested, Harness, Origin, QuestionAsked
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus
from tests.fakes.adapters import EmittingAdapter

SESSION = SessionRecord(
    session_key="opencode:s1",
    harness=Harness.OPENCODE,
    harness_id="s1",
    workspace="/w",
    origin=Origin.PHONE,
)
DESK = SessionRecord(
    session_key="opencode:d1",
    harness=Harness.OPENCODE,
    harness_id="d1",
    workspace="/w",
    origin=Origin.DESK,
    status=SessionStatus.RUNNING,
)


@pytest.fixture
async def setup(registry: Registry) -> AsyncIterator[tuple[Core, EmittingAdapter]]:
    core = Core(RelayConfig(), registry)
    adapter = EmittingAdapter(count=0)
    core.router.add_adapter(adapter)
    await registry.create_session(SESSION)
    await registry.create_session(DESK)
    yield core, adapter
    await core.router.stop()


async def _request(core: Core, session: SessionRecord, request_id: str) -> None:
    await core.router.publish(
        ApprovalRequested(
            harness=session.harness,
            session_key=session.session_key,
            request_id=request_id,
            tool="bash",
            detail="ls",
        )
    )
    await core.router.join()


async def test_adapter_held_permission_is_allowed_natively_and_leaves_no_row(
    setup: tuple[Core, EmittingAdapter],
) -> None:
    core, adapter = setup
    await _request(core, SESSION, "perm_1")
    assert adapter.approvals == [("perm_1", "allow")]
    assert await core.registry.get_pending("perm_1") is None


async def test_a_desk_session_is_allowed_while_away_and_left_to_the_app_at_the_desk(
    setup: tuple[Core, EmittingAdapter],
) -> None:
    core, adapter = setup
    await _request(core, DESK, "perm_desk")
    assert adapter.approvals == [], "at the desk the app asks for itself"
    await away.set_away(core.registry, True)
    await _request(core, DESK, "perm_away")
    assert adapter.approvals == [("perm_away", "allow")]


async def test_nothing_is_allowed_after_a_panic(setup: tuple[Core, EmittingAdapter]) -> None:
    core, adapter = setup
    paths.pause_flag().parent.mkdir(parents=True, exist_ok=True)
    paths.pause_flag().write_text("paused", encoding="utf-8")
    await _request(core, SESSION, "perm_paused")
    assert adapter.approvals == []


async def test_a_permission_the_app_cannot_be_told_about_is_not_swallowed_silently(
    setup: tuple[Core, EmittingAdapter], caplog: pytest.LogCaptureFixture
) -> None:
    core, adapter = setup
    seen: list[ApprovalRequested] = []

    class Sink:
        async def handle(self, event: object) -> None:
            if isinstance(event, ApprovalRequested):
                seen.append(event)

    async def refuse(*_: object, **__: object) -> None:
        raise ConnectionError("socket closed")

    adapter.answer_approval = refuse  # type: ignore[method-assign]
    core.router.subscribe(Sink())
    with caplog.at_level("WARNING"):
        await _request(core, SESSION, "perm_2")
    assert [e.request_id for e in seen] == ["perm_2"], "the event stays visible to the sinks"
    assert "could not allow" in caplog.text
    assert await core.registry.get_pending("perm_2") is None


async def test_adapter_held_question_and_duplicate_event(setup: tuple[Core, EmittingAdapter]) -> None:
    core, adapter = setup
    asked = QuestionAsked(
        harness=Harness.OPENCODE,
        session_key=SESSION.session_key,
        request_id="q_1",
        question="Which DB?",
        options=("pg",),
    )
    await core.router.publish(asked)
    await core.router.publish(asked)
    await core.router.join()
    assert len(await core.registry.open_pending("question")) == 1
    assert await core.service.answer_question("q_1", "pg")
    assert adapter.answers == [("q_1", "pg")]


async def test_sweep_skips_expired_questions_and_closes_old_approval_rows(
    setup: tuple[Core, EmittingAdapter],
) -> None:
    core, adapter = setup
    past = datetime.now(UTC) - timedelta(seconds=1)
    future = datetime.now(UTC) + timedelta(hours=1)
    for request_id, kind, deadline in (
        ("oldq", "question", past),
        ("newq", "question", future),
        ("olda", "approval", past),
    ):
        await core.registry.add_pending(
            PendingRequest(
                request_id=request_id,
                kind="question" if kind == "question" else "approval",
                session_key=SESSION.session_key,
                payload={"source": "adapter", "native_id": request_id},
                deadline=deadline,
            )
        )
    assert await core.broker.sweep() == 2
    await core.router.join()
    assert adapter.answers == [("oldq", None)]
    assert adapter.approvals == [], "an old approval row is closed, not answered"
    assert [p.request_id for p in await core.registry.open_pending()] == ["newq"]


async def test_hook_question_answer_and_expiry(setup: tuple[Core, EmittingAdapter]) -> None:
    core, _ = setup
    seen: list[QuestionAsked] = []

    class Sink:
        async def handle(self, event: object) -> None:
            if isinstance(event, QuestionAsked):
                seen.append(event)

    core.router.subscribe(Sink())
    asking = asyncio.create_task(core.broker.ask(SESSION, "Proceed?", ("yes", "no"), deadline_s=2))
    for _ in range(200):
        if seen:
            break
        await asyncio.sleep(0.005)
    assert await core.service.answer_question(seen[0].request_id, "yes")
    assert await asking == "yes"
    assert await core.broker.ask(SESSION, "Again?", (), deadline_s=0.05) is None


async def test_the_question_deadline_comes_from_the_questions_config(
    setup: tuple[Core, EmittingAdapter],
) -> None:
    core, _ = setup
    assert core.config.questions.deadline_s == RelayConfig().questions.deadline_s
    answer, by = await core.broker.ask_outcome(SESSION, "Anyone?", (), deadline_s=0.05)
    assert (answer, by) == (None, "deadline")
    assert not await core.registry.open_pending()
