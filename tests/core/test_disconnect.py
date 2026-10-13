"""A hook that goes away while it waits must not leave its approval answerable or its park waiting."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from agent_relay import ipc, paths
from agent_relay.core import away
from agent_relay.core.keys import session_signal
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import ApprovalResolved, Event, Harness, Origin, QuestionAnswered
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
async def served(registry: Registry) -> AsyncIterator[tuple[Core, Recorder]]:
    core = Core(RelayConfig(), registry)
    recorder = Recorder()
    core.router.subscribe(recorder)
    server = ipc.IpcServer(paths.socket_path(), core.handle_ipc)
    await server.start()
    yield core, recorder
    await server.stop()
    await core.router.stop()


async def _until[T](probe: Callable[[], Awaitable[T | None]], what: str) -> T:
    for _ in range(300):
        found = await probe()
        if found:
            return found
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def test_a_dead_client_cancels_its_handler() -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(frame: ipc.JsonObj) -> ipc.JsonObj:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    server = ipc.IpcServer(paths.socket_path(), slow)
    await server.start()
    reader, writer = await asyncio.open_unix_connection(str(paths.socket_path()))
    await ipc.write_frame(writer, {"op": "wait"})
    await asyncio.wait_for(started.wait(), 2)
    writer.close()
    await asyncio.wait_for(cancelled.wait(), 2)
    assert await ipc.read_frame(reader) is None
    await server.stop()


async def test_a_slow_handler_still_answers_a_client_that_stays() -> None:
    async def slow(frame: ipc.JsonObj) -> ipc.JsonObj:
        await asyncio.sleep(0.3)
        return {"done": True}

    server = ipc.IpcServer(paths.socket_path(), slow)
    await server.start()
    assert await ipc.async_request({"op": "x"}, paths.socket_path(), timeout=3) == {"done": True}
    await server.stop()


@pytest.mark.parametrize(
    "partial",
    [
        b"\x00\x00",  # half a header
        struct.pack(">I", 50) + b'{"op": "pi',  # a body cut short
        struct.pack(">I", ipc.MAX_FRAME + 1),  # a length no honest client sends
        struct.pack(">I", 3) + b"[1]",  # a complete frame that is not an object
    ],
)
async def test_partial_and_bad_frames_do_not_break_the_server(partial: bytes) -> None:
    async def echo(frame: ipc.JsonObj) -> ipc.JsonObj:
        return {"echo": frame}

    server = ipc.IpcServer(paths.socket_path(), echo)
    await server.start()
    reader, writer = await asyncio.open_unix_connection(str(paths.socket_path()))
    writer.write(partial)
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    assert await ipc.async_request({"op": "ping"}, paths.socket_path(), timeout=2) == {"echo": {"op": "ping"}}
    await server.stop()


async def test_a_hook_that_dies_mid_approval_closes_the_request(served: tuple[Core, Recorder]) -> None:
    core, rec = served
    await away.set_away(core.registry, True)
    reader, writer = await asyncio.open_unix_connection(str(paths.socket_path()))
    await ipc.write_frame(
        writer,
        {
            "op": "hook",
            "harness": "copilot",
            "event": "PreToolUse",
            "payload": {
                "session_id": "L1",
                "cwd": "/w/proj",
                "tool_name": "run_in_terminal",
                "tool_input": {"command": "rm -rf build"},
                "tool_use_id": "t1",
            },
        },
    )

    async def open_request() -> PendingRequest | None:
        pending = await core.registry.open_pending()
        return pending[0] if pending else None

    request = await _until(open_request, "the approval to open")
    writer.close()

    async def closed() -> PendingRequest | None:
        got = await core.registry.get_pending(request.request_id)
        return got if got is not None and got.status != "open" else None

    done = await _until(closed, "the approval to close")
    assert done.status == "expired"
    assert done.answer == {"decision": "deny", "by": "abandoned"}
    await core.router.join()
    assert [e.by for e in rec.events if isinstance(e, ApprovalResolved)] == ["abandoned"]
    assert not await core.service.answer_approval(request.request_id, "allow"), "the card is dead"
    assert await reader.read() == b""


async def test_an_abandoned_question_is_closed_as_skipped(served: tuple[Core, Recorder]) -> None:
    core, rec = served
    session = await core.registry.create_session(
        SessionRecord(
            session_key="opencode:s1",
            harness=Harness.OPENCODE,
            harness_id="s1",
            workspace="/w",
            origin=Origin.PHONE,
        )
    )
    asking = asyncio.create_task(core.broker.ask(session, "which?", ("a", "b"), 30))

    async def open_request() -> PendingRequest | None:
        pending = await core.registry.open_pending("question")
        return pending[0] if pending else None

    request = await _until(open_request, "the question to open")
    asking.cancel()
    await asyncio.gather(asking, return_exceptions=True)
    got = await core.registry.get_pending(request.request_id)
    assert got is not None and got.status == "expired" and got.answer == {"answer": None, "by": "abandoned"}
    await core.router.join()
    assert [e.by for e in rec.events if isinstance(e, QuestionAnswered)] == ["abandoned"]


async def test_a_parked_hook_that_dies_leaves_the_phone_message_queued(registry: Registry) -> None:
    config = RelayConfig()
    config = config.model_copy(
        update={"park": config.park.model_copy(update={"deadline_s": 30.0, "slice_s": 20.0})}
    )
    core = Core(config, registry)
    assert core.parking is not None
    session = await registry.create_session(
        SessionRecord(
            session_key="copilot_cli:P1",
            harness=Harness.COPILOT_CLI,
            harness_id="P1",
            workspace="/w",
            origin=Origin.DESK,
            status=SessionStatus.RUNNING,
        )
    )
    await away.set_away(registry, True)
    parking = asyncio.create_task(core.parking.park(session, stop_hook_active=False))

    async def parked() -> bool | None:
        got = await registry.get_session(session.session_key)
        return got is not None and got.status is SessionStatus.PARKED

    await _until(parked, "the session to park")
    for _ in range(100):
        if core.signals.waiting(session_signal(session.session_key)):
            break
        await asyncio.sleep(0.01)

    parking.cancel()
    await asyncio.gather(parking, return_exceptions=True)

    got = await registry.get_session(session.session_key)
    assert got is not None and got.status is SessionStatus.IDLE
    assert core.signals.waiting(session_signal(session.session_key)) == 0
    await registry.enqueue(session.session_key, "prompt", "from the phone")
    await asyncio.sleep(0.05)
    queued = await registry.pop_queued(session.session_key, ("prompt",))
    assert [q.text for q in queued] == ["from the phone"], "no dead waiter consumed it"
