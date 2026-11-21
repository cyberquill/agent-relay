"""UR-8: the phone E2E driver. Every scenario passes against a well-behaved fake relay and fails against
one that breaks the matching rule; the session cap, the report and the scope fallback are checked too."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from scripts.e2e.env import SESSION_CAP, Env, SessionBudget, Settings, Thread
from scripts.e2e.model import BudgetExceeded, Msg, Person, Redactor
from scripts.e2e.report import Report, Result
from scripts.e2e.runner import NO_SCOPE, cleanup, needs_apps, plan, run
from tests.e2e_world import BOT, ME, ROOM, Behaviour, World

APP = "vscode_copilot"


def make_env(world: World, tmp_path: Path, said: list[str] | None = None) -> Env:
    lines = said if said is not None else []
    return Env(
        user=world,
        clock=world.clock,
        room_id=ROOM,
        bot_email=BOT,
        me=Person(id="me", email=ME),
        settings=Settings(),
        budget=SessionBudget(),
        redact=Redactor(),
        say=lines.append,
        tmp_dir=tmp_path,
        can_submit=world.b.submit_scope,
    )


async def drive(
    world: World, tmp_path: Path, only: set[int] | None = None, apps: list[str] | None = None
) -> tuple[Report, Env]:
    env = make_env(world, tmp_path)
    report = Report(started="test")
    await run(env, plan(only), [APP] if apps is None else apps, report)
    await cleanup(env, report)
    return report, env


def broken(flag: str) -> Behaviour:
    behaviour = Behaviour()
    setattr(behaviour, flag, True)
    return behaviour


def row(report: Report, label: str, app: str | None = None) -> Result:
    found = [r for r in report.results if r.label == label and (app is None or r.app == app)]
    assert len(found) == 1, f"{label}: {[(r.label, r.app) for r in report.results]}"
    return found[0]


def table(report: Report) -> dict[str, str]:
    return {r.label: f"{r.status} {r.detail}" for r in report.results}


async def test_every_scenario_passes_against_a_well_behaved_relay(tmp_path: Path) -> None:
    world = World()
    report, env = await drive(world, tmp_path)
    assert [r.label for r in report.results] == ["1", "2", "3", "4", "5", "6", "7", "8", "9"], table(report)
    assert [r.status for r in report.results] == ["PASS"] * 9, table(report)
    assert report.exit_code() == 0
    assert "Model → gpt-5" in row(report, "6").detail
    assert "answer.html" in row(report, "5").detail
    assert env.budget.used == {APP: 1}


async def test_the_driver_deletes_every_message_it_posted_and_leaves_the_relay_alone(tmp_path: Path) -> None:
    world = World()
    report, env = await drive(world, tmp_path)
    mine = {mid for mid, raw in world.raw.items() if raw.person_email == ME}
    assert mine and mine == set(env.posted)
    assert mine <= world.deleted
    bots = {mid for mid, raw in world.raw.items() if raw.person_email == BOT}
    assert bots & world.deleted <= set(world.cards)  # only cards: the relay deletes those itself
    assert report.notes[-1] == f"cleanup: deleted {len(mine)} of {len(mine)} messages posted"


async def test_each_app_gets_its_own_session_and_one_charge(tmp_path: Path) -> None:
    world = World()
    report, env = await drive(world, tmp_path, only={2, 3}, apps=[APP, "cursor_cli"])
    assert [(r.label, r.app, r.status) for r in report.results] == [
        ("2", APP, "PASS"),
        ("3", APP, "PASS"),
        ("2", "cursor_cli", "PASS"),
        ("3", "cursor_cli", "PASS"),
    ]
    assert env.budget.used == {APP: 1, "cursor_cli": 1}


async def test_choosing_a_later_scenario_also_runs_the_session_it_needs(tmp_path: Path) -> None:
    assert [s.number for s in plan({4})] == [2, 4]
    assert [s.number for s in plan({1, 9})] == [1, 9]
    assert [s.number for s in plan(None)] == list(range(1, 10))
    assert needs_apps(plan({3})) and not needs_apps(plan({1, 8, 9}))
    report, _ = await drive(World(), tmp_path, only={4})
    assert [(r.label, r.status) for r in report.results] == [("2", "PASS"), ("4", "PASS")]


MISBEHAVIOURS = [
    ("help_twice", {1}, "1", "2 messages after /help"),
    ("extra_final", {2}, "2", "2 messages in the thread"),
    ("chatter_top", {2}, "2", "unexpected top-level"),
    ("card_kept", {2}, "2", "was not deleted"),
    ("no_root", {2}, "2", "waiting for the thread root"),
    ("approval_card", {7}, "7", "approval card"),
    ("script_in_html", {5}, "5", "contains <script"),
    ("href_in_html", {5}, "5", "contains href="),
    ("no_csp", {5}, "5", "no Content-Security-Policy"),
    ("no_html_file", {5}, "5", "without a file"),
    ("ignore_files", {4}, "4", "did not name"),
    ("wrong_model_reply", {6}, "6", "another model"),
    ("back_keeps_parked", {8}, "8", "desk chat released"),
]


@pytest.mark.parametrize(("flag", "only", "label", "expected"), MISBEHAVIOURS)
async def test_a_misbehaving_relay_fails_the_matching_scenario(
    tmp_path: Path, flag: str, only: set[int], label: str, expected: str
) -> None:
    report, _ = await drive(World(broken(flag)), tmp_path, only)
    result = row(report, label)
    assert result.status == "FAIL", table(report)
    assert expected in result.detail
    assert report.exit_code() == 1


async def test_a_failed_turn_is_reported_with_its_text(tmp_path: Path) -> None:
    report, _ = await drive(World(replace(Behaviour(), final_text="⚠️ boom")), tmp_path, {2})
    assert row(report, "2").status == "FAIL" and "the turn failed: ⚠️ boom" in row(report, "2").detail


async def test_a_reply_that_is_not_ok_fails_the_thread_scenario(tmp_path: Path) -> None:
    world = World()
    env = make_env(world, tmp_path)
    report = Report(started="test")
    await run(env, [s for s in plan({2}) if s.number == 2], [APP], report)
    world.b.final_text = "I did it"
    await run(env, [s for s in plan({3}) if s.number == 3], [APP], report)
    assert (row(report, "2").status, row(report, "3").status) == ("PASS", "FAIL")
    assert "the reply is not OK" in row(report, "3").detail


async def test_a_failed_session_start_skips_the_scenarios_that_need_it(tmp_path: Path) -> None:
    report, _ = await drive(World(replace(Behaviour(), no_root=True)), tmp_path, {2, 3, 4, 5, 6, 7})
    assert row(report, "2").status == "FAIL"
    for label in "34567":
        skipped = row(report, label)
        assert skipped.status == "SKIP" and "no session: 2 failed" in skipped.detail
    assert report.exit_code() == 1


async def test_a_card_scenario_skips_when_the_app_has_nothing_to_choose(tmp_path: Path) -> None:
    report, _ = await drive(World(replace(Behaviour(), single_model=True)), tmp_path, {6})
    assert row(report, "6").status == "SKIP" and "no model other" in row(report, "6").detail
    report, _ = await drive(World(replace(Behaviour(), no_model_card=True)), tmp_path, {6})
    assert row(report, "6").status == "SKIP" and "no model selection" in row(report, "6").detail
    assert report.exit_code() == 0


async def test_away_is_skipped_when_nobody_runs_a_desk_turn(tmp_path: Path) -> None:
    world = World(replace(Behaviour(), desk_turn=False))
    env = make_env(world, tmp_path)
    report = Report(started="test")
    await run(env, plan({8}), [], report)
    assert row(report, "8").status == "SKIP" and "no desk chat turn" in row(report, "8").detail
    assert world.sent[-1] == "/back"  # away mode is switched off again
    assert report.exit_code() == 0


async def test_the_away_scenario_tells_the_human_what_to_do(tmp_path: Path) -> None:
    said: list[str] = []
    world = World()
    env = make_env(world, tmp_path, said)
    report = Report(started="test")
    await run(env, plan({8}), [], report)
    assert row(report, "8").status == "PASS"
    assert said and ".scratch" in said[0] and "Cursor" in said[0]
    assert world.sent == ["/away", "Reply with exactly: OK", "/back"]


async def test_panic_fails_when_a_turn_keeps_running(tmp_path: Path) -> None:
    for keeps, expected in ((False, "PASS"), (True, "FAIL")):
        world = World(replace(Behaviour(), panic_keeps_running=keeps))
        env = make_env(world, tmp_path)
        root = world.add(BOT, "")
        world.roots[root.id] = {"harness": APP, "model": "", "emoji": "🟦", "status": "⏳ running"}
        world.refresh_root(root.id)
        env.threads[APP] = Thread(APP, (await world.get(root.id)) or pytest.fail("no root"))
        report = Report(started="test")
        await run(env, plan({9}), [], report)
        assert row(report, "9").status == expected, table(report)
        if keeps:
            assert "no running turn" in row(report, "9").detail
        else:
            assert world.paused and world.sent == ["/panic", "/away", "/help", "/back"]


async def test_no_scope_skips_the_card_scenarios_and_runs_the_typed_forms(tmp_path: Path) -> None:
    world = World(replace(Behaviour(), submit_scope=False))
    report, _ = await drive(world, tmp_path)
    labels = [r.label for r in report.results]
    assert labels == ["1", "2", "2b", "3", "4", "5", "6", "6b", "7", "8", "9", "9b"], table(report)
    for label in ("2", "6", "9"):
        assert row(report, label).status == "SKIP" and row(report, label).detail == NO_SCOPE
    expected = {"2": "SKIP", "6": "SKIP", "9": "SKIP"}
    assert {r.label: r.status for r in report.results} == {
        r.label: expected.get(r.label, "PASS") for r in report.results
    }
    assert '/new vscode_copilot scratch "Reply with exactly: OK"' in world.sent
    assert "/panic now" in world.sent
    assert report.exit_code() == 0


async def test_a_refused_submit_after_a_yes_probe_falls_back_the_same_way(tmp_path: Path) -> None:
    world = World(replace(Behaviour(), submit_scope=False, probe_lies=True))
    env = make_env(world, tmp_path)
    env.can_submit = True  # the probe said yes
    report = Report(started="test")
    await run(env, plan({2, 3}), [APP], report)
    assert (row(report, "2").status, row(report, "2b").status, row(report, "3").status) == (
        "SKIP",
        "PASS",
        "PASS",
    )
    assert "403" in row(report, "2").detail
    assert env.can_submit is False
    assert env.budget.used[APP] == 2 <= SESSION_CAP


async def test_the_session_cap_stops_a_run_before_it_sends_anything(tmp_path: Path) -> None:
    world = World()
    env = make_env(world, tmp_path)
    env.budget.used[APP] = SESSION_CAP
    report = Report(started="test")
    await run(env, plan({2}), [APP], report)
    assert row(report, "2").status == "FAIL" and "session cap" in row(report, "2").detail
    assert world.sent == []


def test_the_budget_allows_three_per_adapter_and_counts_adapters_apart() -> None:
    budget = SessionBudget()
    for _ in range(SESSION_CAP):
        budget.charge("opencode")
    with pytest.raises(BudgetExceeded):
        budget.charge("opencode")
    budget.charge("vscode")
    assert budget.used == {"opencode": 3, "vscode": 1}


async def test_an_error_text_never_carries_a_secret_into_the_report(tmp_path: Path) -> None:
    class Leaky(World):
        async def send(
            self, room_id: str, text: str, *, parent_id: str | None = None, file: Path | None = None
        ) -> Msg:
            raise RuntimeError("boom Bearer abcdefghijklmnop1234 and sekret-value-12345")

    world = Leaky()
    env = make_env(world, tmp_path)
    env.redact.add("sekret-value-12345")
    report = Report(started="test")
    await run(env, plan({1}), [], report)
    detail = row(report, "1").detail
    assert row(report, "1").status == "FAIL" and "boom" in detail
    assert "abcdefghijklmnop1234" not in detail and "sekret-value-12345" not in detail
    assert "abcdefghijklmnop1234" not in report.markdown()
