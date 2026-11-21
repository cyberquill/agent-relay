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

import httpx
import keyring
import keyring.errors
import pytest

from agent_relay import daemon, ipc, paths
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.doctor.model import Check
from agent_relay.frontends.webex.ws_client import WebSocketLike
from agent_relay.models.config import AdaptersConfig, AttachmentsConfig, RelayConfig, WebexConfig
from agent_relay.models.events import Harness, Origin
from agent_relay.models.sessions import SessionRecord, SessionStatus
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


def _config(webex: FakeWebex) -> RelayConfig:
    return RelayConfig(
        webex=WebexConfig(allowed_email=webex.user_email, u2c_url=U2C),
        adapters=AdaptersConfig(opencode=False, ahp=False, cursor_cli=False),
    )


async def _ipc(message: dict[str, object]) -> dict[str, object]:
    return await asyncio.to_thread(ipc.request, message, paths.socket_path(), 2.0)


async def _doctor_checks() -> dict[str, Check]:
    reply = await _ipc({"op": "doctor"})
    raw = reply["checks"]
    assert isinstance(raw, list)
    return {f"{c.area}/{c.name}": c for c in (Check.model_validate(item) for item in raw)}


async def _wait_for_check(name: str, status: str, timeout: float = 5.0) -> Check:
    for _ in range(int(timeout / 0.02)):
        found = (await _doctor_checks()).get(name)
        if found is not None and found.status == status:
            return found
        await asyncio.sleep(0.02)
    raise AssertionError(f"{name} never became {status}: {(await _doctor_checks()).get(name)}")


async def test_serve_boots_answers_ipc_and_webex_then_stops() -> None:
    webex = FakeWebex()
    hello = webex.user_says("/help")
    mercury = IdleSocket([json.dumps(webex.activity_for(hello))])
    stop = asyncio.Event()
    async with webex.client() as http:
        server = asyncio.create_task(daemon.serve(_config(webex), "test-token", stop, http, mercury.connect))
        await _until(lambda: paths.socket_path().exists())
        reply = await _ipc({"op": "ping"})
        assert reply["ok"] is True
        assert await _ipc({"op": "away", "enabled": True}) == {"away": True}
        assert await _ipc({"op": "away", "enabled": False, "workspace": "/ignored"}) == {"away": False}
        status = await _ipc({"op": "status"})
        assert status["sessions"] == 0 and status["paused"] is False
        await _until(lambda: any("/new" in m.text for m in webex.bot_messages()))
        names = {t.get_name() for t in asyncio.all_tasks()}
        assert {"webex", "pending-sweeper", "housekeeper", "update-watcher"} <= names
        stop.set()
        await asyncio.wait_for(server, 5)
    assert not webex.violations
    assert not paths.socket_path().exists()


async def test_nothing_is_posted_at_the_top_level_when_relayd_starts() -> None:
    """U4/P3: no "Mac online", no lock notice, no update notice; the log and `/doctor` carry them."""
    webex = FakeWebex()
    stop = asyncio.Event()
    async with webex.client() as http:
        server = asyncio.create_task(
            daemon.serve(_config(webex), "test-token", stop, http, IdleSocket([]).connect)
        )
        await _until(lambda: paths.socket_path().exists())
        await _wait_for_check("Webex/connection", "ok")
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(server, 5)
    assert webex.bot_messages() == []


class Gate:
    """A Webex that answers 401 until `open` is set, like a bot token that was revoked and then replaced."""

    def __init__(self, webex: FakeWebex) -> None:
        self.webex = webex
        self.open = False
        self.calls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if not self.open:
            return httpx.Response(401, json={"message": "The access token expired or is invalid"})
        return self.webex.handle(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), headers={"Authorization": "Bearer test-token"}
        )


async def test_startup_401_keeps_relayd_up() -> None:
    webex = FakeWebex()
    gate = Gate(webex)
    asked: list[int] = []

    def token_source() -> str | None:
        asked.append(1)
        return "replacement-token"

    stop = asyncio.Event()
    async with gate.client() as http:
        server = asyncio.create_task(
            daemon.serve(
                _config(webex),
                "stale-token",
                stop,
                http,
                IdleSocket([]).connect,
                token_source=token_source,
                webex_backoff=(0.01, 0.05),
            )
        )
        await _until(lambda: paths.socket_path().exists())
        rejected = await _wait_for_check("Webex/connection", "fail")
        assert "rejected (401)" in rejected.detail
        assert rejected.fix == "pbpaste | relay setup token --stdin"
        assert (await _ipc({"op": "ping"}))["ok"] is True, "the IPC socket serves while Webex is down"
        assert not server.done(), "relayd must not exit on a 401"
        await _until(lambda: len(asked) >= 3)
        gate.open = True
        attached = await _wait_for_check("Webex/connection", "ok")
        assert "connected" in attached.detail
        assert not server.done()
        stop.set()
        await asyncio.wait_for(server, 5)
    assert not webex.violations


