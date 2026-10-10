"""Workspace claims that link a phone-started VS Code chat to its first hook."""

from __future__ import annotations

import asyncio

from agent_relay.core.claims import claim_key, make_claim, take_claim
from agent_relay.core.registry import Registry


async def test_a_claim_is_taken_once(registry: Registry) -> None:
    await make_claim(registry, "/w/proj", "vscode:phone-1")
    claim = await take_claim(registry, "/w/proj")
    assert claim is not None and claim.session_key == "vscode:phone-1"
    assert await take_claim(registry, "/w/proj") is None


async def test_concurrent_takers_get_one_claim(registry: Registry) -> None:
    await make_claim(registry, "/w/proj", "k")
    results = await asyncio.gather(*(take_claim(registry, "/w/proj") for _ in range(5)))
    assert sum(r is not None for r in results) == 1


async def test_an_expired_claim_is_dropped(registry: Registry) -> None:
    await make_claim(registry, "/w/proj", "k", ttl=10, clock=lambda: 1000.0)
    assert await take_claim(registry, "/w/proj", clock=lambda: 1011.0) is None
    assert await registry.kv_get(claim_key("/w/proj")) is None


async def test_a_malformed_claim_is_dropped(registry: Registry) -> None:
    await registry.kv_set(claim_key("/w/proj"), "not json")
    assert await take_claim(registry, "/w/proj") is None
    assert await registry.kv_get(claim_key("/w/proj")) is None


async def test_claims_are_per_workspace_and_need_one(registry: Registry) -> None:
    await make_claim(registry, "/w/a", "k")
    assert await take_claim(registry, "/w/b") is None
    assert await take_claim(registry, None) is None
    assert await take_claim(registry, "/w/a/../a") is not None


async def test_kv_pop_returns_and_removes(registry: Registry) -> None:
    await registry.kv_set("x", "1")
    assert await registry.kv_pop("x") == "1"
    assert await registry.kv_pop("x") is None
