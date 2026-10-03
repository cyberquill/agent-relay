"""AC-34: relay-mcp over stdio with a real MCP client, against a real relayd core on the Unix socket."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.ipc import IpcServer
from agent_relay.mcp import server as relay_mcp
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import Event, Harness, Origin, QuestionAsked
from agent_relay.models.sessions import SessionRecord, SessionStatus

REPO = Path(__file__).resolve().parents[2]


@dataclass
class Relayd:
    core: Core
    registry: Registry
    workspace: Path
    notes: list[tuple[str, str]] = field(default_factory=list)
    asked: list[QuestionAsked] = field(default_factory=list)

    async def handle(self, event: Event) -> None:
        if isinstance(event, QuestionAsked):
            self.asked.append(event)

    async def session(
        self, origin: Origin = Origin.PHONE, status: SessionStatus = SessionStatus.RUNNING
    ) -> SessionRecord:
        return await self.registry.upsert_session(
            SessionRecord(
                session_key=f"cursor:{origin.value}-{status.value}",
                harness=Harness.CURSOR,
                harness_id=f"{origin.value}-{status.value}",
                workspace=str(self.workspace),
                origin=origin,
                status=status,
            )
        )

    async def answer_next(self, answer: str | None, by: str = "phone") -> None:
        for _ in range(200):
            pending = [p for p in await self.registry.open_pending() if p.kind == "question"]
            if pending:
                assert await self.core.service.answer_question(pending[0].request_id, answer, by=by)  # type: ignore[arg-type]
                return
            await asyncio.sleep(0.05)
        raise AssertionError("no question was posted")


@pytest.fixture
async def relayd(registry: Registry, relay_home: Path) -> AsyncIterator[Relayd]:
    workspace = relay_home / "ws"
    (workspace / "sub").mkdir(parents=True)
    core = Core(RelayConfig(), registry)
    state = Relayd(core, registry, workspace)
    core.router.subscribe(state)

    async def capture(session: SessionRecord, text: str) -> None:
        state.notes.append((session.session_key, text))

    core.notify_targets.append(capture)
    server = IpcServer(paths.socket_path(), core.handle_ipc, expected_uid=os.getuid())
    await server.start()
    yield state
    await server.stop()
    await core.router.stop()


@asynccontextmanager
async def client(relay_home: Path, workspace: Path) -> AsyncIterator[ClientSession]:
    env = {**os.environ, "RELAY_HOME": str(relay_home), "RELAY_WORKSPACE": str(workspace)}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agent_relay.mcp.server"], env=env, cwd=REPO
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


def structured(result: CallToolResult) -> dict[str, object]:
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return dict(result.structuredContent)


async def test_tools_are_listed_with_their_schemas(relayd: Relayd, relay_home: Path) -> None:
    async with client(relay_home, relayd.workspace) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
    assert set(tools) == {"notify_phone", "ask_phone"}
    assert set(tools["ask_phone"].inputSchema["properties"]) == {"question", "options", "timeout_s"}
    assert tools["ask_phone"].inputSchema["required"] == ["question"]


async def test_notify_reaches_the_phone_session_of_the_workspace(relayd: Relayd, relay_home: Path) -> None:
    session = await relayd.session()
    async with client(relay_home, relayd.workspace / "sub") as mcp:
        result = structured(await mcp.call_tool("notify_phone", {"text": "tests are green"}))
    assert result == {"delivered": True, "reason": ""}
    assert relayd.notes == [(session.session_key, "tests are green")]


async def test_notify_without_a_session_is_not_delivered(relayd: Relayd, relay_home: Path) -> None:
    async with client(relay_home, relayd.workspace) as mcp:
        result = structured(await mcp.call_tool("notify_phone", {"text": "hello"}))
    assert result == {"delivered": False, "reason": "no relay session is live for this workspace"}
    assert relayd.notes == []


async def test_ask_returns_the_phone_answer(relayd: Relayd, relay_home: Path) -> None:
    await relayd.session()
    async with client(relay_home, relayd.workspace) as mcp:
        call = asyncio.create_task(
            mcp.call_tool(
                "ask_phone", {"question": "Which DB?", "options": ["sqlite", "postgres"], "timeout_s": 60}
            )
        )
        await relayd.answer_next("postgres")
        result = structured(await call)
    assert result == {"outcome": "answered", "answer": "postgres", "reason": ""}
    assert relayd.asked[0].question == "Which DB?"
    assert relayd.asked[0].options == ("sqlite", "postgres")


async def test_ask_times_out(relayd: Relayd, relay_home: Path) -> None:
    await relayd.session()
    async with client(relay_home, relayd.workspace) as mcp:
        result = structured(await mcp.call_tool("ask_phone", {"question": "Still there?", "timeout_s": 1}))
    assert result["outcome"] == "timeout"


async def test_a_skipped_question_is_denied(relayd: Relayd, relay_home: Path) -> None:
    await relayd.session()
    async with client(relay_home, relayd.workspace) as mcp:
        call = asyncio.create_task(mcp.call_tool("ask_phone", {"question": "Go?", "timeout_s": 60}))
        await relayd.answer_next(None, by="panic")
        result = structured(await call)
    assert result == {"outcome": "denied", "answer": None, "reason": "skipped (panic)"}


async def test_desk_sessions_need_away_mode(relayd: Relayd, relay_home: Path) -> None:
    await relayd.session(origin=Origin.DESK)
    async with client(relay_home, relayd.workspace) as mcp:
        denied = structured(await mcp.call_tool("ask_phone", {"question": "Go?", "timeout_s": 5}))
        assert denied == {
            "outcome": "denied",
            "answer": None,
            "reason": "away mode is off for this workspace",
        }
        await away.set_away(relayd.registry, True, str(relayd.workspace))
        delivered = structured(await mcp.call_tool("notify_phone", {"text": "on it"}))
    assert delivered["delivered"] is True


async def test_a_paused_relay_denies(relayd: Relayd, relay_home: Path) -> None:
    await relayd.session()
    paths.pause_flag().parent.mkdir(parents=True, exist_ok=True)
    paths.pause_flag().touch()
    async with client(relay_home, relayd.workspace) as mcp:
        result = structured(await mcp.call_tool("ask_phone", {"question": "Go?", "timeout_s": 5}))
    assert result["outcome"] == "denied" and "paused" in str(result["reason"])


async def test_without_relayd_the_tools_degrade(relay_home: Path) -> None:
    async with client(relay_home, relay_home) as mcp:
        asked = structured(await mcp.call_tool("ask_phone", {"question": "Go?", "timeout_s": 5}))
        told = structured(await mcp.call_tool("notify_phone", {"text": "hi"}))
    assert asked["outcome"] == "denied" and "relayd is not reachable" in str(asked["reason"])
    assert told["delivered"] is False and "relay start" in str(told["reason"])


class Progress:
    def __init__(self) -> None:
        self.reports: list[tuple[float, float | None, str | None]] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.reports.append((progress, total, message))


async def test_a_long_wait_reports_progress(relayd: Relayd, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay_mcp, "PROGRESS_EVERY_S", 0.1)
    await relayd.session()
    progress = Progress()
    call = asyncio.create_task(
        relay_mcp.ask("q", [], 30, paths.socket_path(), str(relayd.workspace), progress)
    )
    await asyncio.sleep(0.45)
    await relayd.answer_next("y")
    assert (await call).answer == "y"
    assert len(progress.reports) >= 3
    assert progress.reports[0] == (0.1, 30.0, "waiting for the phone")


async def test_busy_sessions_win_over_idle_ones(relayd: Relayd, relay_home: Path) -> None:
    await relayd.session(status=SessionStatus.IDLE)
    busy = await relayd.session(status=SessionStatus.PARKED)
    await relayd.session(status=SessionStatus.ENDED)
    async with client(relay_home, relayd.workspace) as mcp:
        structured(await mcp.call_tool("notify_phone", {"text": "x"}))
    assert relayd.notes == [(busy.session_key, "x")]
