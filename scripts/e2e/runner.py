"""The scenario table, the ordering rules and the run loop that turns outcomes into a `Report`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from scripts.e2e import scenarios, scenarios_more
from scripts.e2e.env import Env
from scripts.e2e.model import DriverError, ScenarioFailed, ScenarioSkipped, SubmitRefused
from scripts.e2e.report import Report, Result, Status

type Body = Callable[[Env, str], Awaitable[str]]

NO_SCOPE = "no scope: submit the card on the phone"
BASE = 2  # the scenario that creates the session the later ones talk to
FIRST_PER_APP = 2
NEEDS_SESSION = frozenset({3, 4, 5, 6, 7, 10})  # the scenarios that talk to the session 2 creates


@dataclass(frozen=True)
class Scenario:
    number: int
    title: str
    run: Body
    per_app: bool = True
    typed: Body | None = None  # the variant used when the owner's token cannot submit cards


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(1, "/help replies, nothing else", scenarios.help_replies, per_app=False),
    Scenario(
        2,
        "/new card → card deleted, root, one final OK",
        scenarios.new_session,
        typed=scenarios.new_session_typed,
    ),
    Scenario(3, "thread reply → one final message", scenarios.thread_reply),
    Scenario(4, "PNG and PDF reply → the agent names them", scenarios.file_reply),
    Scenario(5, "long table → one message with answer.html", scenarios.html_answer),
    Scenario(
        6,
        "/model card → Model → X, next turn uses it",
        scenarios_more.model_switch,
        typed=scenarios_more.model_switch_typed,
    ),
    Scenario(7, "shell command → no approval card, it ran", scenarios.shell_without_approval),
    Scenario(10, "/stop a running turn → stopped, the chat still answers", scenarios_more.stop_turn),
    Scenario(8, "/away → desk turn → reply continues → /back", scenarios_more.away_round_trip, per_app=False),
    Scenario(
        9,
        "/panic → confirm → stopped, /away resumes",
        scenarios_more.panic,
        per_app=False,
        typed=scenarios_more.panic_typed,
    ),
)
NUMBERS = tuple(s.number for s in SCENARIOS)


def plan(only: set[int] | None) -> list[Scenario]:
    """The scenarios to run, in order. Choosing 3 to 7 or 10 also runs 2: they need its session."""
    chosen = set(NUMBERS) if not only else set(only)
    if chosen & NEEDS_SESSION:
        chosen.add(BASE)
    return [s for s in SCENARIOS if s.number in chosen]


def needs_apps(selected: list[Scenario]) -> bool:
    return any(s.per_app for s in selected)


def _result(
    label: str, scenario: Scenario, app: str, status: Status, detail: str, started: float, env: Env
) -> Result:
    return Result(label, scenario.title, app, status, detail, env.clock.now() - started)


async def _attempt(env: Env, body: Body, app: str) -> tuple[Status, str]:
    """Run one scenario body; never raises (except cancellation)."""
    try:
        async with asyncio.timeout(env.settings.scenario_max_s):
            return "PASS", await body(env, app)
    except ScenarioSkipped as exc:
        return "SKIP", env.redact(str(exc))
    except ScenarioFailed as exc:
        return "FAIL", env.redact(str(exc))
    except SubmitRefused:
        raise
    except TimeoutError:
        return "FAIL", f"the scenario ran past {env.settings.scenario_max_s:.0f}s"
    except DriverError as exc:
        return "FAIL", env.redact(str(exc))
    except Exception as exc:
        return "FAIL", env.redact(f"{type(exc).__name__}: {exc}")


async def _run_one(env: Env, scenario: Scenario, app: str, report: Report) -> Status:
    label = str(scenario.number)
    shown = app if scenario.per_app else "-"
    use_typed = scenario.typed is not None and not env.can_submit
    if use_typed:
        report.add(Result(label, scenario.title, shown, "SKIP", NO_SCOPE, 0.0))
        label = f"{label}b"
    body = scenario.typed if use_typed and scenario.typed else scenario.run
    started = env.clock.now()
    try:
        status, detail = await _attempt(env, body, app)
    except SubmitRefused as exc:
        env.can_submit = False
        report.add(_result(label, scenario, shown, "SKIP", f"{NO_SCOPE} ({exc.status})", started, env))
        if scenario.typed is None:
            return "SKIP"
        label = f"{scenario.number}b"
        started = env.clock.now()
        status, detail = await _attempt(env, scenario.typed, app)
    report.add(_result(label, scenario, shown, status, detail, started, env))
    return status


async def run(env: Env, selected: list[Scenario], apps: list[str], report: Report) -> Report:
    """Scenario 1, then 2 to 7 and 10 once per app, then 8 and 9. Failure of 2 skips the rest for that app."""
    for scenario in (s for s in selected if not s.per_app and s.number < FIRST_PER_APP):
        await _run_one(env, scenario, "-", report)
    per_app = [s for s in selected if s.per_app]
    for app in apps:
        base_ok = True
        for scenario in per_app:
            if scenario.number != BASE and not base_ok:
                skipped = Result(
                    str(scenario.number), scenario.title, app, "SKIP", f"no session: {BASE} failed", 0.0
                )
                report.add(skipped)
                continue
            status = await _run_one(env, scenario, app, report)
            if scenario.number == BASE and status != "PASS":
                base_ok = False
    for scenario in (s for s in selected if not s.per_app and s.number > FIRST_PER_APP):
        await _run_one(env, scenario, "-", report)
    return report


async def cleanup(env: Env, report: Report) -> None:
    """Delete every message the driver posted; relay threads are left alone."""
    failed = 0
    for message_id in reversed(env.posted):
        try:
            await env.user.delete(message_id)
        except Exception:
            failed += 1
    report.notes.append(f"cleanup: deleted {len(env.posted) - failed} of {len(env.posted)} messages posted")
