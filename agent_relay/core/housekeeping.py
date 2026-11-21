"""Periodic cleanup of registry rows that would otherwise grow without bound."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from agent_relay.core.inbox import sweep_inbox
from agent_relay.core.lifecycle import Lifecycle
from agent_relay.core.registry import CARD_NONCE_PREFIX, Registry, stamped_at
from agent_relay.log import get_logger
from agent_relay.models.events import utcnow
from agent_relay.models.sessions import SessionStatus

log = get_logger(__name__)

SEEN_TTL = timedelta(days=7)
NONCE_TTL = timedelta(days=2)
STALE_CARD_TTL = timedelta(days=1)
# An idle session nobody has touched this long is over: its thread is retired and its files can go.
IDLE_SESSION_TTL = timedelta(days=7)
INTERVAL_S = 3600.0
# Written by the Webex frontend, which core cannot import; tests pin both to the frontend's constants.
PANIC_ISSUED_PREFIX = "panic.issued:"  # valued with the ISO-8601 time the confirm card was sent
DRAFT_PREFIX = "new.draft:"  # valued with JSON that carries no time
OFFER_PREFIX = "card.offer:"  # the choices a card offered, keyed by its nonce or a question's request id
# An offer has no time of its own: the first pass that sees one with no live nonce leaves its time here.
OFFER_SEEN_PREFIX = "housekeep.offer_seen:"
# A draft has no time of its own, so the first pass that sees it leaves its time here.
DRAFT_SEEN_PREFIX = "housekeep.draft_seen:"


async def _prune_drafts(registry: Registry, now: datetime, ttl: timedelta) -> int:
    """Drop `/new` drafts a day after they were first seen; a draft the user finished is already gone."""
    drafts = await registry.kv_prefix(DRAFT_PREFIX)
    markers = await registry.kv_prefix(DRAFT_SEEN_PREFIX)
    removed = 0
    for key in drafts:
        marker = f"{DRAFT_SEEN_PREFIX}{key}"
        first_seen = stamped_at(markers.get(marker, ""))
        if first_seen is None:
            await registry.kv_set(marker, now.isoformat())
        elif first_seen < now - ttl:
            await registry.kv_delete(key)
            await registry.kv_delete(marker)
            removed += 1
    for marker in markers:
        if marker[len(DRAFT_SEEN_PREFIX) :] not in drafts:
            await registry.kv_delete(marker)
    return removed


async def _prune_offers(registry: Registry, now: datetime, ttl: timedelta) -> int:
    """Drop the choices of cards that were never submitted: no live nonce, and a day since first seen.

    A card's offer is keyed by its nonce, which the first submit spends; the offer stays behind when the
    card is never used. A question's offer is keyed by its request id and dropped when it is answered."""
    offers = await registry.kv_prefix(OFFER_PREFIX)
    nonces = await registry.kv_prefix(CARD_NONCE_PREFIX)
    markers = await registry.kv_prefix(OFFER_SEEN_PREFIX)
    removed = 0
    for key in offers:
        marker = f"{OFFER_SEEN_PREFIX}{key}"
        if f"{CARD_NONCE_PREFIX}{key[len(OFFER_PREFIX) :]}" in nonces:
            await registry.kv_delete(marker)
            continue
        first_seen = stamped_at(markers.get(marker, ""))
        if first_seen is None:
            await registry.kv_set(marker, now.isoformat())
        elif first_seen < now - ttl:
            await registry.kv_delete(key)
            await registry.kv_delete(marker)
            removed += 1
    for marker in markers:
        if marker[len(OFFER_SEEN_PREFIX) :] not in offers:
            await registry.kv_delete(marker)
    return removed


async def reconcile_sessions(registry: Registry, lifecycle: Lifecycle) -> int:
    """At relayd start no turn is live, so a session still marked running is idle. Returns how many."""
    count = 0
    for session in await registry.list_sessions():
        if session.status is SessionStatus.RUNNING:
            await lifecycle.transition(session.session_key, SessionStatus.IDLE, "relayd restarted")
            count += 1
    return count


async def end_idle_sessions(
    registry: Registry,
    lifecycle: Lifecycle,
    ttl: timedelta = IDLE_SESSION_TTL,
    *,
    now: datetime | None = None,
) -> int:
    """End idle sessions not touched for `ttl`. Returns how many."""
    cutoff = (now or utcnow()) - ttl
    count = 0
    for session in await registry.list_sessions():
        if session.status is SessionStatus.IDLE and session.updated < cutoff:
            await lifecycle.transition(session.session_key, SessionStatus.ENDED, "idle for 7 days")
            count += 1
    return count


async def housekeep(
    registry: Registry,
    seen_ttl: timedelta = SEEN_TTL,
    nonce_ttl: timedelta = NONCE_TTL,
    *,
    now: datetime | None = None,
    lifecycle: Lifecycle | None = None,
) -> int:
    """One pass. Returns how many rows it removed or ended."""
    now = now or utcnow()
    seen = await registry.prune_seen(now - seen_ttl)
    nonces = await registry.prune_stamped(CARD_NONCE_PREFIX, now - nonce_ttl, drop_unparseable=False)
    panics = await registry.prune_stamped(PANIC_ISSUED_PREFIX, now - STALE_CARD_TTL, drop_unparseable=True)
    drafts = await _prune_drafts(registry, now, STALE_CARD_TTL)
    offers = await _prune_offers(registry, now, STALE_CARD_TTL)
    files = await sweep_inbox(registry)
    idle = await end_idle_sessions(registry, lifecycle, now=now) if lifecycle is not None else 0
    for count, what in (
        (seen, "processed-message marker"),
        (nonces, "card nonce"),
        (panics, "stale panic confirmation"),
        (drafts, "abandoned /new draft"),
        (offers, "unused card offer"),
        (files, "phone file folder or held file"),
        (idle, "idle session ended"),
    ):
        if count:
            log.info(f"pruned {count} {what}(s)")
    return seen + nonces + panics + drafts + offers + files + idle


async def housekeeper(
    registry: Registry, interval: float = INTERVAL_S, lifecycle: Lifecycle | None = None
) -> None:
    while True:
        try:
            await housekeep(registry, lifecycle=lifecycle)
        except Exception:
            log.exception("housekeeping failed")
        await asyncio.sleep(interval)
