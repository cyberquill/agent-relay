"""Housekeeping drops stale card nonces and old markers, reconciles sessions at start, and ends idle ones."""

from __future__ import annotations

from datetime import timedelta

from agent_relay.core.housekeeping import (
    DRAFT_PREFIX,
    DRAFT_SEEN_PREFIX,
    OFFER_PREFIX,
    OFFER_SEEN_PREFIX,
    PANIC_ISSUED_PREFIX,
    end_idle_sessions,
    housekeep,
    reconcile_sessions,
)
from agent_relay.core.lifecycle import Lifecycle
from agent_relay.core.registry import CARD_NONCE_PREFIX, SEEN_PREFIX, Registry
from agent_relay.core.router import Router
from agent_relay.core.signals import Signals
from agent_relay.frontends import card_state
from agent_relay.frontends.webex import handlers
from agent_relay.frontends.webex.commands import away as away_commands
from agent_relay.models.events import Harness, Origin, utcnow
from agent_relay.models.sessions import SessionRecord, SessionStatus


def _ago(**delta: float) -> str:
    return (utcnow() - timedelta(**delta)).isoformat()


async def test_card_nonces_older_than_two_days_are_pruned(registry: Registry) -> None:
    await registry.kv_set(f"{CARD_NONCE_PREFIX}old", _ago(days=3))
    await registry.kv_set(f"{CARD_NONCE_PREFIX}fresh", _ago(hours=5))
    await registry.kv_set(
        f"{CARD_NONCE_PREFIX}naive", (utcnow() - timedelta(days=4)).replace(tzinfo=None).isoformat()
    )
    assert await housekeep(registry) == 2
    assert await registry.kv_get(f"{CARD_NONCE_PREFIX}old") is None
    assert await registry.kv_get(f"{CARD_NONCE_PREFIX}naive") is None
    assert await registry.kv_get(f"{CARD_NONCE_PREFIX}fresh") is not None


async def test_nonces_that_are_not_timestamps_are_left_alone(registry: Registry) -> None:
    await registry.kv_set(f"{CARD_NONCE_PREFIX}odd", "not a time")
    await registry.kv_set(f"{CARD_NONCE_PREFIX}empty", "")
    assert await housekeep(registry) == 0
    assert await registry.kv_get(f"{CARD_NONCE_PREFIX}odd") == "not a time"
    assert await registry.kv_get(f"{CARD_NONCE_PREFIX}empty") == ""


async def test_other_keys_and_old_markers_follow_their_own_rules(registry: Registry) -> None:
    await registry.kv_set(f"{SEEN_PREFIX}m1", _ago(days=9))
    await registry.kv_set("webex.last_seen", "msg")
    await registry.kv_set("approved_call:t1", "allow")
    assert await housekeep(registry) == 1
    assert await registry.kv_get("webex.last_seen") == "msg"
    assert await registry.kv_get("approved_call:t1") == "allow"


def test_the_prefixes_match_the_ones_the_webex_frontend_writes() -> None:
    assert PANIC_ISSUED_PREFIX == away_commands.PANIC_ISSUED_PREFIX
    assert DRAFT_PREFIX == handlers.DRAFT_PREFIX
    assert OFFER_PREFIX == card_state.OFFER_PREFIX


async def test_panic_confirmations_older_than_a_day_are_pruned(registry: Registry) -> None:
    await registry.kv_set(f"{PANIC_ISSUED_PREFIX}old", _ago(days=2))
    await registry.kv_set(f"{PANIC_ISSUED_PREFIX}fresh", _ago(hours=2))
    await registry.kv_set(f"{PANIC_ISSUED_PREFIX}odd", "not a time")
    assert await housekeep(registry) == 2
    assert await registry.kv_get(f"{PANIC_ISSUED_PREFIX}old") is None
    assert await registry.kv_get(f"{PANIC_ISSUED_PREFIX}odd") is None
    assert await registry.kv_get(f"{PANIC_ISSUED_PREFIX}fresh") is not None


async def test_a_new_draft_is_dropped_a_day_after_it_was_first_seen(registry: Registry) -> None:
    start = utcnow()
    await registry.kv_set(f"{DRAFT_PREFIX}n1", '{"app": "vscode"}')
    assert await housekeep(registry, now=start) == 0
    assert await housekeep(registry, now=start + timedelta(hours=23)) == 0
    assert await registry.kv_get(f"{DRAFT_PREFIX}n1") is not None
    assert await housekeep(registry, now=start + timedelta(hours=25)) == 1
    assert await registry.kv_get(f"{DRAFT_PREFIX}n1") is None
    assert await registry.kv_prefix(DRAFT_SEEN_PREFIX) == {}


