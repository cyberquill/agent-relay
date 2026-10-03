"""AC-20 (live, RELAY_LIVE=1): a real `opencode serve` in `.scratch/` streams `OK`, then the session is
aborted. Uses one of the three live attempts the plan allows for OpenCode."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from agent_relay.adapters.base import StartRequest
from agent_relay.adapters.opencode.adapter import OpenCodeAdapter
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.models.config import OpenCodeConfig
from agent_relay.models.events import Event, TextDelta, TurnCompleted
from tests.conftest import live

SCRATCH = Path(__file__).resolve().parents[2] / ".scratch"
OPENCODE = Path.home() / ".opencode" / "bin" / "opencode"
MODEL = os.environ.get("RELAY_LIVE_OPENCODE_MODEL", "github-copilot/gpt-5-mini")

pytestmark = live


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def server() -> Iterator[str]:
    port = _free_port()
    SCRATCH.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "OPENCODE_SERVER_PASSWORD"}
    proc = subprocess.Popen(
        [str(OPENCODE), "serve", "--hostname", "127.0.0.1", "--port", str(port)],
        cwd=SCRATCH,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{url}/doc", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                raise RuntimeError("opencode serve exited")
            time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
async def adapter(server: str, registry: Registry) -> AsyncIterator[tuple[OpenCodeAdapter, Router, Recorder]]:
    router = Router()
    rec = Recorder()
    router.subscribe(rec)
    built = OpenCodeAdapter(OpenCodeConfig(url=server), registry, None)
    router.add_adapter(built)
    yield built, router, rec
    await router.stop()
    await built.aclose()


async def test_live_session_streams_ok_and_aborts(adapter: tuple[OpenCodeAdapter, Router, Recorder]) -> None:
    built, router, rec = adapter
    assert (await built.health())[0]
    models = {m.id for m in await built.list_models()}
    assert MODEL in models, f"{MODEL} not offered; set RELAY_LIVE_OPENCODE_MODEL"
    await asyncio.sleep(1.0)
    session = await built.start_session(
        StartRequest(workspace=str(SCRATCH), prompt="Reply with exactly: OK", model=MODEL)
    )
    for _ in range(600):
        if any(isinstance(e, TurnCompleted) for e in rec.events):
            break
        await asyncio.sleep(0.1)
    completed = [e for e in rec.events if isinstance(e, TurnCompleted)]
    assert completed and completed[0].text is not None and "OK" in completed[0].text
    assert any(isinstance(e, TextDelta) for e in rec.events)
    await built.cancel(session)
    await router.join()
