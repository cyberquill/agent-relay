"""AC-3 (state survives restart) and AC-4 (concurrent writes are safe)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_relay import paths
from agent_relay.core.registry import GLOBAL_SCOPE, MIGRATIONS, SEEN_PREFIX, Registry
from agent_relay.models.events import Harness, Origin, utcnow
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus


def _session(key: str = "s1", harness: Harness = Harness.OPENCODE) -> SessionRecord:
    return SessionRecord(
        session_key=key, harness=harness, harness_id=f"h-{key}", workspace="/w", origin=Origin.PHONE
    )


async def test_state_survives_restart() -> None:
    reg = Registry(paths.db_path())
    await reg.open()
    await reg.create_session(_session())
    await reg.update_session_fields("s1", webex_thread_id="thread-1", model="gpt")
    await reg.set_session_status("s1", SessionStatus.PARKED)
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
    await a.create_session(_session("s"))

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


async def test_update_session_fields_rejects_unknown(registry: Registry) -> None:
    await registry.create_session(_session())
    try:
        await registry.update_session_fields("s1", bogus="x")
    except ValueError as exc:
        assert "bogus" in str(exc)
    else:
        raise AssertionError("expected ValueError")


async def test_create_session_adopts_the_row_that_is_already_there(registry: Registry) -> None:
    first = await registry.create_session(_session("s1"))
    await registry.set_session_status("s1", SessionStatus.PARKED)
    await registry.update_session_fields("s1", title="kept")
    # same key, and a different key for the same (harness, harness_id): both adopt, neither raises
    same_key = await registry.create_session(_session("s1").model_copy(update={"title": "new"}))
    same_native_id = await registry.create_session(
        _session("s2").model_copy(update={"harness_id": first.harness_id, "title": "other"})
    )
    for adopted in (same_key, same_native_id):
        assert adopted.session_key == "s1"
        assert (adopted.status, adopted.title) == (SessionStatus.PARKED, "kept")
    assert await registry.get_session("s2") is None
    assert len(await registry.list_sessions()) == 1


async def test_partial_updates_leave_status_and_extra_alone(registry: Registry) -> None:
    await registry.create_session(_session("s1"))
    await registry.set_session_status("s1", SessionStatus.PARKED)
    await registry.set_session_extra("s1", "a", 1)
    await registry.update_session_fields("s1", title="t")
    await registry.set_session_extra("s1", "b", 2)
    await registry.update_session_fields("s1", model="m")
    got = await registry.get_session("s1")
    assert got is not None
    assert (got.status, got.title, got.model, got.extra) == (SessionStatus.PARKED, "t", "m", {"a": 1, "b": 2})
    with pytest.raises(ValueError, match="status"):
        await registry.update_session_fields("s1", status="ended")


async def test_migrations_run_once_and_are_idempotent(relay_home: Path) -> None:
    db = paths.db_path()
    reg = Registry(db)
    await reg.open()
    assert await reg.schema_version() == len(MIGRATIONS)
    await reg.kv_set("model:vscode", "gpt-5")
    await reg.close()
    again = Registry(db)
    await again.open()
    assert await again.schema_version() == len(MIGRATIONS)
    assert await again.kv_get("model:vscode") == "gpt-5"
    await again.close()


async def test_migrating_an_old_database_cleans_it_up(relay_home: Path) -> None:
    db = paths.db_path()
    reg = Registry(db)
    await reg.open()
    await reg.close()
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version = 0")
    raw.execute("DROP INDEX IF EXISTS pending_status")
    raw.executemany(
        "INSERT INTO kv (key, value) VALUES (?, ?)",
        [
            ("model:vscode_local", "Run"),
            ("model:vscode", "Run"),
            ("model:opencode", "Run"),
            (f"{SEEN_PREFIX}m1", "1"),
        ],
    )
    raw.commit()
    raw.close()

    migrated = Registry(db)
    await migrated.open()
    assert await migrated.schema_version() == len(MIGRATIONS)
    assert await migrated.kv_get("model:vscode_local") is None
    assert await migrated.kv_get("model:vscode") is None
    assert await migrated.kv_get("model:opencode") == "Run", "only the VS Code sticky models are cleaned"
    assert await migrated.kv_get(f"{SEEN_PREFIX}m1") not in (None, "1"), "legacy markers get a timestamp"
    indexes = {r[1] for r in sqlite3.connect(db).execute("PRAGMA index_list(pending)")}
    assert "pending_status" in indexes
    await migrated.close()


async def test_rows_this_build_cannot_read_are_skipped(registry: Registry, relay_home: Path) -> None:
    await registry.create_session(_session("good"))
    await registry.create_session(_session("odd"))
    raw = sqlite3.connect(paths.db_path())
    raw.execute("UPDATE sessions SET status='from-the-future' WHERE session_key='odd'")
    raw.commit()
    raw.close()
    assert [s.session_key for s in await registry.list_sessions()] == ["good"]
    assert await registry.get_session("odd") is None


async def test_prune_seen_drops_only_old_markers(registry: Registry) -> None:
    now = utcnow()
    await registry.kv_set(f"{SEEN_PREFIX}old", (now - timedelta(days=9)).isoformat())
    await registry.kv_set(f"{SEEN_PREFIX}new", (now - timedelta(days=1)).isoformat())
    await registry.kv_set("webex.last_seen", "msg")
    assert await registry.prune_seen(now - timedelta(days=7)) == 1
    assert await registry.kv_get(f"{SEEN_PREFIX}old") is None
    assert await registry.kv_get(f"{SEEN_PREFIX}new") is not None
    assert await registry.kv_get("webex.last_seen") == "msg"
