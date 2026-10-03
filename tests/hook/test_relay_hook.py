"""AC-25: relay-hook is fast when away mode is off or relayd is down, and always fails open."""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_relay import paths
from agent_relay.hook import relay_hook
from agent_relay.ipc import IpcServer

SCRIPT = Path(relay_hook.__file__).resolve()


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[18]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Path]:
    sock = paths.socket_path()
    seen: list[dict[str, object]] = []

    async def handler(message: dict[str, object]) -> dict[str, object]:
        seen.append(message)
        if message.get("event") == "PreToolUse":
            return {"stdout": {"permissionDecision": "deny"}, "exit": 0}
        if message.get("event") == "Stop":
            return {"stdout": "plain", "exit": 2}
        return {}

    loop = asyncio.new_event_loop()
    srv = IpcServer(sock, handler, expected_uid=os.getuid())
    started = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(srv.start())
        started.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    started.wait(5)
    yield sock
    asyncio.run_coroutine_threadsafe(srv.stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def test_reply_mapping(server: Path) -> None:
    exit_code, out = relay_hook.run(["--harness", "copilot", "--event", "PreToolUse"], "{}", server)
    assert exit_code == 0 and json.loads(out) == {"permissionDecision": "deny"}
    assert relay_hook.run(["--harness", "copilot", "--event", "Stop"], "{}", server) == (2, "plain")
    assert relay_hook.run(["--harness", "cursor", "--event", "stop"], "{}", server) == (0, "")


def test_fails_open_on_bad_input_and_missing_daemon(tmp_path: Path) -> None:
    missing = tmp_path / "none.sock"
    assert relay_hook.run(["--harness", "cursor", "--event", "stop"], "{}", missing) == (0, "")
    assert relay_hook.run(["--harness", "cursor", "--event", "stop"], "not json", missing) == (0, "")
    assert relay_hook.run([], "{}", missing) == (0, "")


def test_p95_in_process_under_budget(server: Path, tmp_path: Path) -> None:
    observe = [
        _timed(lambda: relay_hook.run(["--harness", "cursor", "--event", "afterAgentResponse"], "{}", server))
        for _ in range(60)
    ]
    down = [
        _timed(lambda: relay_hook.run(["--harness", "cursor", "--event", "stop"], "{}", tmp_path / "x.sock"))
        for _ in range(60)
    ]
    assert _p95(observe) < 0.150
    assert _p95(down) < 0.080


def _timed(fn: object) -> float:
    assert callable(fn)
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def test_p95_as_subprocess_when_daemon_down(tmp_path: Path) -> None:
    """The real cost: interpreter start-up with -S -E plus the failed connect."""
    env = {"RELAY_HOME": os.environ["RELAY_HOME"], "PATH": os.environ.get("PATH", "")}
    samples: list[float] = []
    for _ in range(25):
        start = time.perf_counter()
        done = subprocess.run(
            [sys.executable, "-S", "-E", str(SCRIPT), "--harness", "cursor", "--event", "stop"],
            input=b"{}",
            capture_output=True,
            env=env,
            check=False,
        )
        samples.append(time.perf_counter() - start)
        assert done.returncode == 0 and done.stdout == b""
    assert _p95(samples) < 0.080


def test_subprocess_round_trip_through_daemon(server: Path) -> None:
    env = {"RELAY_HOME": os.environ["RELAY_HOME"], "PATH": os.environ.get("PATH", "")}
    samples: list[float] = []
    for _ in range(25):
        start = time.perf_counter()
        done = subprocess.run(
            [sys.executable, "-S", "-E", str(SCRIPT), "--harness", "copilot", "--event", "PostToolUse"],
            input=b'{"session_id": "s"}',
            capture_output=True,
            env=env,
            check=False,
        )
        samples.append(time.perf_counter() - start)
        assert done.returncode == 0
    assert _p95(samples) < 0.150
