"""Queue modes: what a phone message does while a session is mid-turn, and how waiting messages go out
when the turn ends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from pydantic import JsonValue

from agent_relay.adapters.base import UnsupportedError
from agent_relay.core import away
from agent_relay.core.hooks import HOOKED as CORE_HOOKED
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayError, RelayService
from agent_relay.core.service.queue import HOOKED, QUEUE_MODE_KEY, SELF_DRAINING
from agent_relay.core.signals import Signals
from agent_relay.daemon import Core
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.config import QueueConfig, QueueMode, RelayConfig
from agent_relay.models.events import ErrorEvent, Event, Harness, TurnCompleted
from agent_relay.models.sessions import SessionRecord, SessionStatus
from tests.fakes.adapters import EmittingAdapter

KEY = "opencode:q1"


class NoSteerAdapter(EmittingAdapter):
    """Takes a prompt only between turns, like VS Code and Cursor's composer."""

    async def send_prompt(self, session: SessionRecord, text: str, attachments: object = ()) -> None:
        if session.status is SessionStatus.RUNNING:
            raise UnsupportedError(self.harness, "steering")
        await super().send_prompt(session, text)


class FailingAdapter(EmittingAdapter):
    async def send_prompt(self, session: SessionRecord, text: str, attachments: object = ()) -> None:
        raise RuntimeError("window closed")

    async def wake(self, session: SessionRecord, text: str) -> None:
        raise RuntimeError("window closed")


class SpyCancelAdapter(EmittingAdapter):
    def __init__(self, registry: Registry) -> None:
        super().__init__(count=0)
        self.registry = registry
        self.waiting_at_cancel: list[int] = []

    async def cancel(self, session: SessionRecord) -> None:
        self.waiting_at_cancel.append(len(await self.registry.list_queued(session.session_key)))
        await super().cancel(session)


class BrokenCancelAdapter(EmittingAdapter):
    async def cancel(self, session: SessionRecord) -> None:
        raise RuntimeError("no stop button")


class NoCancelAdapter(EmittingAdapter):
    capabilities = Capabilities(cold_start=True)


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)


@dataclass
class Rig:
    registry: Registry
    router: Router
    service: RelayService
    adapter: EmittingAdapter
    recorder: Recorder

    async def session(
        self,
        key: str = KEY,
        status: SessionStatus = SessionStatus.RUNNING,
        harness: Harness | None = None,
        extra: dict[str, JsonValue] | None = None,
    ) -> SessionRecord:
        record = await self.registry.create_session(
            SessionRecord(
                session_key=key,
                harness=harness or self.adapter.harness,
                harness_id=key.split(":")[1],
                workspace="/w/proj",
                status=status,
                extra=extra or {},
            )
        )
        return record

    async def end_turn(self, key: str = KEY, harness: Harness | None = None) -> None:
        await self.service.lifecycle.observe(key, SessionStatus.IDLE, "turn over")
        await self.router.publish(TurnCompleted(harness=harness or self.adapter.harness, session_key=key))
        await self.router.join()


async def build(registry: Registry, adapter: EmittingAdapter, config: RelayConfig | None = None) -> Rig:
    router = Router()
    router.add_adapter(adapter)
    service = RelayService(registry, router, Signals(), config or RelayConfig())
    recorder = Recorder()
    router.subscribe(recorder)
    return Rig(registry, router, service, adapter, recorder)


@pytest.fixture
async def rig(registry: Registry) -> AsyncIterator[Rig]:
    built = await build(registry, EmittingAdapter(count=0))
    yield built
    await built.router.stop()


def test_the_hooked_flag_matches_the_one_hooks_sets() -> None:
    assert HOOKED == CORE_HOOKED


async def test_the_mode_defaults_to_the_config_and_is_kept_per_session(rig: Rig) -> None:
    await rig.session()
    await rig.session("opencode:q2")
    assert await rig.service.queue_mode(KEY) == "queue"
    assert await rig.service.set_queue_mode(KEY, "STEER") == "steer"
    assert (await rig.service.session(KEY)).extra[QUEUE_MODE_KEY] == "steer"
    assert await rig.service.queue_mode("opencode:q2") == "queue"


