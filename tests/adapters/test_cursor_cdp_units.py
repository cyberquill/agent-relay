"""Cursor CDP pieces without a browser: turn tracking, versions, the launcher and selector loading."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from agent_relay.adapters.base import AdapterUnavailableError
from agent_relay.adapters.cursor_cdp.connect import NOT_DEBUGGABLE, Launcher, cursor_version
from agent_relay.adapters.cursor_cdp.driver import Snapshot, TurnTracker
from agent_relay.adapters.cursor_cdp.selectors import SELECTORS_FILE, load_selectors
from agent_relay.adapters.cursor_cdp.selftest import SelftestReport, gate
from tests.adapters.cdp_rig import FakeRunner


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def finished(tracker: TurnTracker) -> bool:
    """Read through a call so the type checker does not pin `done` after the first assert."""
    return tracker.done


def test_tracker_ignores_the_previous_reply_and_streams_the_new_one() -> None:
    tracker = TurnTracker(Snapshot(count=1, text="old answer", busy=False))
    assert tracker.feed(Snapshot(1, "old answer", True)) == ""
    assert tracker.feed(Snapshot(2, "He", True)) == "He"
    assert tracker.feed(Snapshot(2, "Hello", True)) == "llo"
    assert not finished(tracker)
    assert tracker.feed(Snapshot(2, "Hello", False)) == ""
    assert finished(tracker) and tracker.text == "Hello"
    assert tracker.feed(Snapshot(3, "late", True)) == ""


def test_tracker_does_not_repeat_text_when_the_reply_is_rewritten() -> None:
    tracker = TurnTracker(Snapshot(0, "", False))
    assert tracker.feed(Snapshot(1, "draft", True)) == "draft"
    assert tracker.feed(Snapshot(1, "final", True)) == ""
    assert tracker.feed(Snapshot(1, "final answer", True)) == " answer"
    tracker.feed(Snapshot(1, "final answer", False))
    assert tracker.text == "final answer"


def test_tracker_waits_while_nothing_has_happened_yet() -> None:
    tracker = TurnTracker(Snapshot(0, "", False))
    tracker.feed(Snapshot(0, "", False))
    assert not finished(tracker)
    tracker.feed(Snapshot(1, "", True))
    tracker.feed(Snapshot(1, "", False))
    assert finished(tracker) and tracker.text == ""


def test_snapshot_parse_is_tolerant() -> None:
    assert Snapshot.parse(None) == Snapshot(0, "", False)
    assert Snapshot.parse({"count": "2", "text": 3, "busy": 1}) == Snapshot(0, "", True)


def test_cursor_version_prefers_the_user_agent() -> None:
    agent = (
        "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Cursor/1.7.38 Chrome/132.0 Electron/34.5.8 Safari/537.36"
    )
    assert cursor_version({"User-Agent": agent, "Browser": "Chrome/132.0"}) == "1.7.38"
    assert cursor_version({"Browser": "Chrome/132.0"}) == "Chrome/132.0"
    assert cursor_version({}) == "unknown"


def test_launcher_binds_the_debug_port_to_loopback(tmp_path: Path) -> None:
    argv = Launcher(app=tmp_path / "Cursor.app", runner=FakeRunner()).argv(9223)
    assert argv[-2:] == ["--remote-debugging-port=9223", "--remote-debugging-address=127.0.0.1"]


async def test_launcher_refuses_to_restart_a_running_cursor(tmp_path: Path) -> None:
    runner = FakeRunner(running=True)
    with pytest.raises(AdapterUnavailableError) as raised:
        await Launcher(app=tmp_path / "Cursor.app", runner=runner).ensure(_unused_port())
    assert str(raised.value) == NOT_DEBUGGABLE
    assert all(call[0] != "open" for call in runner.calls)


async def test_launcher_reports_a_missing_app(tmp_path: Path) -> None:
    with pytest.raises(AdapterUnavailableError, match="not installed"):
        await Launcher(app=tmp_path / "Cursor.app", runner=FakeRunner()).ensure(_unused_port())


async def test_launcher_starts_cursor_and_gives_up_when_the_port_never_opens(tmp_path: Path) -> None:
    app = tmp_path / "Cursor.app"
    app.mkdir()
    runner = FakeRunner()
    port = _unused_port()
    with pytest.raises(AdapterUnavailableError, match="did not open its debug port"):
        await Launcher(app=app, runner=runner).ensure(port, wait_s=0.6)
    assert runner.calls[-1] == Launcher(app=app).argv(port)


def test_shipped_selectors_load_and_mark_the_essentials_required() -> None:
    selectors = load_selectors()
    assert set(selectors.required()) == {"composer", "send", "new_chat", "model_picker", "messages"}


def test_selectors_missing_a_name_are_rejected(tmp_path: Path) -> None:
    data = json.loads(SELECTORS_FILE.read_text())
    del data["selectors"]["stop"]
    broken = tmp_path / "selectors.json"
    broken.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="missing selectors: stop"):
        load_selectors(broken)


def test_gate_requires_matching_selector_version() -> None:
    selectors = load_selectors()
    report = SelftestReport(version="1.0", selectors_version=selectors.version + 1, checks=())
    assert (
        gate(report, "1.0", selectors)
        == "The selectors changed since the last self-test: run `relay selftest cursor-cdp`."
    )
    passing = report.model_copy(update={"selectors_version": selectors.version})
    assert gate(passing, "1.0", selectors) is None
