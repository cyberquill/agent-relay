"""Away mode that follows the screen lock: the ioreg probe, the lock/unlock rules and what never changes."""

from __future__ import annotations

import asyncio
import plistlib
import sys
from datetime import timedelta

import pytest

from agent_relay import paths
from agent_relay.core import away, presence
from agent_relay.core.keys import RELEASE_KEY
from agent_relay.core.presence import AUTO_KEY, LOCKED_NOTICE, UNLOCKED_NOTICE, LockState, PresenceMonitor
from agent_relay.core.registry import GLOBAL_SCOPE, Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayService
from agent_relay.core.service.common import DESK_DECISION
from agent_relay.core.signals import Signals
from agent_relay.models.config import PresenceConfig, RelayConfig
from agent_relay.models.events import Harness, Origin, utcnow
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus


def ioreg(*users: dict[str, object], locked: bool | None = None, as_list: bool = False) -> bytes:
    root: dict[str, object] = {"IOConsoleUsers": list(users)}
    if locked is not None:
        root["IOConsoleLocked"] = locked
    return plistlib.dumps([root] if as_list else root)


UNLOCKED_USER: dict[str, object] = {"kCGSSessionOnConsoleKey": True, "kCGSSessionUserNameKey": "me"}
LOCKED_USER: dict[str, object] = {**UNLOCKED_USER, "CGSSessionScreenIsLocked": True}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (ioreg(UNLOCKED_USER), "unlocked"),
        (ioreg(LOCKED_USER), "locked"),
        (ioreg(LOCKED_USER, as_list=True), "locked"),
        (ioreg({**LOCKED_USER, "CGSSessionScreenIsLocked": False}), "unlocked"),
        # Only the user on the console counts: a background fast-user-switched session is not the screen.
        (
            ioreg({"kCGSSessionOnConsoleKey": False, "CGSSessionScreenIsLocked": True}, UNLOCKED_USER),
            "unlocked",
        ),
        (ioreg({"kCGSSessionOnConsoleKey": False}, locked=True), "locked"),
        (ioreg(locked=False), "unknown"),
        (ioreg(), "unknown"),
        (b"", "unknown"),
        (b"not a plist", "unknown"),
        (b"<?xml version='1.0'?><plist><dict>", "unknown"),
        (plistlib.dumps("text"), "unknown"),
        (plistlib.dumps([]), "unknown"),
    ],
)
def test_parse_lock_state(raw: bytes, expected: LockState) -> None:
    assert presence.parse_lock_state(raw) == expected


def fake_ioreg(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    monkeypatch.setattr(presence, "IOREG", (sys.executable, "-c", script))


async def test_probe_reads_the_plist_the_command_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ioreg(monkeypatch, f"import sys; sys.stdout.buffer.write({ioreg(LOCKED_USER)!r})")
    assert await presence.probe_ioreg() == "locked"


async def test_probe_is_unknown_when_the_command_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ioreg(monkeypatch, f"import sys; sys.stdout.buffer.write({ioreg(LOCKED_USER)!r}); sys.exit(1)")
    assert await presence.probe_ioreg() == "unknown"


async def test_probe_is_unknown_when_the_binary_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(presence, "IOREG", ("/nonexistent/ioreg",))
    assert await presence.probe_ioreg() == "unknown"


async def test_probe_gives_up_on_a_hung_command(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ioreg(monkeypatch, "import time; time.sleep(30)")
    monkeypatch.setattr(presence, "PROBE_TIMEOUT_S", 0.3)
    assert await presence.probe_ioreg() == "unknown"


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({presence.ON_CONSOLE: True}, "unlocked"),
        ({presence.ON_CONSOLE: True, presence.SCREEN_LOCKED: False}, "unlocked"),
        ({presence.ON_CONSOLE: True, presence.SCREEN_LOCKED: True}, "locked"),
        ({presence.ON_CONSOLE: False, presence.SCREEN_LOCKED: True}, "unknown"),
        ({presence.SCREEN_LOCKED: True}, "unknown"),
        ({}, "unknown"),
        (None, "unknown"),
    ],
)
def test_session_lock_state(flags: dict[str, bool] | None, expected: LockState) -> None:
    assert presence.session_lock_state(flags) == expected


def test_this_session_is_read_in_process() -> None:
    flags = presence.read_session()
    assert flags is not None and flags.get(presence.ON_CONSOLE) is not None


