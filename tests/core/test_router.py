"""AC-5 (adapter isolation) and per-session ordering; AC-6 capability model."""

from __future__ import annotations

import asyncio

import pytest

from agent_relay.adapters.base import UnsupportedError
from agent_relay.core.router import Router
from agent_relay.models.capabilities import COMMAND_REQUIRES, Capabilities, Capability
from agent_relay.models.events import Event, Harness, TextDelta
from tests.fakes.adapters import CrashingAdapter, EmittingAdapter


class Collector:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)


class ExplodingSink:
    async def handle(self, event: Event) -> None:
        raise ValueError("sink failure")


async def test_crashing_adapter_does_not_stop_others() -> None:
    router = Router(restart_backoff=(0.01, 0.02))
    sink = Collector()
    router.subscribe(ExplodingSink())
    router.subscribe(sink)
    crashing = CrashingAdapter()
    router.add_adapter(crashing)
    router.add_adapter(EmittingAdapter(count=5))
    await asyncio.sleep(0.2)
    await router.join()
    texts = [e.text for e in sink.events if isinstance(e, TextDelta)]
    assert texts == [f"t{i}" for i in range(5)]
    assert crashing.runs >= 2
    assert router.adapter_failures[Harness.CURSOR_CDP] >= 2
    assert any(e.type == "error" for e in sink.events)
    await router.stop()


async def test_per_session_order_with_interleaving() -> None:
    router = Router()
    sink = Collector()
    router.subscribe(sink)
    for i in range(50):
        await router.publish(TextDelta(harness=Harness.OPENCODE, session_key=f"s{i % 3}", text=str(i)))
    await router.join()
    for key in ("s0", "s1", "s2"):
        got = [int(e.text) for e in sink.events if isinstance(e, TextDelta) and e.session_key == key]
        assert got == sorted(got)
    await router.stop()


async def test_unsupported_operations_raise() -> None:
    adapter = CrashingAdapter()
    with pytest.raises(UnsupportedError):
        await adapter.list_models()


def test_capabilities() -> None:
    caps = Capabilities(cold_start=True, cancel=True)
    assert caps.supported() == (Capability.COLD_START, Capability.CANCEL)
    assert caps.has(Capability.CANCEL)
    assert not caps.has(COMMAND_REQUIRES["/model"])
