"""The session lifecycle: validated moves and the cleanup that goes with ending or stopping a session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_relay.adapters.base import UnsupportedError
from agent_relay.core import away
from agent_relay.core.approvals import ALLOW_SESSION_KEY
from agent_relay.core.hooks import RECENT_APPROVAL_KEY
from agent_relay.core.keys import RELEASE_KEY, session_signal
from agent_relay.core.lifecycle import ALLOWED, InvalidTransitionError, UnknownSessionError
from agent_relay.core.registry import Registry
from agent_relay.core.service import RelayError
from agent_relay.daemon import Core
from agent_relay.models.attachments import Attachment
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import (
    ApprovalResolved,
    Event,
    Harness,
    Origin,
    QuestionAnswered,
    SessionReleased,
)
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus
from tests.fakes.adapters import EmittingAdapter

H = Harness.OPENCODE
SRC = Path(__file__).resolve().parents[2] / "agent_relay"


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)


class FlakyAdapter(EmittingAdapter):
    """Cancelling one particular session fails."""

    def __init__(self, failing: str) -> None:
        super().__init__(count=0)
        self.failing = failing

    async def cancel(self, session: SessionRecord) -> None:
        if session.session_key == self.failing:
            raise RuntimeError("boom")
        await super().cancel(session)


class NoResumeAdapter(EmittingAdapter):
    async def send_prompt(
        self, session: SessionRecord, text: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        raise UnsupportedError(self.harness, "prompt")


class ReleasingAdapter(EmittingAdapter):
    def __init__(self, fail: bool = False) -> None:
        super().__init__(count=0)
        self.fail = fail
        self.released: list[str] = []

    async def release_session(self, session: SessionRecord) -> None:
        self.released.append(session.session_key)
        if self.fail:
            raise RuntimeError("boom")


@pytest.mark.parametrize("fail", [False, True])
async def test_ending_a_session_tells_its_adapter_even_if_the_adapter_fails(
    core: tuple[Core, Recorder], fail: bool
) -> None:
    built, _ = core
    adapter = ReleasingAdapter(fail)
    built.router.add_adapter(adapter)
    await _session(built)
    await built.lifecycle.transition("opencode:s1", SessionStatus.IDLE, "turn over")
    assert adapter.released == []
    ended = await built.lifecycle.transition("opencode:s1", SessionStatus.ENDED, "done")
    assert ended.status is SessionStatus.ENDED
    assert adapter.released == ["opencode:s1"]


@pytest.fixture
async def core(registry: Registry) -> AsyncIterator[tuple[Core, Recorder]]:
    built = Core(RelayConfig(), registry)
    recorder = Recorder()
    built.router.subscribe(recorder)
    yield built, recorder
    await built.router.stop()


async def _session(
    core: Core, key: str = "opencode:s1", status: SessionStatus = SessionStatus.RUNNING
) -> SessionRecord:
    record = SessionRecord(
        session_key=key, harness=H, harness_id=key.split(":")[1], workspace="/w", origin=Origin.PHONE
    )
    created = await core.registry.create_session(record)
    await core.registry.set_session_status(key, status)
    return created


async def _pending(core: Core, key: str, request_id: str, kind: str = "approval") -> None:
    await core.registry.add_pending(
        PendingRequest(
            request_id=request_id,
            kind="approval" if kind == "approval" else "question",
            session_key=key,
            payload={"source": "hook"},
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        )
    )


async def test_moves_are_validated_and_idempotent(core: tuple[Core, Recorder]) -> None:
    built, _ = core
    await _session(built)
    key = "opencode:s1"
    assert (
        await built.lifecycle.transition(key, SessionStatus.RUNNING, "again")
    ).status is SessionStatus.RUNNING
    with pytest.raises(InvalidTransitionError):
        await built.lifecycle.transition(key, SessionStatus.ACTIVE, "never goes back")
    await built.lifecycle.transition(key, SessionStatus.ENDED, "done")
    with pytest.raises(InvalidTransitionError):
        await built.lifecycle.transition(key, SessionStatus.PARKED, "an ended session cannot park")
    with pytest.raises(UnknownSessionError):
        await built.lifecycle.transition("nope", SessionStatus.IDLE, "x")
    assert await built.lifecycle.observe("nope", SessionStatus.IDLE, "x") is None
    assert await built.lifecycle.observe(key, SessionStatus.PARKED, "late report") is None
    assert (await built.registry.get_session(key)) is not None
    assert SessionStatus.ENDED not in ALLOWED[SessionStatus.ENDED]


async def test_ending_a_session_cleans_up_after_it(core: tuple[Core, Recorder]) -> None:
    built, rec = core
    key = "opencode:s1"
    await _session(built, status=SessionStatus.PARKED)
    await _pending(built, key, "a1")
    await _pending(built, key, "q1", "question")
    await built.registry.enqueue(key, "prompt", "hello")
    allow = ALLOW_SESSION_KEY.format(session_key=key, tool="bash")
    recent = RECENT_APPROVAL_KEY.format(session_key=key, digest="d1")
    other = RECENT_APPROVAL_KEY.format(session_key="opencode:other", digest="d1")
    for flag in (allow, recent, other):
        await built.registry.kv_set(flag, "1")
    woken = asyncio.create_task(built.signals.wait(session_signal(key), 5))
    await asyncio.sleep(0)

    await built.lifecycle.transition(key, SessionStatus.ENDED, "session ended")
    await built.router.join()

    assert await woken, "the parked hook is woken"
    assert await built.registry.kv_get(RELEASE_KEY.format(session_key=key)) == "session ended"
    assert await built.registry.open_pending() == []
    approval = await built.registry.get_pending("a1")
    assert approval is not None and approval.status == "expired"
    assert approval.answer == {"decision": "deny", "by": "ended"}
    question = await built.registry.get_pending("q1")
    assert question is not None and question.answer == {"answer": None, "by": "ended"}
    assert await built.registry.pop_queued(key, ("prompt",)) == []
    assert await built.registry.kv_get(allow) is None and await built.registry.kv_get(recent) is None
    assert await built.registry.kv_get(other) == "1", "another session's flags stay"
    assert [e.by for e in rec.events if isinstance(e, ApprovalResolved)] == ["ended"]
    assert [e.by for e in rec.events if isinstance(e, QuestionAnswered)] == ["ended"]
    assert any(isinstance(e, SessionReleased) for e in rec.events)


async def test_a_hook_waiting_on_an_approval_is_denied_when_its_session_ends(
    core: tuple[Core, Recorder],
) -> None:
    built, _ = core
    session = await _session(built, "opencode:s1", SessionStatus.RUNNING)
    waiting = asyncio.create_task(built.broker.request(session, "bash", "rm -rf x", 30))
    for _ in range(100):
        if await built.registry.open_pending():
            break
        await asyncio.sleep(0.01)
    await built.lifecycle.transition(session.session_key, SessionStatus.ENDED, "session ended")
    outcome = await asyncio.wait_for(waiting, 2)
    assert (outcome.decision, outcome.by) == ("deny", "ended")


async def test_stop_closes_requests_and_queue_but_keeps_grants(core: tuple[Core, Recorder]) -> None:
    built, rec = core
    key = "opencode:s1"
    await _session(built)
    built.router.add_adapter(EmittingAdapter(count=0))
    await _pending(built, key, "a1")
    await built.registry.enqueue(key, "steer", "later")
    allow = ALLOW_SESSION_KEY.format(session_key=key, tool="bash")
    await built.registry.kv_set(allow, "1")

    await built.service.stop(key)
    await built.router.join()

    assert (await built.service.session(key)).status is SessionStatus.IDLE
    pending = await built.registry.get_pending("a1")
    assert pending is not None and pending.answer == {"decision": "deny", "by": "stopped"}
    assert await built.registry.pop_queued(key, ("steer",)) == []
    assert await built.registry.kv_get(allow) == "1"
    assert [e.by for e in rec.events if isinstance(e, ApprovalResolved)] == ["stopped"]


async def test_revive_moves_ended_to_idle_only(core: tuple[Core, Recorder]) -> None:
    built, _ = core
    key = "opencode:s1"
    await _session(built, status=SessionStatus.ENDED)
    assert (await built.lifecycle.revive(key, "reply")).status is SessionStatus.IDLE
    assert (await built.lifecycle.revive(key, "again")).status is SessionStatus.IDLE


async def test_prompting_an_ended_session_revives_and_delivers(core: tuple[Core, Recorder]) -> None:
    built, _ = core
    key = "opencode:s1"
    adapter = EmittingAdapter(count=0)
    built.router.add_adapter(adapter)
    await _session(built, status=SessionStatus.ENDED)
    receipt = await built.service.prompt(key, "carry on")
    assert receipt == "sent (session resumed)"
    assert adapter.prompts == [(key, "carry on")]
    assert (await built.service.session(key)).status is SessionStatus.RUNNING
    assert await built.registry.pop_queued(key, ("prompt", "steer")) == []


async def test_prompting_an_ended_session_without_a_way_to_resume_is_an_error(
    core: tuple[Core, Recorder],
) -> None:
    built, _ = core
    key = "opencode:s1"
    await _session(built, status=SessionStatus.ENDED)
    with pytest.raises(RelayError, match="cannot be resumed"):
        await built.service.prompt(key, "hello")
    assert await built.registry.pop_queued(key, ("prompt", "steer")) == []
    assert (await built.service.session(key)).status is SessionStatus.ENDED

    built.router.add_adapter(NoResumeAdapter(count=0))
    with pytest.raises(RelayError, match="cannot be resumed"):
        await built.service.prompt(key, "hello")
    assert (await built.service.session(key)).status is SessionStatus.ENDED
    assert await built.registry.pop_queued(key, ("prompt", "steer")) == []


async def test_panic_keeps_going_after_an_error_and_reports_it(core: tuple[Core, Recorder]) -> None:
    built, _ = core
    await _session(built, "opencode:bad")
    await _session(built, "opencode:good")
    await _session(built, "opencode:parked", SessionStatus.PARKED)
    await _pending(built, "opencode:good", "a1")
    await built.registry.enqueue("opencode:good", "prompt", "x")
    adapter = FlakyAdapter("opencode:bad")
    built.router.add_adapter(adapter)
    await away.set_away(built.registry, True)

    summary = await built.service.panic()

    assert adapter.cancelled == ["opencode:good"], "the failing cancel did not stop the others"
    assert "Cancelled 1 turn(s), denied 1 request(s)" in summary
    assert "1 step(s) failed: cancel opencode:bad" in summary
    good = await built.service.session("opencode:good")
    assert good.status is SessionStatus.IDLE
    assert (await built.service.session("opencode:bad")).status is SessionStatus.RUNNING
    assert await built.registry.kv_get(RELEASE_KEY.format(session_key="opencode:parked")) == "panic"
    assert await built.registry.pop_queued("opencode:good", ("prompt",)) == []
    assert not await away.is_away(built.registry, None)


def test_only_the_lifecycle_writes_a_session_status() -> None:
    allowed = {
        SRC / "core" / "lifecycle.py",
        SRC / "core" / "registry.py",
        SRC / "adapters" / "base.py",
    }
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "set_session_status" in path.read_text(encoding="utf-8") and path not in allowed
    ]
    assert offenders == []
