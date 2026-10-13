"""The presence check: whether away-on-lock is on and whether the lock state can be read."""

from __future__ import annotations

from agent_relay.core.presence import LockState, Probe
from agent_relay.doctor import local
from agent_relay.models.config import PresenceConfig, RelayConfig


def probe_of(state: LockState) -> Probe:
    async def probe() -> LockState:
        return state

    return probe


async def test_a_readable_lock_state_is_ok_and_shown() -> None:
    check = await local.presence_check(RelayConfig(), probe_of("unlocked"))
    assert (check.status, check.area) == ("ok", "Presence")
    assert "unlocked" in check.detail
    locked = await local.presence_check(RelayConfig(), probe_of("locked"))
    assert locked.status == "ok" and "locked" in locked.detail


async def test_an_unreadable_lock_state_is_a_warning() -> None:
    check = await local.presence_check(RelayConfig(), probe_of("unknown"))
    assert check.status == "warn"
    assert "cannot be read" in check.detail


async def test_the_feature_off_is_reported_without_probing() -> None:
    calls: list[int] = []

    async def probe() -> LockState:
        calls.append(1)
        return "locked"

    config = RelayConfig(presence=PresenceConfig(auto_away_on_lock=False))
    check = await local.presence_check(config, probe)
    assert check.status == "info" and "off" in check.detail
    assert check.fix == "relay config set presence.auto_away_on_lock true"
    assert calls == []