async def test_the_config_default_applies_until_a_session_chooses(registry: Registry) -> None:
    config = RelayConfig().model_copy(update={"queue": QueueConfig(default_mode="interrupt")})
    built = await build(registry, EmittingAdapter(count=0), config)
    await built.session()
    assert await built.service.queue_mode(KEY) == "interrupt"
    await built.service.set_queue_mode(KEY, "queue")
    assert await built.service.queue_mode(KEY) == "queue"
    await built.router.stop()


async def test_an_unknown_mode_or_session_is_refused(rig: Rig) -> None:
    await rig.session()
    with pytest.raises(RelayError, match="Unknown queue mode `later`"):
        await rig.service.set_queue_mode(KEY, "later")
    with pytest.raises(RelayError, match="no longer known"):
        await rig.service.set_queue_mode("opencode:nope", "steer")


async def test_queue_mode_holds_a_message_until_the_turn_ends(rig: Rig) -> None:
    await rig.session()
    assert await rig.service.prompt(KEY, "one") == "Queued (1 waiting)"
    assert await rig.service.prompt(KEY, "two") == "Queued (2 waiting)"
    assert rig.adapter.prompts == []
    await rig.end_turn()
    assert rig.adapter.prompts == [(KEY, "one\n\ntwo")]
    assert await rig.service.queued(KEY) == []
    assert (await rig.service.session(KEY)).status is SessionStatus.RUNNING


async def test_a_turn_end_with_nothing_waiting_sends_nothing(rig: Rig) -> None:
    await rig.session()
    await rig.end_turn()
    assert rig.adapter.prompts == []
    assert (await rig.service.session(KEY)).status is SessionStatus.IDLE


async def test_steer_mode_goes_straight_in_where_the_app_can_take_it(rig: Rig) -> None:
    await rig.session()
    await rig.service.set_queue_mode(KEY, "steer")
    assert await rig.service.prompt(KEY, "use pytest") == "sent"
    assert rig.adapter.prompts == [(KEY, "use pytest")]
    assert await rig.service.queued(KEY) == []


async def test_steer_once_overrides_the_mode(rig: Rig) -> None:
    await rig.session()
    assert await rig.service.steer(KEY, "use pytest") == "sent"
    assert rig.adapter.prompts == [(KEY, "use pytest")]
    assert await rig.service.queue_mode(KEY) == "queue"


async def test_steer_in_an_app_that_cannot_says_so_and_queues(registry: Registry) -> None:
    built = await build(registry, NoSteerAdapter(count=0))
    await built.session()
    await built.service.set_queue_mode(KEY, "steer")
    receipt = await built.service.prompt(KEY, "use pytest")
    assert receipt == (
        "Queued (1 waiting). OpenCode cannot be steered mid-turn, so it goes in when this turn ends"
    )
    assert built.adapter.prompts == []
    assert [q.kind for q in await built.service.queued(KEY)] == ["prompt"]
    await built.end_turn()
    assert built.adapter.prompts == [(KEY, "use pytest")]
    await built.router.stop()


async def test_steer_in_a_hooked_session_rides_the_next_tool_call(registry: Registry) -> None:
    built = await build(registry, NoSteerAdapter(count=0))
    await built.session(extra={HOOKED: True})
    await built.service.set_queue_mode(KEY, "steer")
    assert await built.service.prompt(KEY, "use pytest") == "queued as steering for the next tool call"
    assert [q.kind for q in await built.service.queued(KEY)] == ["steer"]
    await built.router.stop()


