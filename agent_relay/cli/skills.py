"""`relay skills dedupe`: drop harness copies of skills that ~/.agents/skills already provides."""

from __future__ import annotations

from typing import Annotated

import typer

from agent_relay.cli.output import DRY_RUN_HINT, note, out, written
from agent_relay.fileops import timestamp
from agent_relay.sync.skills import plan_dedupe

app = typer.Typer(help="Keep one copy of each skill, in ~/.agents/skills.", no_args_is_help=True)


@app.command("dedupe")
def dedupe(
    apply: Annotated[bool, typer.Option("--apply", help="Move the duplicates aside.")] = False,
) -> None:
    """Move byte-identical harness copies to a dated backup folder; different copies are kept."""
    planned = plan_dedupe(timestamp())
    for line in planned.report():
        out(line)
    if not planned.moves:
        out("No duplicates to move.")
        return
    if apply:
        written(planned.apply(), verb="moved")
    else:
        note(DRY_RUN_HINT)