async def test_probe_answers_from_the_session_without_running_ioreg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        presence, "read_session", lambda: {presence.ON_CONSOLE: True, presence.SCREEN_LOCKED: True}
    )
    monkeypatch.setattr(presence, "IOREG", ("/nonexistent/ioreg",))
    assert await presence.probe_lock_state() == "locked"


@pytest.mark.parametrize("flags", [None, {presence.ON_CONSOLE: False}])
async def test_probe_falls_back_to_ioreg(
    monkeypatch: pytest.MonkeyPatch, flags: dict[str, bool] | None
) -> None:
    monkeypatch.setattr(presence, "read_session", lambda: flags)
    fake_ioreg(monkeypatch, f"import sys; sys.stdout.buffer.write({ioreg(LOCKED_USER)!r})")
    assert await presence.probe_lock_state() == "locked"


class Screen:
    """A probe the test steers; `states` queues what the next polls will see."""

    def __init__(self) -> None:
        self.states: list[LockState] = []
        self.calls = 0

    async def __call__(self) -> LockState:
        self.calls += 1
        return self.states.pop(0) if self.states else "unknown"


class Rig:
    def __init__(self, registry: Registry, config: PresenceConfig | None = None) -> None:
        self.registry = registry
        self.service = RelayService(registry, Router(), Signals(), RelayConfig())
        self.screen = Screen()
        self.posts: list[str] = []
        self.fail_posts = False
        self.config = config or PresenceConfig()
        self.monitor = self.build()

    def build(self) -> PresenceMonitor:
        return PresenceMonitor(self.registry, self.service, self.config, self.post, self.screen)

    async def post(self, text: str) -> None:
        if self.fail_posts:
            raise OSError("webex is down")
        self.posts.append(text)

    async def see(self, state: LockState) -> None:
        self.screen.states.append(state)
        await self.monitor.tick()

    async def away(self) -> bool:
        return (await self.registry.away_scopes()).get(GLOBAL_SCOPE, False)

    async def session(self, key: str = "s1", origin: Origin = Origin.DESK) -> None:
        await self.registry.create_session(
            SessionRecord(
                session_key=key, harness=Harness.OPENCODE, harness_id=key, workspace="/w", origin=origin
            )
        )


@pytest.fixture
def rig(registry: Registry) -> Rig:
    return Rig(registry)


async def test_lock_turns_away_on_and_unlock_turns_it_off(rig: Rig) -> None:
    await rig.session()
    await rig.see("locked")
    assert await rig.away()
    assert await rig.registry.kv_get(AUTO_KEY) == "lock"
    assert rig.posts == [LOCKED_NOTICE]
    await rig.see("unlocked")
    assert not await rig.away()
    assert await rig.registry.kv_get(AUTO_KEY) is None
    assert rig.posts == [LOCKED_NOTICE, UNLOCKED_NOTICE]


async def test_no_notice_without_a_live_session(rig: Rig) -> None:
    await rig.see("locked")
    await rig.see("unlocked")
    assert rig.posts == []
    await rig.session()
    await rig.registry.set_session_status("s1", SessionStatus.ENDED)
    await rig.see("locked")
    assert await rig.away() and rig.posts == []


async def test_a_failing_notice_does_not_undo_the_change(rig: Rig) -> None:
    await rig.session()
    rig.fail_posts = True
    await rig.see("locked")
    assert await rig.away()


async def test_away_set_by_the_user_is_left_alone_at_lock_and_unlock(rig: Rig) -> None:
    await rig.session()
    await rig.registry.set_away(True)
    await rig.see("locked")
    await rig.see("unlocked")
    assert await rig.away()
    assert await rig.registry.kv_get(AUTO_KEY) is None
    assert rig.posts == []


async def test_turning_away_off_while_locked_is_not_undone_or_repeated(rig: Rig) -> None:
    await rig.session()
    await rig.see("locked")
    await rig.service.set_away_for(False, None)
    assert await rig.registry.kv_get(AUTO_KEY) is None
    await rig.see("unlocked")
    assert not await rig.away()
    assert rig.posts == [LOCKED_NOTICE]


async def test_turning_away_on_by_hand_while_locked_survives_the_unlock(rig: Rig) -> None:
    await rig.session()
    await rig.see("locked")
    await rig.service.set_away_for(False, None)
    await rig.service.set_away_for(True, None)
    await rig.see("unlocked")
    assert await rig.away()
    assert rig.posts == [LOCKED_NOTICE]


