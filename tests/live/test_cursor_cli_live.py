"""AC-22 (live, RELAY_LIVE=1): a real `cursor-agent acp` in `.scratch/` answers `OK`.

Needs `cursor-agent login` first. Uses one of the three live attempts the plan allows for Cursor.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_relay.adapters.base import StartRequest
from agent_relay.adapters.cursor_cli.adapter import CursorCliAdapter
from agent_relay.adapters.cursor_cli.catalog import CursorCli
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.models.events import Event, TurnCompleted
from tests.conftest import live

SCRATCH = Path(__file__).resolve().parents[2] / ".scratch"
CURSOR_AGENT = Path.home() / ".local" / "bin" / "cursor-agent"

pytestmark = live


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
async def adapter(registry: Registry) -> AsyncIterator[tuple[CursorCliAdapter, Router, Recorder]]:
    router = Router()
    rec = Recorder()
    router.subscribe(rec)
    built = CursorCliAdapter(registry, CursorCli(CURSOR_AGENT))
    router.add_adapter(built)
    yield built, router, rec
    await router.stop()
    await built.aclose()


async def test_live_cursor_cli_answers_ok(adapter: tuple[CursorCliAdapter, Router, Recorder]) -> None:
    built, router, rec = adapter
    ok, detail = await built.health()
    assert ok, detail
    SCRATCH.mkdir(exist_ok=True)
    await built.start_session(StartRequest(workspace=str(SCRATCH), prompt="Reply with exactly: OK"))
    for _ in range(1200):
        if any(isinstance(e, TurnCompleted) for e in rec.events):
            break
        await asyncio.sleep(0.1)
    completed = [e for e in rec.events if isinstance(e, TurnCompleted)]
    assert completed and completed[0].text is not None and "OK" in completed[0].text
    await router.join()
