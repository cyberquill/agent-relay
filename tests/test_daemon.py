"""AC-2: relayd refuses to start without its secret or allowed user, with the fixing command; plus the
single-instance lock and an end-to-end boot against the fake Webex."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import keyring
import keyring.errors
import pytest

from agent_relay import daemon, ipc, paths
from agent_relay.core.presence import LockState
from agent_relay.frontends.webex.ws_client import WebSocketLike
from agent_relay.models.config import AdaptersConfig, PresenceConfig, RelayConfig, WebexConfig
from agent_relay.settings import SECRET_SETUP_COMMAND, Secret, save_config, set_secret
from tests.fakes.webex import U2C, FakeWebex

REPO = Path(__file__).resolve().parents[1]


def _configure(email: str = "me@example.com") -> None:
    save_config(RelayConfig(webex=WebexConfig(allowed_email=email)))


def test_missing_token_exits_2_with_setup_command(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    assert daemon.main() == daemon.EXIT_CONFIG
    err = capsys.readouterr().err
    assert SECRET_SETUP_COMMAND[Secret.WEBEX_BOT_TOKEN] in err
    assert "webex-bot-token" in err


def test_a_locked_keychain_exits_2_and_says_locked(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure()

    def locked(service: str, username: str) -> str:
        raise keyring.errors.KeyringLocked("(-25308, 'Unknown Error')")

    monkeypatch.setattr(keyring, "get_password", locked)
    assert daemon.main() == daemon.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "locked" in err and "unlock-keychain" in err
    assert "is missing" not in err


def test_missing_allowed_email_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    set_secret(Secret.WEBEX_BOT_TOKEN, "test-token")
    _configure(email="")
    assert daemon.main() == daemon.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "relay config set webex.allowed_email" in err
    assert "test-token" not in err


def test_missing_token_in_a_real_process(relay_home: Path) -> None:
    _configure()
    env = {
        **os.environ,
        "RELAY_HOME": str(relay_home),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "agent_relay.daemon"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == daemon.EXIT_CONFIG
    assert SECRET_SETUP_COMMAND[Secret.WEBEX_BOT_TOKEN] in proc.stderr


def test_second_instance_is_refused() -> None:
    first = daemon.acquire_lock()
    try:
        with pytest.raises(daemon.AlreadyRunningError):
            daemon.acquire_lock()
    finally:
        first.close()
    daemon.acquire_lock().close()


class IdleSocket:
    """Mercury that delivers scripted frames and then stays quiet until the test stops the daemon."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []
        self._quiet = asyncio.Event()

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if self.frames:
            return self.frames.pop(0)
        await self._quiet.wait()
        return ""

    def connect(self, _: str) -> AbstractAsyncContextManager[WebSocketLike]:
        @asynccontextmanager
        async def _cm() -> AsyncIterator[WebSocketLike]:
            yield self

        return _cm()


async def _until(check: object, timeout: float = 5.0) -> None:
    assert callable(check)
    for _ in range(int(timeout / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def _unlocked() -> LockState:
    return "unlocked"


async def test_serve_boots_answers_ipc_and_webex_then_stops() -> None:
    webex = FakeWebex()
    hello = webex.user_says("/help")
    mercury = IdleSocket([json.dumps(webex.activity_for(hello))])
    config = RelayConfig(
        webex=WebexConfig(allowed_email=webex.user_email, u2c_url=U2C),
        adapters=AdaptersConfig(opencode=False, ahp=False, cursor_cli=False),
    )
    stop = asyncio.Event()
    async with webex.client() as http:
        server = asyncio.create_task(
            daemon.serve(config, "test-token", stop, http, mercury.connect, lock_probe=_unlocked)
        )
        await _until(lambda: paths.socket_path().exists())
        reply = await asyncio.to_thread(ipc.request, {"op": "ping"}, paths.socket_path(), 2.0)
        assert reply["ok"] is True
        away_reply = await asyncio.to_thread(
            ipc.request, {"op": "away", "enabled": True}, paths.socket_path(), 2.0
        )
        assert away_reply == {"away": True}
        status = await asyncio.to_thread(ipc.request, {"op": "status"}, paths.socket_path(), 2.0)
        assert status["sessions"] == 0 and status["paused"] is False
        await _until(lambda: any("/new" in m.text for m in webex.bot_messages()))
        stop.set()
        await asyncio.wait_for(server, 5)
    assert not webex.violations
    assert not paths.socket_path().exists()


def _presence_config(webex: FakeWebex, *, auto_away_on_lock: bool = True) -> RelayConfig:
    return RelayConfig(
        webex=WebexConfig(allowed_email=webex.user_email, u2c_url=U2C),
        adapters=AdaptersConfig(opencode=False, ahp=False, cursor_cli=False),
        presence=PresenceConfig(auto_away_on_lock=auto_away_on_lock, poll_s=1),
    )


def _presence_task_alive() -> bool:
    return any(t.get_name() == "presence" and not t.done() for t in asyncio.all_tasks())


async def _away_now() -> bool:
    reply = await asyncio.to_thread(ipc.request, {"op": "away"}, paths.socket_path(), 2.0)
    return reply["away"] is True


async def test_serve_follows_the_screen_lock_and_stops_the_poller() -> None:
    webex = FakeWebex()
    screen: list[LockState] = ["unlocked"]

    async def probe() -> LockState:
        return screen[0]

    stop = asyncio.Event()
    async with webex.client() as http:
        server = asyncio.create_task(
            daemon.serve(
                _presence_config(webex), "test-token", stop, http, IdleSocket([]).connect, lock_probe=probe
            )
        )
        await _until(lambda: paths.socket_path().exists())
        assert _presence_task_alive()
        assert not await _away_now()
        screen[0] = "locked"
        for _ in range(400):
            if await _away_now():
                break
            await asyncio.sleep(0.02)
        assert await _away_now()
        screen[0] = "unlocked"
        for _ in range(400):
            if not await _away_now():
                break
            await asyncio.sleep(0.02)
        assert not await _away_now()
        stop.set()
        await asyncio.wait_for(server, 5)
    assert not _presence_task_alive()


async def test_serve_does_not_poll_when_the_feature_is_off() -> None:
    webex = FakeWebex()
    calls: list[int] = []

    async def probe() -> LockState:
        calls.append(1)
        return "locked"

    stop = asyncio.Event()
    async with webex.client() as http:
        server = asyncio.create_task(
            daemon.serve(
                _presence_config(webex, auto_away_on_lock=False),
                "test-token",
                stop,
                http,
                IdleSocket([]).connect,
                lock_probe=probe,
            )
        )
        await _until(lambda: paths.socket_path().exists())
        assert not _presence_task_alive()
        stop.set()
        await asyncio.wait_for(server, 5)
    assert calls == []
