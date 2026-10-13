"""AC-23: the Cursor CDP adapter against the saved composer DOM, in a real headless Chromium over CDP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_relay.adapters.base import AdapterUnavailableError, StartRequest, UnsupportedError
from agent_relay.adapters.cursor_cdp.adapter import (
    NOT_STARTED_TEXT,
    OLDER_CHAT,
    START_DEADLINE_S,
    STOPPED_TEXT,
    CursorCdpAdapter,
)
from agent_relay.adapters.cursor_cdp.connect import Launcher, connect, fetch_version
from agent_relay.adapters.cursor_cdp.selectors import load_selectors
from agent_relay.adapters.cursor_cdp.selftest import SELFTEST_KEY, last_report, run_live
from agent_relay.core.registry import Registry
from agent_relay.models.config import CdpConfig
from agent_relay.models.events import ErrorEvent, Event, TextDelta, TurnCompleted
from agent_relay.models.sessions import SessionRecord, SessionStatus
from tests.adapters.cdp_rig import WORKSPACE_NAME, Chromium, FakeRunner, start_chromium


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def completed(self) -> list[TurnCompleted]:
        return [e for e in self.events if isinstance(e, TurnCompleted)]

    def deltas(self, session_key: str) -> str:
        return "".join(
            e.text for e in self.events if isinstance(e, TextDelta) and e.session_key == session_key
        )

    async def wait_completed(self, count: int, timeout: float = 15.0) -> list[TurnCompleted]:
        for _ in range(int(timeout * 20)):
            if len(self.completed()) >= count:
                return self.completed()
            await asyncio.sleep(0.05)
        raise AssertionError(f"expected {count} completed turn(s), saw {self.events}")


@dataclass
class Rig:
    adapter: CursorCdpAdapter
    registry: Registry
    recorder: Recorder
    runner: FakeRunner
    chromium: Chromium
    workspace: str

    async def prompts(self) -> list[dict[str, str]]:
        conn = await connect(self.chromium.port)
        try:
            value = await conn.pages()[0].evaluate("window.__prompts")
        finally:
            await conn.close()
        assert isinstance(value, list)
        return value

    async def session(self, key: str) -> SessionRecord:
        record = await self.registry.get_session(key)
        assert record is not None
        return record


@pytest.fixture
def chromium(relay_home: Path) -> Iterator[Chromium]:
    browser = start_chromium(relay_home / "chromium")
    yield browser
    browser.stop()


@pytest.fixture
async def rig(
    registry: Registry, chromium: Chromium, relay_home: Path, request: pytest.FixtureRequest
) -> AsyncIterator[Rig]:
    start_deadline: float = getattr(request, "param", START_DEADLINE_S)
    runner = FakeRunner()
    launcher = Launcher(app=relay_home / "Cursor.app", runner=runner)
    selectors = load_selectors()
    report = await run_live(registry, chromium.port, selectors, launcher)
    assert report.ok, report.render()
    adapter = CursorCdpAdapter(
        registry,
        CdpConfig(enabled=True, port=chromium.port),
        launcher=launcher,
        open_wait=1,
        start_deadline=start_deadline,
    )
    recorder = Recorder()
    task = asyncio.create_task(adapter.run(recorder))
    workspace = str(relay_home / WORKSPACE_NAME)
    yield Rig(adapter, registry, recorder, runner, chromium, workspace)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await adapter.aclose()


async def test_start_streams_the_reply_and_ends_idle(rig: Rig) -> None:
    session = await rig.adapter.start_session(
        StartRequest(workspace=rig.workspace, prompt="Reply with exactly: OK")
    )
    completed = await rig.recorder.wait_completed(1)
    assert completed[0].text == "OK"
    assert rig.recorder.deltas(session.session_key) == "OK"
    stored = await rig.session(session.session_key)
    assert stored.status is SessionStatus.IDLE
    assert stored.origin.value == "phone"
    assert [p["prompt"] for p in await rig.prompts()] == ["Reply with exactly: OK"]


async def test_health_reports_the_passing_self_test(rig: Rig) -> None:
    ok, detail = await rig.adapter.health()
    assert ok, detail
    assert "self-test passed" in detail


async def test_model_is_chosen_from_the_menu(rig: Rig) -> None:
    await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="hello", model="GPT-5"))
    await rig.recorder.wait_completed(1)
    assert (await rig.prompts())[0]["model"] == "GPT-5"


async def test_unknown_model_is_refused_and_the_menu_closed(rig: Rig) -> None:
    with pytest.raises(AdapterUnavailableError, match="does not offer the model Opus 9"):
        await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="hello", model="Opus 9"))
    assert await rig.prompts() == []
    assert [m.label for m in await rig.adapter.list_models()] == ["Auto", "GPT-5", "Sonnet 4.5"]


async def test_follow_up_prompt_runs_a_new_turn(rig: Rig) -> None:
    session = await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="first"))
    await rig.recorder.wait_completed(1)
    await rig.adapter.send_prompt(await rig.session(session.session_key), "second")
    completed = await rig.recorder.wait_completed(2)
    assert completed[1].text == "Echo: second"


async def test_a_new_chat_over_an_older_one_still_returns_its_reply(rig: Rig) -> None:
    """Cursor swaps the chat a moment after the click; the old replies must not hide the new one."""
    await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="first"))
    await rig.recorder.wait_completed(1)
    second = await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="second"))
    completed = await rig.recorder.wait_completed(2)
    assert completed[1].text == "Echo: second"
    assert rig.recorder.deltas(second.session_key) == "Echo: second"


async def test_steering_while_running_is_queued_and_sent_after_the_turn(rig: Rig) -> None:
    session = await rig.adapter.start_session(
        StartRequest(workspace=rig.workspace, prompt="Reply with exactly: OK")
    )
    with pytest.raises(UnsupportedError):
        await rig.adapter.send_prompt(await rig.session(session.session_key), "more")
    await rig.registry.enqueue(session.session_key, "steer", "more")
    completed = await rig.recorder.wait_completed(2)
    assert completed[1].text == "Echo: more"
    assert [p["prompt"] for p in await rig.prompts()] == ["Reply with exactly: OK", "more"]


async def test_stop_ends_the_turn_with_a_note(rig: Rig) -> None:
    session = await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="slow please"))
    for _ in range(100):
        if rig.recorder.deltas(session.session_key):
            break
        await asyncio.sleep(0.05)
    await rig.adapter.cancel(await rig.session(session.session_key))
    completed = await rig.recorder.wait_completed(1)
    assert completed[0].text is not None and completed[0].text.endswith(STOPPED_TEXT)
    assert completed[0].text.startswith("t0")
    with pytest.raises(AdapterUnavailableError, match="not running"):
        await rig.adapter.cancel(await rig.session(session.session_key))


async def test_an_older_chat_is_continued_at_the_desk(rig: Rig) -> None:
    first = await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="one"))
    await rig.recorder.wait_completed(1)
    await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="two"))
    await rig.recorder.wait_completed(2)
    with pytest.raises(AdapterUnavailableError, match="continue this one at the desk"):
        await rig.adapter.send_prompt(await rig.session(first.session_key), "three")
    assert OLDER_CHAT.endswith("at the desk.")


async def test_a_busy_composer_refuses_a_second_chat(rig: Rig) -> None:
    await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="slow please"))
    with pytest.raises(AdapterUnavailableError, match="busy"):
        await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="other"))
    with pytest.raises(AdapterUnavailableError, match="busy"):
        await rig.adapter.list_models()


async def test_a_silent_reply_still_completes(rig: Rig) -> None:
    await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="silent"))
    completed = await rig.recorder.wait_completed(1)
    assert completed[0].text is None


@pytest.mark.parametrize("rig", [0.6], indirect=True)
async def test_a_send_that_never_starts_frees_the_composer_for_the_next_prompt(rig: Rig) -> None:
    session = await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="deadsend"))
    for _ in range(100):
        if any(isinstance(e, ErrorEvent) for e in rig.recorder.events):
            break
        await asyncio.sleep(0.05)
    errors = [e for e in rig.recorder.events if isinstance(e, ErrorEvent)]
    assert [e.message for e in errors] == [NOT_STARTED_TEXT]
    assert (await rig.session(session.session_key)).status is SessionStatus.IDLE
    await rig.adapter.send_prompt(await rig.session(session.session_key), "second")
    completed = await rig.recorder.wait_completed(1)
    assert completed[0].text == "Echo: second"
    assert [p["prompt"] for p in await rig.prompts()] == ["second"]


@pytest.mark.parametrize("rig", [0.6], indirect=True)
async def test_a_turn_that_started_is_left_alone_past_the_deadline(rig: Rig) -> None:
    await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="slow please"))
    await asyncio.sleep(1.2)
    assert not [e for e in rig.recorder.events if isinstance(e, ErrorEvent)]
    assert rig.recorder.deltas(next(e.session_key for e in rig.recorder.events if isinstance(e, TextDelta)))


async def test_another_workspace_is_opened_and_waited_for(rig: Rig, relay_home: Path) -> None:
    other = str(relay_home / "elsewhere")
    with pytest.raises(AdapterUnavailableError, match="No Cursor window"):
        await rig.adapter.start_session(StartRequest(workspace=other, prompt="hi"))
    assert ["open", "-a", str(relay_home / "Cursor.app"), other] in rig.runner.calls


async def test_a_new_cursor_version_disables_the_adapter(rig: Rig) -> None:
    report = await last_report(rig.registry)
    assert report is not None
    await rig.registry.kv_set(SELFTEST_KEY, report.model_copy(update={"version": "0.9.0"}).model_dump_json())
    ok, detail = await rig.adapter.health()
    assert not ok and "changed from 0.9.0" in detail
    with pytest.raises(AdapterUnavailableError, match="until `relay selftest cursor-cdp` passes"):
        await rig.adapter.start_session(StartRequest(workspace=rig.workspace, prompt="hi"))


async def test_without_a_self_test_the_adapter_refuses(
    registry: Registry, chromium: Chromium, relay_home: Path
) -> None:
    launcher = Launcher(app=relay_home / "Cursor.app", runner=FakeRunner())
    adapter = CursorCdpAdapter(registry, CdpConfig(enabled=True, port=chromium.port), launcher=launcher)
    ok, detail = await adapter.health()
    assert not ok and "no self-test on record" in detail
    with pytest.raises(AdapterUnavailableError, match="no self-test on record"):
        await adapter.start_session(StartRequest(workspace=str(relay_home / WORKSPACE_NAME), prompt="hi"))
    await adapter.aclose()


async def test_self_test_names_a_missing_selector(
    registry: Registry, chromium: Chromium, relay_home: Path
) -> None:
    conn = await connect(chromium.port)
    try:
        await conn.pages()[0].evaluate("document.getElementById('new-chat').remove()")
    finally:
        await conn.close()
    launcher = Launcher(app=relay_home / "Cursor.app", runner=FakeRunner())
    report = await run_live(registry, chromium.port, load_selectors(), launcher)
    assert not report.ok
    assert report.failures() == ["new_chat"]
    assert "MISSING new_chat" in report.render()
    adapter = CursorCdpAdapter(registry, CdpConfig(enabled=True, port=chromium.port), launcher=launcher)
    ok, detail = await adapter.health()
    assert not ok and "failed (new_chat)" in detail
    await adapter.aclose()


async def test_version_is_read_from_the_debug_port(chromium: Chromium) -> None:
    version = await fetch_version(chromium.port)
    assert version is not None and version.startswith(("Chrome/", "HeadlessChrome/"))