async def test_a_finished_draft_leaves_no_marker_behind(registry: Registry) -> None:
    start = utcnow()
    await registry.kv_set(f"{DRAFT_PREFIX}n1", "{}")
    await housekeep(registry, now=start)
    assert len(await registry.kv_prefix(DRAFT_SEEN_PREFIX)) == 1
    await registry.kv_delete(f"{DRAFT_PREFIX}n1")
    assert await housekeep(registry, now=start + timedelta(hours=1)) == 0
    assert await registry.kv_prefix(DRAFT_SEEN_PREFIX) == {}


def _lifecycle(registry: Registry) -> Lifecycle:
    return Lifecycle(registry, Router(), Signals())


async def _session(registry: Registry, key: str, status: SessionStatus) -> None:
    await registry.create_session(
        SessionRecord(
            session_key=key,
            harness=Harness.OPENCODE,
            harness_id=f"h-{key}",
            workspace="/w",
            origin=Origin.PHONE,
        )
    )
    await registry.set_session_status(key, status)


async def _status(registry: Registry, key: str) -> SessionStatus:
    found = await registry.get_session(key)
    assert found is not None
    return found.status


async def test_start_up_marks_running_sessions_idle_and_touches_nothing_else(registry: Registry) -> None:
    for key, status in (
        ("run1", SessionStatus.RUNNING),
        ("run2", SessionStatus.RUNNING),
        ("idle", SessionStatus.IDLE),
        ("ended", SessionStatus.ENDED),
    ):
        await _session(registry, key, status)
    assert await reconcile_sessions(registry, _lifecycle(registry)) == 2
    assert [await _status(registry, k) for k in ("run1", "run2", "idle", "ended")] == [
        SessionStatus.IDLE,
        SessionStatus.IDLE,
        SessionStatus.IDLE,
        SessionStatus.ENDED,
    ]
    assert await reconcile_sessions(registry, _lifecycle(registry)) == 0


async def test_idle_sessions_untouched_for_seven_days_are_ended(registry: Registry) -> None:
    for key, status in (
        ("idle-a", SessionStatus.IDLE),
        ("idle-b", SessionStatus.IDLE),
        ("running", SessionStatus.RUNNING),
        ("parked", SessionStatus.PARKED),
    ):
        await _session(registry, key, status)
    lifecycle = _lifecycle(registry)
    assert await end_idle_sessions(registry, lifecycle, now=utcnow() + timedelta(days=6)) == 0
    assert await end_idle_sessions(registry, lifecycle, now=utcnow() + timedelta(days=8)) == 2
    assert [await _status(registry, k) for k in ("idle-a", "idle-b", "running", "parked")] == [
        SessionStatus.ENDED,
        SessionStatus.ENDED,
        SessionStatus.RUNNING,
        SessionStatus.PARKED,
    ]


async def test_housekeep_ends_idle_sessions_only_when_given_a_lifecycle(registry: Registry) -> None:
    await _session(registry, "old-idle", SessionStatus.IDLE)
    later = utcnow() + timedelta(days=8)
    assert await housekeep(registry, now=later) == 0
    assert await _status(registry, "old-idle") is SessionStatus.IDLE
    assert await housekeep(registry, now=later, lifecycle=_lifecycle(registry)) == 1
    assert await _status(registry, "old-idle") is SessionStatus.ENDED


async def test_an_offer_with_no_live_nonce_is_dropped_a_day_after_it_was_first_seen(
    registry: Registry,
) -> None:
    start = utcnow()
    await registry.kv_set(f"{OFFER_PREFIX}used", '["a"]')  # its card was submitted: the nonce is spent
    await registry.kv_set(f"{OFFER_PREFIX}open", '["a"]')
    await registry.kv_set(f"{CARD_NONCE_PREFIX}open", _ago(hours=1))
    assert await housekeep(registry, now=start) == 0
    assert await housekeep(registry, now=start + timedelta(hours=23)) == 0
    assert await housekeep(registry, now=start + timedelta(hours=25)) == 1
    assert await registry.kv_get(f"{OFFER_PREFIX}used") is None
    assert await registry.kv_get(f"{OFFER_PREFIX}open") is not None
    assert await registry.kv_prefix(OFFER_SEEN_PREFIX) == {}
