"""`relay install|uninstall`: the whole touch table as one reversible plan, dry run by default."""

from __future__ import annotations

from typing import Annotated

import typer

from agent_relay.cli.output import DRY_RUN_HINT, fail, note, notes, out, python_option, written
from agent_relay.fileops import LinkBlockedError, Manifest
from agent_relay.ops.install import Outcome, apply_install, apply_uninstall, plan_install, uninstall_report
from agent_relay.settings import load_config
from agent_relay.sync.plan import CatalogMissingError
from agent_relay.sync.secrets import MissingMcpSecretError

Apply = Annotated[bool, typer.Option("--apply", help="Make the changes (backups are kept).")]
Diff = Annotated[bool, typer.Option("--diff", help="Show the full diff of every file.")]
Python = Annotated[str | None, typer.Option("--python", help="Interpreter for relayd, hooks and relay-mcp.")]


def _finish(outcome: Outcome, verb: str) -> None:
    written(outcome.changed, verb)
    for command in outcome.commands:
        note(f"ran {command}")
    notes(outcome.notes)
    if not outcome.ok:
        notes(outcome.problems)
        raise fail("some steps failed; see above, then run `relay doctor`")


def install(apply: Apply = False, diff: Diff = False, python: Python = None) -> None:
    """Hooks, MCP sync, skills, launch agents and the VS Code extension. Dry run unless --apply."""
    try:
        plan = plan_install(load_config(), python or python_option())
    except MissingMcpSecretError as exc:
        raise fail(f"{exc}. Add it to the Keychain, or run `relay config import --apply`.") from exc
    except CatalogMissingError as exc:
        raise fail(str(exc)) from exc
    notes(plan.notes)
    for line in plan.report():
        out(line)
    if diff:
        out(plan.diff().rstrip("\n"))
    if not apply:
        note(DRY_RUN_HINT)
        return
    try:
        outcome = apply_install(plan)
    except LinkBlockedError as exc:
        raise fail(str(exc)) from exc
    _finish(outcome, "changed")
    out("Installed. Next: `relay doctor`.")


def uninstall(apply: Apply = False) -> None:
    """Undo `relay install`: remove what it created, unmerge shared files, restore the rest."""
    lines = uninstall_report(Manifest.load())
    if not lines:
        out("Nothing is installed.")
        return
    for line in lines:
        out(line)
    if not apply:
        note(DRY_RUN_HINT)
        return
    _finish(apply_uninstall(), "restored")
    out("Uninstalled. relay.toml, workspaces.toml, the session history and logs are kept.")