async def test_interrupt_queues_first_then_cancels_and_the_message_follows_the_turn_end(
    registry: Registry,
) -> None:
    spy = SpyCancelAdapter(registry)
    built = await build(registry, spy)
    await built.session()
    await built.service.set_queue_mode(KEY, "interrupt")
    receipt = await built.service.prompt(KEY, "stop and do this")
    assert receipt == "Interrupting; your message goes in as soon as the turn stops."
    assert spy.cancelled == [KEY] and spy.waiting_at_cancel == [1]
    assert spy.prompts == []
    await built.end_turn()
    assert spy.prompts == [(KEY, "stop and do this")]
    await built.router.stop()


async def test_interrupt_in_an_app_that_cannot_be_cancelled_queues_and_says_so(registry: Registry) -> None:
    built = await build(registry, NoCancelAdapter(count=0))
    await built.session()
    await built.service.set_queue_mode(KEY, "interrupt")
    receipt = await built.service.prompt(KEY, "now")
    assert receipt == (
        "Queued (1 waiting). OpenCode cannot be interrupted from the phone, so it waits for this turn to end"
    )
    assert built.adapter.cancelled == []
    warning = await built.service.queue_mode_warning(KEY, "interrupt")
    assert warning is not None and "cannot be interrupted" in warning
    assert await built.service.queue_mode_warning(KEY, "steer") is None
    await built.router.stop()


async def test_interrupt_that_fails_leaves_the_message_queued(registry: Registry) -> None:
    built = await build(registry, BrokenCancelAdapter(count=0))
    await built.session()
    await built.service.set_queue_mode(KEY, "interrupt")
    receipt = await built.service.prompt(KEY, "now")
    assert receipt.startswith("Queued (1 waiting). Could not interrupt (no stop button)")
    assert [q.text for q in await built.service.queued(KEY)] == ["now"]
    await built.router.stop()


async def test_an_idle_session_still_takes_the_message_at_once(rig: Rig) -> None:
    await rig.session(status=SessionStatus.IDLE)
    assert await rig.service.prompt(KEY, "go") == "sent"
    assert rig.adapter.prompts == [(KEY, "go")]


@pytest.mark.parametrize("harness", [Harness.OPENCODE, Harness.VSCODE_COPILOT, Harness.VSCODE])
async def test_every_adapter_driven_app_sends_the_queue_when_its_turn_ends(
    registry: Registry, harness: Harness
) -> None:
    adapter = EmittingAdapter(count=0)
    adapter.harness = harness
    built = await build(registry, adapter)
    key = f"{harness.value}:q"
    await built.session(key, harness=harness)
    assert await built.service.prompt(key, "after") == "Queued (1 waiting)"
    await built.end_turn(key, harness)
    assert adapter.prompts == [(key, "after")]
    await built.router.stop()


async def test_a_hooked_session_is_left_to_its_own_stop_hook(registry: Registry) -> None:
    built = await build(registry, EmittingAdapter(count=0))
    await built.session(extra={HOOKED: True})
    await built.service.prompt(KEY, "after")
    await built.end_turn()
    assert built.adapter.prompts == []
    assert [q.text for q in await built.service.queued(KEY)] == ["after"]
    await built.router.stop()


@pytest.mark.parametrize("harness", sorted(SELF_DRAINING))
async def test_adapters_that_start_their_own_next_turn_are_not_drained_twice(
    registry: Registry, harness: Harness
) -> None:
    adapter = EmittingAdapter(count=0)
    adapter.harness = harness
    built = await build(registry, adapter)
    key = f"{harness.value}:q"
    await built.session(key, harness=harness)
    await built.service.prompt(key, "after")
    await built.end_turn(key, harness)
    assert adapter.prompts == []
    assert [q.text for q in await built.service.queued(key)] == ["after"]
    await built.router.stop()


