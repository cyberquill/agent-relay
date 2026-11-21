"""Argument parsing, the RELAY_LIVE gate and the wiring of a live run."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from agent_relay.models.events import Harness
from scripts.e2e.env import SESSION_CAP, Env, SessionBudget, Settings
from scripts.e2e.model import Clock, DriverError, RealClock, Redactor, WebexUser
from scripts.e2e.report import Report
from scripts.e2e.runner import NUMBERS, Scenario, cleanup, needs_apps, plan, run
from scripts.e2e.webex_cli_user import DEFAULT_CLI, WebexCliUser

ROOM_TITLE = "Brij Copilot Agent"
# Apps a session can be started in from the phone; the desk-only harnesses (hooks) are not.
PHONE_APPS = frozenset(
    {Harness.VSCODE, Harness.VSCODE_COPILOT, Harness.CURSOR_CLI, Harness.CURSOR_CDP, Harness.OPENCODE}
)
CARD_SCENARIOS = frozenset({2, 6, 9})
# The spellings `/new` accepts (a copy of core.service.common.APP_ALIASES, checked by a test), so the
# driver imports nothing from the relay's service layer.
APP_ALIASES: dict[str, Harness] = {
    "vscode": Harness.VSCODE,
    "copilot": Harness.VSCODE_COPILOT,
    "vscode-copilot": Harness.VSCODE_COPILOT,
    "ahp": Harness.VSCODE_COPILOT,
    "copilot-cli": Harness.COPILOT_CLI,
    "cursor": Harness.CURSOR_CLI,
    "cursor-cli": Harness.CURSOR_CLI,
    "cursor-ide": Harness.CURSOR_CDP,
    "cdp": Harness.CURSOR_CDP,
    "opencode": Harness.OPENCODE,
    "oc": Harness.OPENCODE,
}

type UserFactory = Callable[[Redactor, Path], WebexUser]


def _default_user(redact: Redactor, cli: Path) -> WebexUser:
    return WebexCliUser(redact, cli)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phone_e2e",
        description="Drive the relay end to end as the owner, through the Webex person token that "
        "webex-cli uses. Needs RELAY_LIVE=1. Live sessions only in .scratch, at most "
        f"{SESSION_CAP} per adapter per run.",
    )
    parser.add_argument("--only", default="", help="scenario numbers, comma separated (default: all)")
    parser.add_argument(
        "--apps",
        default="",
        help="apps for scenarios 2 to 7, comma separated, e.g. copilot,vscode,cursor,opencode",
    )
    parser.add_argument("--room", default=ROOM_TITLE, help="title of the 1:1 room with the bot")
    parser.add_argument("--room-id", default="", help="use this room id instead of finding it by title")
    parser.add_argument("--report", default="", help="also write the markdown table to this file")
    parser.add_argument("--webex-cli", default=str(DEFAULT_CLI), help="path of the webex-cli script")
    parser.add_argument("--turn-timeout", type=float, default=Settings.turn_s)
    parser.add_argument(
        "--quiet", type=float, default=Settings.quiet_s, help="seconds to watch for a second message"
    )
    parser.add_argument(
        "--away-wait", type=float, default=Settings.away_wait_s, help="seconds to wait for the desk turn"
    )
    return parser


def parse_numbers(text: str) -> set[int]:
    chosen: set[int] = set()
    for part in filter(None, (p.strip() for p in text.split(","))):
        if not part.isdigit() or int(part) not in NUMBERS:
            raise DriverError(f"unknown scenario {part!r}; scenarios are {', '.join(map(str, NUMBERS))}")
        chosen.add(int(part))
    return chosen


def parse_apps(text: str) -> list[str]:
    """Harness values for `--apps`, aliases resolved, phone-startable apps only, duplicates dropped."""
    apps: list[str] = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        lowered = part.lower()
        try:
            harness = APP_ALIASES.get(lowered) or Harness(lowered)
        except ValueError:
            raise DriverError(f"unknown app {part!r}") from None
        if harness not in PHONE_APPS:
            raise DriverError(f"{harness.value} cannot be started from the phone")
        if harness.value not in apps:
            apps.append(harness.value)
    return apps


async def execute(
    args: argparse.Namespace,
    selected: list[Scenario],
    apps: list[str],
    *,
    user: WebexUser,
    clock: Clock,
    redact: Redactor,
    say: Callable[[str], None],
    tmp_dir: Path,
) -> Report:
    me = await user.whoami()
    room_id = args.room_id or await user.find_room(args.room)
    bot = await user.counterpart(room_id)
    wants_cards = any(s.number in CARD_SCENARIOS for s in selected)
    can_submit = await user.can_submit_cards() if wants_cards else True
    settings = Settings(turn_s=args.turn_timeout, quiet_s=args.quiet, away_wait_s=args.away_wait)
    env = Env(
        user=user,
        clock=clock,
        room_id=room_id,
        bot_email=bot.email,
        me=me,
        settings=settings,
        budget=SessionBudget(),
        redact=redact,
        say=say,
        tmp_dir=tmp_dir,
        can_submit=can_submit,
    )
    report = Report(started=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"))
    if not can_submit:
        report.notes.append("the owner's token cannot submit cards: card scenarios ran as typed commands")
    try:
        await run(env, selected, apps, report)
    finally:
        await cleanup(env, report)
    return report


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    user_factory: UserFactory = _default_user,
    clock: Clock | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    env_vars = os.environ if environ is None else environ
    out, err = stdout or sys.stdout, stderr or sys.stderr
    args = build_parser().parse_args(argv)
    if env_vars.get("RELAY_LIVE") != "1":
        err.write("phone_e2e: set RELAY_LIVE=1; this drives the real relay and real apps\n")
        return 2
    redact = Redactor()

    def say(line: str) -> None:
        out.write(f"{redact(line)}\n")

    try:
        selected = plan(parse_numbers(args.only) or None)
        apps = parse_apps(args.apps)
        if needs_apps(selected) and not apps:
            raise DriverError("choose the apps to test with --apps, e.g. --apps copilot,cursor")
        with tempfile.TemporaryDirectory(prefix="relay-e2e-", dir="/tmp") as tmp:
            report = asyncio.run(
                execute(
                    args,
                    selected,
                    apps,
                    user=user_factory(redact, Path(args.webex_cli)),
                    clock=clock or RealClock(),
                    redact=redact,
                    say=say,
                    tmp_dir=Path(tmp),
                )
            )
    except DriverError as exc:
        err.write(f"phone_e2e: {redact(str(exc))}\n")
        return 2
    except Exception as exc:
        err.write(f"phone_e2e: {type(exc).__name__}: {redact(str(exc))}\n")
        return 2
    out.write(report.text())
    if args.report:
        Path(args.report).write_text(report.markdown(), encoding="utf-8")
    return report.exit_code()
