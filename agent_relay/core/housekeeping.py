"""Periodic cleanup of registry rows that would otherwise grow without bound."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from agent_relay.core.inbox import sweep_inbox
from agent_relay.core.registry import (
    CARD_NONCE_PREFIX,
    RECENT_APPROVAL_PREFIX,
    RECENT_APPROVAL_TTL,
    Registry,
    stamped_at,
)
from agent_relay.core.service.approvals import WORKSPACE_GRANT_PREFIX
from agent_relay.log import get_logger
from agent_relay.models.events import utcnow

log = get_logger(__name__)

SEEN_TTL = timedelta(days=7)
NONCE_TTL = timedelta(days=2)
STALE_CARD_TTL = timedelta(days=1)
INTERVAL_S = 3600.0
# Written by the Webex frontend, which core cannot import; tests pin both to the frontend's constants.
PANIC_ISSUED_PREFIX = "panic.issued:"  # valued with the ISO-8601 time the confirm card was sent
DRAFT_PREFIX = "new.draft:"  # valued with JSON that carries no time
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


async def housekeep(
    registry: Registry,
    seen_ttl: timedelta = SEEN_TTL,
    nonce_ttl: timedelta = NONCE_TTL,
    *,
    now: datetime | None = None,
) -> int:
    """One pass. Returns how many rows it removed."""
    now = now or utcnow()
    seen = await registry.prune_seen(now - seen_ttl)
    # A recent approval without a readable time predates the expiry; it is never honoured, so drop it.
    approvals = await registry.prune_stamped(
        RECENT_APPROVAL_PREFIX, now - RECENT_APPROVAL_TTL, drop_unparseable=True
    )
    nonces = await registry.prune_stamped(CARD_NONCE_PREFIX, now - nonce_ttl, drop_unparseable=False)
    # A workspace grant's value is its expiry; one without a readable time is never honoured.
    grants = await registry.prune_stamped(WORKSPACE_GRANT_PREFIX, now, drop_unparseable=True)
    panics = await registry.prune_stamped(PANIC_ISSUED_PREFIX, now - STALE_CARD_TTL, drop_unparseable=True)
    drafts = await _prune_drafts(registry, now, STALE_CARD_TTL)
    files = await sweep_inbox(registry)
    for count, what in (
        (seen, "processed-message marker"),
        (approvals, "recent approval"),
        (nonces, "card nonce"),
        (grants, "expired workspace grant"),
        (panics, "stale panic confirmation"),
        (drafts, "abandoned /new draft"),
        (files, "phone file folder or held file"),
    ):
        if count:
            log.info(f"pruned {count} {what}(s)")
    return seen + approvals + nonces + grants + panics + drafts + files


async def housekeeper(registry: Registry, interval: float = INTERVAL_S) -> None:
    while True:
        try:
            await housekeep(registry)
        except Exception:
            log.exception("housekeeping failed")
        await asyncio.sleep(interval)