async def test_a_failed_send_keeps_the_message_and_tells_the_user(registry: Registry) -> None:
    built = await build(registry, FailingAdapter(count=0))
    await built.session()
    await built.service.prompt(KEY, "after")
    await built.end_turn()
    assert [q.text for q in await built.service.queued(KEY)] == ["after"]
    errors = [e for e in built.recorder.events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1 and "still queued" in errors[0].message and "window closed" in errors[0].message
    await built.router.stop()


@pytest.mark.parametrize("status", [SessionStatus.PARKED, SessionStatus.ENDED])
async def test_parked_and_ended_sessions_are_not_drained(rig: Rig, status: SessionStatus) -> None:
    await rig.session()
    await rig.registry.enqueue(KEY, "prompt", "after")
    await rig.registry.set_session_status(KEY, status)
    await rig.router.publish(TurnCompleted(harness=rig.adapter.harness, session_key=KEY))
    await rig.router.join()
    assert rig.adapter.prompts == []


async def test_a_parked_session_still_gets_the_message_through_the_park(rig: Rig) -> None:
    await rig.session(status=SessionStatus.PARKED)
    assert await rig.service.prompt(KEY, "carry on") == "delivered to the parked session"
    assert [q.text for q in await rig.service.queued(KEY)] == ["carry on"]


async def test_queued_messages_can_be_listed_removed_and_cleared(rig: Rig) -> None:
    await rig.session()
    for text in ("a", "b", "c"):
        await rig.service.prompt(KEY, text)
    items = await rig.service.queued(KEY)
    assert [q.text for q in items] == ["a", "b", "c"]
    assert await rig.service.remove_queued(KEY, items[1].id) is True
    assert await rig.service.remove_queued(KEY, items[1].id) is False
    assert [q.text for q in await rig.service.queued(KEY)] == ["a", "c"]
    assert await rig.service.clear_queued(KEY) == 2
    assert await rig.service.clear_queued(KEY) == 0
    assert await rig.service.queued(KEY) == []


async def test_a_message_cannot_be_removed_through_another_session(rig: Rig) -> None:
    await rig.session()
    await rig.session("opencode:q2")
    await rig.service.prompt(KEY, "mine")
    (item,) = await rig.service.queued(KEY)
    assert await rig.service.remove_queued("opencode:q2", item.id) is False
    assert [q.text for q in await rig.service.queued(KEY)] == ["mine"]


async def test_removed_messages_are_not_sent(rig: Rig) -> None:
    await rig.session()
    await rig.service.prompt(KEY, "drop me")
    await rig.service.prompt(KEY, "keep me")
    first, _ = await rig.service.queued(KEY)
    await rig.service.remove_queued(KEY, first.id)
    await rig.end_turn()
    assert rig.adapter.prompts == [(KEY, "keep me")]


# hook-driven apps: the stop hook is their turn end


async def _hook(core: Core, family: str, event: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    reply = await core.handle_ipc({"op": "hook", "harness": family, "event": event, "payload": payload})
    await core.router.join()
    return reply  # type: ignore[return-value]


@pytest.mark.parametrize("mode", ["queue", "steer", "interrupt"])
async def test_the_stop_hook_hands_the_queued_message_to_a_hook_driven_app(
    registry: Registry, mode: QueueMode
) -> None:
    config = RelayConfig()
    config = config.model_copy(update={"park": config.park.model_copy(update={"deadline_s": 1.0})})
    built = Core(config, registry)
    try:
        await away.set_away(registry, True)
        pre: dict[str, JsonValue] = {
            "session_id": "L1",
            "cwd": "/w/proj",
            "tool_name": "read_file",
            "tool_input": {"path": "a"},
            "tool_use_id": "t1",
        }
        await _hook(built, "copilot", "PreToolUse", pre)
        await built.service.set_queue_mode("vscode:L1", mode)
        receipt = await built.service.prompt("vscode:L1", "now add tests")
        assert receipt != "sent"
        reply = await asyncio.wait_for(
            _hook(
                built, "copilot", "Stop", {"session_id": "L1", "cwd": "/w/proj", "stop_hook_active": False}
            ),
            3,
        )
        stdout = reply["stdout"]
        assert isinstance(stdout, dict) and stdout["decision"] == "block"
        assert "now add tests" in str(stdout["reason"])
        assert await built.service.queued("vscode:L1") == []
    finally:
        await built.router.stop()