async def test_panic_while_locked_clears_the_marker(rig: Rig) -> None:
    await rig.see("locked")
    await rig.service.panic()
    assert await rig.registry.kv_get(AUTO_KEY) is None
    await rig.see("unlocked")
    assert not await rig.away()


async def test_a_workspace_scoped_change_does_not_claim_the_global_setting(rig: Rig) -> None:
    await rig.see("locked")
    await rig.service.set_away_for(True, "/w")
    assert await rig.registry.kv_get(AUTO_KEY) == "lock"
    await rig.see("unlocked")
    assert not await rig.away()
    assert (await rig.registry.away_scopes())[away.scope_for("/w")] is True


async def test_an_unknown_probe_changes_nothing(rig: Rig) -> None:
    await rig.session()
    await rig.see("unknown")
    assert not await rig.away()
    await rig.see("locked")
    await rig.see("unknown")
    assert await rig.away() and await rig.registry.kv_get(AUTO_KEY) == "lock"
    await rig.see("locked")
    assert rig.posts == [LOCKED_NOTICE]
    await rig.see("unknown")
    await rig.see("unlocked")
    assert not await rig.away()
    assert rig.posts == [LOCKED_NOTICE, UNLOCKED_NOTICE]


async def test_repeated_states_do_not_repeat_the_action(rig: Rig) -> None:
    await rig.session()
    await rig.see("locked")
    await rig.see("locked")
    await rig.see("unlocked")
    await rig.see("unlocked")
    assert rig.posts == [LOCKED_NOTICE, UNLOCKED_NOTICE]


async def test_the_feature_off_never_touches_away(registry: Registry) -> None:
    rig = Rig(registry, PresenceConfig(auto_away_on_lock=False))
    await rig.session()
    await rig.see("locked")
    await rig.see("unlocked")
    assert not await rig.away() and rig.posts == []


async def test_a_paused_relay_is_not_resumed_by_a_lock(rig: Rig) -> None:
    paths.pause_flag().parent.mkdir(parents=True, exist_ok=True)
    paths.pause_flag().write_text("now", encoding="utf-8")
    await rig.see("locked")
    assert not await rig.away()
    assert paths.pause_flag().exists()


async def test_unlock_hands_desk_approvals_back_like_away_off(rig: Rig) -> None:
    await rig.session("s1", Origin.DESK)
    await rig.registry.add_pending(
        PendingRequest(
            request_id="r1",
            kind="approval",
            session_key="s1",
            payload={"tool": "bash"},
            deadline=utcnow() + timedelta(minutes=5),
        )
    )
    await rig.see("locked")
    await rig.see("unlocked")
    pending = await rig.registry.get_pending("r1")
    assert pending is not None and pending.answer == {"decision": DESK_DECISION, "by": "desk"}


async def test_unlock_releases_parked_sessions(rig: Rig) -> None:
    await rig.session()
    await rig.see("locked")
    await rig.registry.set_session_status("s1", SessionStatus.PARKED)
    await rig.see("unlocked")
    assert await rig.registry.kv_get(RELEASE_KEY.format(session_key="s1")) == "away mode off"


async def test_a_restart_while_locked_still_unlocks_what_it_locked(rig: Rig) -> None:
    await rig.see("locked")
    restarted = rig.build()
    rig.screen.states.append("unlocked")
    await restarted.tick()
    assert not await rig.away()
    assert await rig.registry.kv_get(AUTO_KEY) is None


async def test_a_restart_on_a_locked_screen_turns_away_on(registry: Registry) -> None:
    first = Rig(registry)
    first.screen.states.append("locked")
    await first.monitor.tick()
    assert await first.away()
    await registry.set_away(False)
    second = first.build()
    first.screen.states.append("locked")
    await second.tick()
    assert await first.away()


async def test_run_polls_survives_a_failing_probe_and_cancels_cleanly(registry: Registry) -> None:
    rig = Rig(registry, PresenceConfig.model_construct(auto_away_on_lock=True, poll_s=0.01))
    outcomes: list[LockState | Exception] = [RuntimeError("boom"), "locked"]

    async def flaky() -> LockState:
        step = outcomes.pop(0) if outcomes else "locked"
        if isinstance(step, Exception):
            raise step
        return step

    monitor = PresenceMonitor(registry, rig.service, rig.config, rig.post, flaky)
    task = asyncio.create_task(monitor.run())
    for _ in range(300):
        if await rig.away():
            break
        await asyncio.sleep(0.01)
    assert await rig.away()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
