"""Away mode is one global manual flag (U5): semantics, the desk-session gate, panic and back."""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.registry import GLOBAL_SCOPE, Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayService
from agent_relay.core.signals import Signals
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import (
    Event,
    Harness,
    Origin,
    QuestionAsked,
    SessionStarted,
    TextDelta,
    utcnow,
)
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus


class Collector:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def keys(self) -> list[str]:
        return [e.session_key for e in self.events]


class BookKeeper(Collector):
    """A sink that must see every event, like the question broker."""

    handles_desk_events = True


async def make_session(
    registry: Registry,
    key: str,
    origin: Origin,
    harness: Harness = Harness.VSCODE,
    status: SessionStatus = SessionStatus.RUNNING,
    **fields: object,
) -> SessionRecord:
    record = SessionRecord(
        session_key=key,
        harness=harness,
        harness_id=key.split(":", 1)[-1],
        workspace="/w/proj",
        origin=origin,
        status=status,
    ).model_copy(update=fields)
    await registry.create_session(record)
    await registry.set_session_status(key, status)
    return record


@pytest.fixture
async def service(registry: Registry) -> RelayService:
    return RelayService(registry, Router(), Signals(), RelayConfig())


# the flag


async def test_the_flag_defaults_to_off(registry: Registry) -> None:
    assert await away.is_away(registry) is False
    assert await away.is_away(registry, "/w/proj") is False


async def test_set_away_without_a_workspace_is_global(registry: Registry) -> None:
    await away.set_away(registry, True)
    assert await away.is_away(registry) is True
    assert await away.is_away(registry, "/anywhere") is True
    assert await registry.away_scopes() == {GLOBAL_SCOPE: True}


async def test_a_legacy_workspace_argument_switches_the_global_flag(registry: Registry) -> None:
    await away.set_away(registry, True, "/w/one")
    assert await away.is_away(registry) is True
    assert await away.is_away(registry, "/w/two") is True
    assert list((await registry.away_scopes()).keys()) == [GLOBAL_SCOPE]
    await away.set_away(registry, False, "/w/two")
    assert await away.is_away(registry, "/w/one") is False


async def test_a_pause_counts_as_not_away(registry: Registry) -> None:
    await away.set_away(registry, True)
    paths.pause_flag().parent.mkdir(parents=True, exist_ok=True)
    paths.pause_flag().write_text("now", encoding="utf-8")
    assert await away.is_away(registry) is False


def test_nothing_turns_away_on_by_itself() -> None:
    assert not (Path(away.__file__).parent / "presence.py").exists()
    assert importlib.util.find_spec("agent_relay.core.presence") is None


async def test_the_service_switch_is_global_and_ignores_the_workspace(
    service: RelayService, registry: Registry
) -> None:
    assert await service.set_away_for(True, "/w/one") == "Away mode on."
    assert await away.is_away(registry, "/w/other") is True
    assert await service.set_away_for(False) == "Away mode off."
    assert await away.is_away(registry) is False
    assert await service.set_away(True, None) == "Away mode on."
    assert await away.is_away(registry) is True


async def test_set_away_for_on_resumes_after_a_panic(service: RelayService, registry: Registry) -> None:
    await service.panic()
    assert paths.pause_flag().exists()
    await service.set_away_for(True)
    assert not paths.pause_flag().exists()
    assert await away.is_away(registry) is True


# the router gate


async def deliver(router: Router, *events: Event) -> None:
    for event in events:
        await router.publish(event)
    await router.join()


def text(key: str, harness: Harness = Harness.VSCODE) -> TextDelta:
    return TextDelta(harness=harness, session_key=key, text="x")


async def test_desk_events_are_dropped_while_not_away(service: RelayService, registry: Registry) -> None:
    await make_session(registry, "vscode:D1", Origin.DESK)
    sink = Collector()
    service.router.subscribe(sink)
    await deliver(service.router, text("vscode:D1"))
    assert sink.events == []
    await service.router.stop()


async def test_desk_events_pass_while_away(service: RelayService, registry: Registry) -> None:
    await make_session(registry, "vscode:D1", Origin.DESK)
    sink = Collector()
    service.router.subscribe(sink)
    await away.set_away(registry, True)
    await deliver(service.router, text("vscode:D1"))
    assert sink.keys() == ["vscode:D1"]
    await service.router.stop()


async def test_an_adopted_desk_chat_stops_streaming_the_moment_away_goes_off(
    service: RelayService, registry: Registry
) -> None:
    await make_session(registry, "vscode_copilot:A1", Origin.DESK, Harness.VSCODE_COPILOT)
    sink = Collector()
    service.router.subscribe(sink)
    await service.set_away_for(True)
    await deliver(service.router, text("vscode_copilot:A1", Harness.VSCODE_COPILOT))
    await service.set_away_for(False)
    await deliver(service.router, text("vscode_copilot:A1", Harness.VSCODE_COPILOT))
    assert len(sink.events) == 1
    await service.router.stop()


async def test_phone_held_sessions_always_pass(service: RelayService, registry: Registry) -> None:
    await make_session(registry, "opencode:P1", Origin.PHONE, Harness.OPENCODE)
    sink = Collector()
    service.router.subscribe(sink)
    await deliver(service.router, text("opencode:P1", Harness.OPENCODE))
    assert sink.keys() == ["opencode:P1"]
    await service.router.stop()


async def test_a_phone_session_handed_to_the_desk_follows_away_mode(
    service: RelayService, registry: Registry
) -> None:
    await make_session(registry, "vscode:H1", Origin.PHONE, extra={away.AT_DESK: True})
    sink = Collector()
    service.router.subscribe(sink)
    await deliver(service.router, text("vscode:H1"))
    assert sink.events == []
    await service.router.stop()


