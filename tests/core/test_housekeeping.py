"""Housekeeping drops stale approvals and card nonces and leaves everything else alone."""

from __future__ import annotations

from datetime import timedelta

from agent_relay.core.housekeeping import DRAFT_PREFIX, DRAFT_SEEN_PREFIX, PANIC_ISSUED_PREFIX, housekeep
from agent_relay.core.registry import CARD_NONCE_PREFIX, RECENT_APPROVAL_PREFIX, SEEN_PREFIX, Registry
from agent_relay.frontends.webex import handlers
from agent_relay.frontends.webex.commands import away as away_commands
from agent_relay.models.events import utcnow


def _ago(**delta: float) -> str:
    return (utcnow() - timedelta(**delta)).isoformat()


async def test_expired_recent_approvals_are_pruned(registry: Registry) -> None:
    keys = {
        "old": f"{_ago(minutes=5)} allow",
        "fresh": f"{_ago(seconds=10)} allow",
        "legacy": "allow",
    }
    for name, value in keys.items():
        await registry.kv_set(f"{RECENT_APPROVAL_PREFIX}s:{name}", value)
    assert await housekeep(registry) == 2
    assert await registry.kv_get(f"{RECENT_APPROVAL_PREFIX}s:old") is None
    assert await registry.kv_get(f"{RECENT_APPROVAL_PREFIX}s:legacy") is None
    assert await registry.kv_get(f"{RECENT_APPROVAL_PREFIX}s:fresh") is not None


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
