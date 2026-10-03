"""`relay doctor`: every check, its status, and the command that fixes it."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from agent_relay import doctor as checks
from agent_relay.cli.output import out, python_option
from agent_relay.settings import load_config


def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Check adapters, versions, hooks, MCP and skills drift, Keychain, launchd, Webex and away mode."""
    report = asyncio.run(checks.run(load_config(), python_option()))
    out(report.to_json() if as_json else report.render())
    if not report.ok:
        raise typer.Exit(1)
