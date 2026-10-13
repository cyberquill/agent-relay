"""The approval broker: adapter-held requests answered natively, deadline sweeps, questions and
the read-only policy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from agent_relay.core.approvals import is_read_only
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.models.config import READ_ONLY_TOOLS, RelayConfig
from agent_relay.models.events import ApprovalRequested, ApprovalResolved, Harness, QuestionAsked
from agent_relay.models.sessions import PendingRequest, SessionRecord
from tests.fakes.adapters import EmittingAdapter

SESSION = SessionRecord(session_key="opencode:s1", harness=Harness.OPENCODE, harness_id="s1", workspace="/w")


@pytest.fixture
async def setup(registry: Registry) -> AsyncIterator[tuple[Core, EmittingAdapter]]:
    core = Core(RelayConfig(), registry)
    adapter = EmittingAdapter(count=0)
    core.router.add_adapter(adapter)
    await registry.create_session(SESSION)
    yield core, adapter
    await core.router.stop()


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("read_file", True),
        ("Read", True),
        ("grep", True),
        ("mcp__docs/get_page", True),
        ("semantic_search", True),
        ("todowrite", True),
        ("bash", False),
        ("edit", False),
        ("run_in_terminal", False),
        (None, False),
    ],
)
def test_read_only_policy(tool: str | None, expected: bool) -> None:
    assert is_read_only(tool, list(READ_ONLY_TOOLS)) is expected


async def test_adapter_held_approval_is_answered_natively(setup: tuple[Core, EmittingAdapter]) -> None:
    core, adapter = setup
    await core.router.publish(
        ApprovalRequested(
            harness=Harness.OPENCODE,
            session_key=SESSION.session_key,
            request_id="perm_1",
            tool="bash",
            detail="ls",
        )
    )
    await core.router.join()
    pending = await core.registry.get_pending("perm_1")
    assert pending is not None and pending.payload["source"] == "adapter"
    assert await core.service.answer_approval("perm_1", "allow")
    assert adapter.approvals == [("perm_1", "allow")]
    assert not await core.service.answer_approval("perm_1", "deny"), "first answer wins"


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


async def test_sweep_denies_expired_and_keeps_live(setup: tuple[Core, EmittingAdapter]) -> None:
    core, adapter = setup
    resolved: list[ApprovalResolved] = []

    class Sink:
        async def handle(self, event: object) -> None:
            if isinstance(event, ApprovalResolved):
                resolved.append(event)

    core.router.subscribe(Sink())
    past = datetime.now(UTC) - timedelta(seconds=1)
    future = datetime.now(UTC) + timedelta(hours=1)
    for request_id, deadline in (("old", past), ("new", future)):
        await core.registry.add_pending(
            PendingRequest(
                request_id=request_id,
                kind="approval",
                session_key=SESSION.session_key,
                payload={"source": "adapter", "native_id": request_id, "tool": "bash"},
                deadline=deadline,
            )
        )
    await core.registry.add_pending(
        PendingRequest(
            request_id="oldq",
            kind="question",
            session_key=SESSION.session_key,
            payload={"source": "adapter"},
            deadline=past,
        )
    )
    assert await core.broker.sweep() == 2
    await core.router.join()
    assert adapter.approvals == [("old", "deny")]
    assert adapter.answers == [("oldq", None)]
    assert [r.request_id for r in resolved] == ["old"] and resolved[0].by == "deadline"
    assert [p.request_id for p in await core.registry.open_pending()] == ["new"]


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


async def test_explicit_deadline_override(setup: tuple[Core, EmittingAdapter]) -> None:
    core, _ = setup
    outcome = await core.broker.request(SESSION, "bash", "rm -rf /tmp/x", deadline_s=0.05)
    assert outcome.decision == "deny" and outcome.by == "deadline"
    assert not await core.registry.open_pending()
