"""What a sent card leaves in the registry: a nonce, and the choices a question offered.

Every submit carries the card's `nonce`. Sending a card stores `card.nonce:<nonce>` with the time;
a submit pops it, so a second tap (double tap, another device) finds nothing and is refused.
A reusable card (a dashboard) keeps its nonce until it is `REUSABLE_TTL` old.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta

from agent_relay.core.registry import Registry
from agent_relay.core.service import RelayError
from agent_relay.models.events import utcnow

NONCE_PREFIX = "card.nonce:"
OFFER_PREFIX = "card.offer:"
EXPIRED = "This card has expired or was already answered."
REUSABLE_TTL = timedelta(minutes=15)


class InvalidInput(RelayError):
    """A card submit carried something the card never offered."""


async def issue_nonce(registry: Registry) -> str:
    nonce = secrets.token_urlsafe(9)
    await registry.kv_set(f"{NONCE_PREFIX}{nonce}", utcnow().isoformat())
    return nonce


async def consume_nonce(registry: Registry, nonce: object) -> str | None:
    """The stored time when the nonce was valid (now spent), else None."""
    if not isinstance(nonce, str) or not nonce:
        return None
    return await registry.kv_pop(f"{NONCE_PREFIX}{nonce}")


async def check_nonce(registry: Registry, nonce: object, max_age: timedelta = REUSABLE_TTL) -> str | None:
    """The stored time when the nonce is younger than `max_age`, leaving it valid. An older one is dropped."""
    if not isinstance(nonce, str) or not nonce:
        return None
    key = f"{NONCE_PREFIX}{nonce}"
    issued = await registry.kv_get(key)
    if issued is None:
        return None
    try:
        stamp = datetime.fromisoformat(issued)
    except ValueError:
        stamp = None
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    if stamp is None or utcnow() - stamp > max_age:
        await registry.kv_delete(key)
        return None
    return issued


async def restore_nonce(registry: Registry, nonce: object, issued: str) -> None:
    """Give a nonce back when the submit did nothing, so the same card can be tapped again."""
    if isinstance(nonce, str) and nonce:
        await registry.kv_set(f"{NONCE_PREFIX}{nonce}", issued)


async def store_offer(registry: Registry, request_id: str, options: tuple[str, ...]) -> None:
    await registry.kv_set(f"{OFFER_PREFIX}{request_id}", json.dumps(list(options)))


async def load_offer(registry: Registry, request_id: str) -> list[str]:
    raw = await registry.kv_get(f"{OFFER_PREFIX}{request_id}")
    if raw is None:
        return []
    found = json.loads(raw)
    return [o for o in found if isinstance(o, str)] if isinstance(found, list) else []


async def drop_offer(registry: Registry, request_id: str) -> None:
    await registry.kv_delete(f"{OFFER_PREFIX}{request_id}")
