"""Periodic cleanup of registry rows that would otherwise grow without bound."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from agent_relay.core.inbox import sweep_inbox
from agent_relay.core.registry import (
    CARD_NONCE_PREFIX,
    RECENT_APPROVAL_PREFIX,
    RECENT_APPROVAL_TTL,
    Registry,
)
from agent_relay.core.service.approvals import WORKSPACE_GRANT_PREFIX
from agent_relay.log import get_logger
from agent_relay.models.events import utcnow

log = get_logger(__name__)

SEEN_TTL = timedelta(days=7)
NONCE_TTL = timedelta(days=2)
INTERVAL_S = 3600.0


async def housekeep(
    registry: Registry, seen_ttl: timedelta = SEEN_TTL, nonce_ttl: timedelta = NONCE_TTL
) -> int:
    """One pass. Returns how many rows it removed."""
    now = utcnow()
    seen = await registry.prune_seen(now - seen_ttl)
    # A recent approval without a readable time predates the expiry; it is never honoured, so drop it.
    approvals = await registry.prune_stamped(
        RECENT_APPROVAL_PREFIX, now - RECENT_APPROVAL_TTL, drop_unparseable=True
    )
    nonces = await registry.prune_stamped(CARD_NONCE_PREFIX, now - nonce_ttl, drop_unparseable=False)
    # A workspace grant's value is its expiry; one without a readable time is never honoured.
    grants = await registry.prune_stamped(WORKSPACE_GRANT_PREFIX, now, drop_unparseable=True)
    files = await sweep_inbox(registry)
    for count, what in (
        (seen, "processed-message marker"),
        (approvals, "recent approval"),
        (nonces, "card nonce"),
        (grants, "expired workspace grant"),
        (files, "phone file folder or held file"),
    ):
        if count:
            log.info(f"pruned {count} {what}(s)")
    return seen + approvals + nonces + grants + files


async def housekeeper(registry: Registry, interval: float = INTERVAL_S) -> None:
    while True:
        try:
            await housekeep(registry)
        except Exception:
            log.exception("housekeeping failed")
        await asyncio.sleep(interval)
