"""Guided live smoke test from the phone. Needs RELAY_LIVE=1 and a running relayd (after `relay install`).

    (export RELAY_LIVE=1; uv run python scripts/smoke_live.py [--only cursor_cli] [--record])

`--record` appends the results table to docs/operations.md. Nothing secret is printed or recorded.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_relay import paths
from agent_relay.ipc import JsonObj, request
from agent_relay.ops.smoke import APPS, Smoke, Waiter, render

OPERATIONS = Path(__file__).resolve().parents[1] / "docs" / "operations.md"


def ipc(message: JsonObj) -> JsonObj:
    return request(message, paths.socket_path(), timeout=20.0)


def say(text: str) -> None:
    sys.stdout.write(f"{text}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", default=[], help="harness value, e.g. opencode")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("RELAY_LIVE") != "1":
        sys.stderr.write("smoke_live: set RELAY_LIVE=1; this drives the real apps\n")
        return 2
    try:
        ipc({"op": "ping"})
    except OSError as exc:
        sys.stderr.write(
            f"smoke_live: relayd is not reachable ({exc.__class__.__name__}); run `relay start`\n"
        )
        return 2
    chosen = [app for app in APPS if not args.only or app.harness.value in args.only]
    smoke = Smoke(Waiter(ipc, timeout_s=args.timeout), input, say)
    outcomes = [smoke.run_app(app) for app in chosen]
    table = render(smoke.results, datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"))
    sys.stdout.write(f"\n{table}")
    if args.record:
        with OPERATIONS.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{table}")
    return 0 if all(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
