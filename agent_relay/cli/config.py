"""`relay config import|sync`: one MCP server list in ~/.agents/mcp.yaml, rendered into every harness."""

from __future__ import annotations

from typing import Annotated

import typer

from agent_relay.cli.output import DRY_RUN_HINT, fail, note, notes, out, python_option, written
from agent_relay.sync.plan import CatalogMissingError, Plan, plan_import, plan_sync
from agent_relay.sync.secrets import MissingMcpSecretError

app = typer.Typer(
    help="Share MCP servers across VS Code, Copilot, Cursor and OpenCode.", no_args_is_help=True
)

Apply = Annotated[bool, typer.Option("--apply", help="Write the changes (backups are kept).")]
Python = Annotated[str | None, typer.Option("--python", help="Interpreter that runs relay-mcp.")]


def _show(plan: Plan, apply: bool) -> None:
    notes(plan.notes)
    changed = plan.changed()
    if not changed:
        out("Everything already matches.")
        return
    diff = plan.diff()
    if diff:
        out(diff.rstrip("\n"))
    if apply:
        written(plan.apply())
    else:
        note(DRY_RUN_HINT)


@app.command("import")
def import_(apply: Apply = False, python: Python = None) -> None:
    """Read every harness's MCP servers into ~/.agents/mcp.yaml; literal secrets move to the Keychain."""
    _show(plan_import(python or python_option()), apply)


@app.command("sync")
def sync(apply: Apply = False, python: Python = None) -> None:
    """Render ~/.agents/mcp.yaml into each harness's own file, and Cursor's allowlist into the CLI."""
    try:
        planned = plan_sync(python=python or python_option())
    except MissingMcpSecretError as exc:
        raise fail(f"{exc}. Run `relay config import --apply` again, or add it to the Keychain.") from exc
    except CatalogMissingError as exc:
        raise fail(str(exc)) from exc
    _show(planned, apply)
