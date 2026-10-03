"""The guided smoke runner: relayd-side checks pass and fail correctly; human answers are recorded."""

from __future__ import annotations

from collections.abc import Iterator

from agent_relay.ipc import JsonObj
from agent_relay.models.events import Harness
from agent_relay.ops.smoke import APPS, Smoke, Waiter, render

LOCAL = next(app for app in APPS if app.harness is Harness.VSCODE_LOCAL)


class Daemon:
    """Replays a scripted sequence of `sessions` replies, one per poll."""

    def __init__(self, *frames: list[tuple[str, str]]) -> None:
        self._frames: Iterator[list[tuple[str, str]]] = iter(frames)
        self._last: list[tuple[str, str]] = []

    def __call__(self, message: JsonObj) -> JsonObj:
        assert message == {"op": "sessions"}
        self._last = next(self._frames, self._last)
        return {
            "sessions": [
                {"session_key": key, "harness": Harness.VSCODE_LOCAL.value, "status": status}
                for key, status in self._last
            ]
        }


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def smoke(daemon: Daemon, answers: list[str], timeout: float = 10.0) -> tuple[Smoke, list[str]]:
    clock = Clock()
    said: list[str] = []
    waiter = Waiter(daemon, timeout_s=timeout, poll_s=1.0, sleep=clock.sleep, clock=clock)
    replies = iter(answers)
    return Smoke(waiter, lambda prompt: next(replies), said.append), said


def test_full_pass_walks_every_step() -> None:
    old = [("local:old", "idle")]
    daemon = Daemon(
        old,
        old,
        [*old, ("local:new", "running")],
        [*old, ("local:new", "running")],
        [*old, ("local:new", "parked")],
        [*old, ("local:new", "running")],
    )
    run, said = smoke(daemon, ["y", "yes", "Y"])
    assert run.run_app(LOCAL)
    assert [(r.step, r.ok) for r in run.results] == [
        ("register", True),
        ("stream", True),
        ("approve", True),
        ("park", True),
        ("reply", True),
        ("back", True),
    ]
    assert run.results[0].evidence == "local:new"
    assert any("/away" in line for line in said) and any("/back" in line for line in said)


def test_no_session_fails_at_register_and_stops() -> None:
    run, _ = smoke(Daemon([("local:old", "idle")]), [])
    assert not run.run_app(LOCAL)
    assert [(r.step, r.ok) for r in run.results] == [("register", False)]


def test_a_no_from_the_human_stops_the_app() -> None:
    run, _ = smoke(Daemon([], [("local:new", "running")]), ["n"])
    assert not run.run_app(LOCAL)
    assert [(r.step, r.ok) for r in run.results][-1] == ("stream", False)


def test_never_parking_is_a_failure_after_the_timeout() -> None:
    run, _ = smoke(Daemon([], [("local:new", "running")]), ["y", "y"], timeout=5.0)
    assert not run.run_app(LOCAL)
    assert run.results[-1].step == "park" and not run.results[-1].ok


def test_render_is_a_scrubbed_markdown_table() -> None:
    run, _ = smoke(Daemon([], [("local:new", "running")]), ["n"])
    run.run_app(LOCAL)
    table = render(run.results, "2026-10-03 12:00 UTC")
    assert table.startswith("### Live smoke 2026-10-03 12:00 UTC\n")
    assert "| VS Code Local | register | ✅ | local:new |" in table
    assert "| VS Code Local | stream | ❌ | human: n |" in table


def test_every_app_in_the_definition_of_done_is_covered() -> None:
    assert {app.harness for app in APPS} == {
        Harness.VSCODE_LOCAL,
        Harness.VSCODE_COPILOT,
        Harness.OPENCODE,
        Harness.CURSOR,
        Harness.CURSOR_CLI,
    }
    assert all("echo relay-smoke" in app.start for app in APPS)