async def test_adapter_pseudo_sessions_and_unknown_sessions_pass(service: RelayService) -> None:
    sink = Collector()
    service.router.subscribe(sink)
    await deliver(service.router, text("adapter:opencode", Harness.OPENCODE), text("vscode:unknown"))
    assert sink.keys() == ["adapter:opencode", "vscode:unknown"]
    await service.router.stop()


async def test_a_bookkeeping_sink_still_sees_desk_events_at_the_desk(
    service: RelayService, registry: Registry
) -> None:
    await make_session(registry, "vscode:D1", Origin.DESK)
    shown, kept, forced = Collector(), BookKeeper(), Collector()
    service.router.subscribe(shown)
    service.router.subscribe(kept)
    service.router.subscribe(forced, desk=True)
    await deliver(service.router, text("vscode:D1"))
    assert shown.events == []
    assert kept.keys() == ["vscode:D1"] and forced.keys() == ["vscode:D1"]
    await service.router.stop()


async def test_gates_still_see_desk_events_at_the_desk(service: RelayService, registry: Registry) -> None:
    await make_session(registry, "vscode:D1", Origin.DESK)
    seen: list[str] = []

    class Gate:
        async def intercept(self, event: Event) -> bool:
            seen.append(event.session_key)
            return False

    service.router.add_gate(Gate())
    service.router.subscribe(Collector())
    await deliver(service.router, text("vscode:D1"))
    assert seen == ["vscode:D1"]
    await service.router.stop()


async def test_registration_and_questions_of_a_desk_session_at_the_desk_stay_off_the_phone(
    service: RelayService, registry: Registry
) -> None:
    await make_session(registry, "cursor:C1", Origin.DESK, Harness.CURSOR)
    sink = Collector()
    service.router.subscribe(sink)
    ask = QuestionAsked(harness=Harness.CURSOR, session_key="cursor:C1", request_id="q1", question="Which?")
    await deliver(
        service.router, SessionStarted(harness=Harness.CURSOR, session_key="cursor:C1", title="t"), ask
    )
    assert sink.events == []
    await service.router.stop()


async def test_a_failing_filter_lets_the_event_through() -> None:
    router = Router()

    async def broken(event: Event) -> bool:
        raise RuntimeError("boom")

    router.set_desk_filter(broken)
    sink = Collector()
    router.subscribe(sink)
    await deliver(router, text("vscode:D1"))
    assert sink.keys() == ["vscode:D1"]
    await router.stop()


# panic and back


async def question(registry: Registry, request_id: str, session_key: str) -> None:
    await registry.add_pending(
        PendingRequest(
            request_id=request_id,
            kind="question",
            session_key=session_key,
            payload={"prompt": "Which?"},
            deadline=utcnow() + timedelta(minutes=5),
        )
    )


async def test_panic_pauses_forces_away_off_and_closes_questions(
    service: RelayService, registry: Registry
) -> None:
    await make_session(registry, "vscode:D1", Origin.DESK)
    await away.set_away(registry, True)
    await question(registry, "q1", "vscode:D1")
    reply = await service.panic()
    assert reply.startswith("🛑 Paused.") and "closed 1 question(s)" in reply
    assert paths.pause_flag().exists()
    assert (await registry.away_scopes()).get(GLOBAL_SCOPE) is False
    assert await registry.open_pending() == []
    resolved = await registry.get_pending("q1")
    assert resolved is not None and resolved.answer == {"answer": None, "by": "panic"}


async def test_panic_leaves_a_legacy_approval_row_alone(service: RelayService, registry: Registry) -> None:
    await make_session(registry, "vscode:D1", Origin.DESK)
    await registry.add_pending(
        PendingRequest(
            request_id="a1",
            kind="approval",
            session_key="vscode:D1",
            payload={},
            deadline=utcnow() + timedelta(minutes=5),
        )
    )
    assert "closed 0 question(s)" in await service.panic()


async def test_away_off_releases_every_parked_session(service: RelayService, registry: Registry) -> None:
    await make_session(registry, "cursor:K1", Origin.DESK, Harness.CURSOR, SessionStatus.PARKED)
    await make_session(registry, "cursor:K2", Origin.DESK, Harness.CURSOR, SessionStatus.PARKED)
    await make_session(registry, "cursor:K3", Origin.DESK, Harness.CURSOR, SessionStatus.IDLE)
    await service.set_away_for(True)
    await service.set_away_for(False, "/w/ignored")
    assert await registry.kv_get("release:cursor:K1") == "away mode off"
    assert await registry.kv_get("release:cursor:K2") == "away mode off"
    assert await registry.kv_get("release:cursor:K3") is None


async def test_back_releases_parked_sessions(service: RelayService, registry: Registry) -> None:
    await make_session(registry, "cursor:K1", Origin.DESK, Harness.CURSOR, SessionStatus.PARKED)
    assert await service.back() == "Released 1 parked session(s)."
    assert await registry.kv_get("release:cursor:K1") == "/back"


async def test_away_off_hands_desk_visible_phone_chats_to_the_desk(
    service: RelayService, registry: Registry
) -> None:
    visible = await make_session(registry, "vscode:L1", Origin.PHONE)
    headless = await make_session(registry, "opencode:H1", Origin.PHONE, Harness.OPENCODE)
    reply = await service.set_away_for(False)
    assert reply == "Away mode off. Handed 1 phone session(s) to the desk."
    after = await registry.get_session(visible.session_key)
    assert after is not None and after.extra[away.AT_DESK] is True and not away.phone_held(after)
    other = await registry.get_session(headless.session_key)
    assert other is not None and away.phone_held(other)
    assert "Handed" not in await service.set_away_for(False)
