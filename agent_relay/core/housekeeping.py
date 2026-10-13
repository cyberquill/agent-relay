"""Periodic cleanup of registry rows that would otherwise grow without bound."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from agent_relay.core.registry import Registry
from agent_relay.log import get_logger
from agent_relay.models.events import utcnow

log = get_logger(__name__)

SEEN_TTL = timedelta(days=7)
INTERVAL_S = 3600.0


async def housekeep(registry: Registry, seen_ttl: timedelta = SEEN_TTL) -> int:
    """One pass. Returns how many rows it removed."""
    pruned = await registry.prune_seen(utcnow() - seen_ttl)
    if pruned:
        log.info(f"pruned {pruned} processed-message marker(s)")
    return pruned


async def housekeeper(registry: Registry, interval: float = INTERVAL_S) -> None:
    while True:
        try:
            await housekeep(registry)
        except Exception:
            log.exception("housekeeping failed")
        await asyncio.sleep(interval)
