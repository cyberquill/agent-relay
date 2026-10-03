"""AC-3 (state survives restart) and AC-4 (concurrent writes are safe)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from agent_relay import paths
from agent_relay.core.registry import GLOBAL_SCOPE, Registry
from agent_relay.models.events import Harness, Origin, utcnow
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus


def _session(key: str = "s1", harness: Harness = Harness.OPENCODE) -> SessionRecord:
    return SessionRecord(
        session_key=key, harness=harness, harness_id=f"h-{key}", workspace="/w", origin=Origin.PHONE
    )


async def test_state_survives_restart() -> None:
    reg = Registry(paths.db_path())
    await reg.open()
    await reg.upsert_session(_session())
    await reg.set_session_fields(
        "s1", webex_thread_id="thread-1", model="gpt", status=SessionStatus.PARKED.value
    )
    await reg.set_session_extra("s1", "chat", "ahp-chat://x")
    deadline = utcnow() + timedelta(minutes=5)
    await reg.add_pending(
        PendingRequest(
            request_id="r1", kind="approval", session_key="s1", payload={"tool": "bash"}, deadline=deadline
        )
    )
    await reg.enqueue("s1", "prompt", "hello")
    await reg.set_away(True)
    await reg.kv_set("webex.last_seen", "msg-9")
    await reg.close()

    reopened = Registry(paths.db_path())
    await reopened.open()
    got = await reopened.get_session("s1")
    assert got is not None
    assert (got.webex_thread_id, got.model, got.status) == ("thread-1", "gpt", SessionStatus.PARKED)
    assert got.extra == {"chat": "ahp-chat://x"}
    assert (await reopened.session_by_thread("thread-1")) == got
    assert (await reopened.find_session(Harness.OPENCODE, "h-s1")) == got
    pending = await reopened.open_pending("approval")
    assert [p.request_id for p in pending] == ["r1"]
    assert pending[0].deadline == deadline
    assert [m.text for m in await reopened.pop_queued("s1", ("prompt",))] == ["hello"]
    assert (await reopened.away_scopes()) == {GLOBAL_SCOPE: True}
    assert (await reopened.kv_get("webex.last_seen")) == "msg-9"
    await reopened.close()


async def test_first_answer_wins(registry: Registry) -> None:
    await registry.add_pending(
        PendingRequest(request_id="r", kind="question", session_key="s", payload={}, deadline=utcnow())
    )
    assert await registry.resolve_pending("r", {"answer": "a"})
    assert not await registry.resolve_pending("r", {"answer": "b"})
    got = await registry.get_pending("r")
    assert got is not None and got.answer == {"answer": "a"}


async def test_concurrent_writers_from_two_connections() -> None:
    """Two registries (as two processes would) hammer the same file; nothing is lost."""
    a = Registry(paths.db_path())
    b = Registry(paths.db_path())
    await a.open()
    await b.open()
    await a.upsert_session(_session("s"))

    async def writer(reg: Registry, tag: str, n: int) -> None:
        for i in range(n):
            await reg.enqueue("s", "steer", f"{tag}{i}")

    await asyncio.gather(*(writer(a if i % 2 else b, f"w{i}-", 25) for i in range(8)))
    items = await a.pop_queued("s", ("steer",))
    assert len(items) == 200
    assert len({m.text for m in items}) == 200
    assert await b.pop_queued("s", ("steer",)) == []
    await a.close()
    await b.close()


@settings(max_examples=25, deadline=None)
@given(
    st.lists(st.tuples(st.sampled_from(["s1", "s2", "s3"]), st.text(min_size=1, max_size=20)), max_size=30)
)
def test_queue_preserves_order_per_session(ops: list[tuple[str, str]]) -> None:
    async def scenario() -> None:
        reg = Registry(paths.db_path())
        await reg.open()
        for key in ("s1", "s2", "s3"):
            await reg.drop_queued(key)
        await asyncio.gather(*(reg.enqueue(k, "prompt", t) for k, t in ops))
        for key in ("s1", "s2", "s3"):
            got = [m.text for m in await reg.pop_queued(key, ("prompt",))]
            assert sorted(got) == sorted(t for k, t in ops if k == key)
        await reg.close()

    asyncio.run(scenario())


async def test_set_session_fields_rejects_unknown(registry: Registry) -> None:
    await registry.upsert_session(_session())
    try:
        await registry.set_session_fields("s1", bogus="x")
    except ValueError as exc:
        assert "bogus" in str(exc)
    else:
        raise AssertionError("expected ValueError")
