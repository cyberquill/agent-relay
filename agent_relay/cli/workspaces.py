"""`relay ws add|rm|list`: the named workspaces the phone can start sessions in."""

from __future__ import annotations

from typing import Annotated

import typer

from agent_relay import paths
from agent_relay.cli.output import fail, out
from agent_relay.models.config import Workspace, WorkspacesConfig
from agent_relay.settings import load_workspaces, save_workspaces

app = typer.Typer(help="Workspaces the phone can start sessions in.", no_args_is_help=True)


def added(current: WorkspacesConfig, name: str, folder: str) -> WorkspacesConfig:
    resolved = paths.expand_user(folder).resolve()
    if not resolved.is_dir():
        raise ValueError(f"{resolved} is not a directory")
    if any(w.name == name for w in current.workspaces):
        raise ValueError(f"a workspace named {name!r} exists; remove it first with `relay ws rm {name}`")
    entry = Workspace(name=name, path=str(resolved))
    return WorkspacesConfig(workspaces=[*current.workspaces, entry])


def removed(current: WorkspacesConfig, name: str) -> WorkspacesConfig:
    kept = [w for w in current.workspaces if w.name != name]
    if len(kept) == len(current.workspaces):
        raise ValueError(f"no workspace named {name!r}")
    return WorkspacesConfig(workspaces=kept)


@app.command("add")
def add(
    name: Annotated[str, typer.Argument(help="Short name used from the phone, e.g. relay")],
    folder: Annotated[str, typer.Argument(help="Folder path")],
) -> None:
    """Add a named workspace."""
    try:
        updated = added(load_workspaces(), name, folder)
    except ValueError as exc:
        raise fail(str(exc)) from exc
    out(f"wrote {save_workspaces(updated)}")


@app.command("rm")
def rm(name: Annotated[str, typer.Argument(help="Workspace name")]) -> None:
    """Remove a named workspace."""
    try:
        updated = removed(load_workspaces(), name)
    except ValueError as exc:
        raise fail(str(exc)) from exc
    out(f"wrote {save_workspaces(updated)}")


@app.command("list")
def list_() -> None:
    """List workspaces."""
    current = load_workspaces().workspaces
    if not current:
        out("No workspaces. Add one with `relay ws add <name> <path>`.")
    for workspace in current:
        out(f"{workspace.name}\t{workspace.path}")