async def test_start_up_marks_sessions_left_running_as_idle() -> None:
    registry = Registry(paths.db_path())
    await registry.open()
    await registry.create_session(
        SessionRecord(
            session_key="opencode:stale",
            harness=Harness.OPENCODE,
            harness_id="stale",
            workspace="/w",
            origin=Origin.PHONE,
        )
    )
    await registry.set_session_status("opencode:stale", SessionStatus.RUNNING)
    await registry.close()
    webex = FakeWebex()
    stop = asyncio.Event()
    async with webex.client() as http:
        server = asyncio.create_task(
            daemon.serve(_config(webex), "test-token", stop, http, IdleSocket([]).connect)
        )
        await _until(lambda: paths.socket_path().exists())
        reply = await _ipc({"op": "sessions"})
        sessions = reply["sessions"]
        assert isinstance(sessions, list)
        assert [(s["session_key"], s["status"]) for s in sessions if isinstance(s, dict)] == [
            ("opencode:stale", "idle")
        ]
        stop.set()
        await asyncio.wait_for(server, 5)


def _err_log(size: int) -> Path:
    file = paths.logs_dir() / "relayd.err.log"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b"x" * size)
    return file


def test_err_log_truncated_when_over_10mb() -> None:
    big = _err_log(daemon.ERR_LOG_MAX_BYTES + 1)
    assert daemon.trim_err_log() is True
    assert big.stat().st_size == 0
    small = _err_log(daemon.ERR_LOG_MAX_BYTES)
    assert daemon.trim_err_log() is False
    assert small.stat().st_size == daemon.ERR_LOG_MAX_BYTES
    small.unlink()
    assert daemon.trim_err_log() is False, "a missing log is fine"


def test_relayd_trims_the_err_log_before_it_does_anything_else() -> None:
    big = _err_log(daemon.ERR_LOG_MAX_BYTES + 10)
    assert daemon.main() == daemon.EXIT_CONFIG
    assert big.stat().st_size == 0


async def test_send_file_goes_to_the_session_thread_and_stays_inside_the_workspace(
    registry: Registry, tmp_path: Path
) -> None:
    workspace = tmp_path / "proj"
    (workspace / "reports").mkdir(parents=True)
    (workspace / "reports" / "out.html").write_text("<p>done</p>", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside", encoding="utf-8")
    (workspace / "link.txt").symlink_to(secret)
    (workspace / "big.bin").write_bytes(b"0" * 2048)
    core = Core(RelayConfig(attachments=AttachmentsConfig(max_bytes=1024)), registry)
    await registry.create_session(
        SessionRecord(
            session_key="opencode:S1",
            harness=Harness.OPENCODE,
            harness_id="S1",
            workspace=str(workspace),
            origin=Origin.PHONE,
        )
    )
    sent: list[tuple[str, str, str, bytes]] = []
    deliver = True

    async def target(session: SessionRecord, caption: str, file: tuple[str, bytes, str]) -> bool:
        sent.append((session.session_key, caption, file[0], file[1]))
        return deliver

    core.file_targets.append(target)
    base: dict[str, object] = {"op": "send_file", "workspace": str(workspace / "reports")}

    ok = await core.handle_ipc({**base, "path": "out.html", "caption": "the report"})
    assert ok == {"delivered": True, "session": "opencode:S1", "name": "out.html"}
    assert sent == [("opencode:S1", "the report", "out.html", b"<p>done</p>")]
    named = await core.handle_ipc({**base, "path": str(workspace / "reports" / "out.html")})
    assert named["delivered"] is True and sent[-1][1] == "out.html", "the caption defaults to the name"

    for path in ("../../secret.txt", str(secret), "link.txt", "../big.bin", "missing.txt"):
        refused = await core.handle_ipc({**base, "path": path})
        assert refused["delivered"] is False and "error" in refused, path
    assert len(sent) == 2, "a refused file is never handed to a target"

    deliver = False
    offline = await core.handle_ipc({**base, "path": "out.html"})
    assert offline == {"delivered": False, "reason": daemon.NOT_CONNECTED}

    bad = await core.handle_ipc({"op": "send_file", "workspace": str(workspace)})
    assert "error" in bad
    elsewhere = await core.handle_ipc({"op": "send_file", "workspace": str(tmp_path / "other"), "path": "x"})
    assert elsewhere["delivered"] is False and "no relay session" in str(elsewhere["reason"])
    await core.router.stop()


async def test_send_file_needs_the_phone_to_be_reachable(registry: Registry, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    core = Core(RelayConfig(), registry)
    await registry.create_session(
        SessionRecord(
            session_key="cursor:D1",
            harness=Harness.CURSOR,
            harness_id="D1",
            workspace=str(tmp_path),
            origin=Origin.DESK,
        )
    )
    reply = await core.handle_ipc({"op": "send_file", "workspace": str(tmp_path), "path": "a.txt"})
    assert reply["delivered"] is False and "away mode is off" in str(reply["reason"])
    await core.router.stop()
