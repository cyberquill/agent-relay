"""`relay config set|show|import|sync`: relay settings, and one MCP server list for every harness."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from pydantic import JsonValue, ValidationError

from agent_relay.cli.output import DRY_RUN_HINT, fail, note, notes, out, python_option, written
from agent_relay.models.config import RelayConfig
from agent_relay.settings import load_config, save_config
from agent_relay.sync.plan import CatalogMissingError, Plan, plan_import, plan_sync
from agent_relay.sync.secrets import MissingMcpSecretError

app = typer.Typer(
    help="relay.toml settings, and one MCP server list shared by VS Code, Copilot, Cursor and OpenCode.",
    no_args_is_help=True,
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


def parse_value(raw: str) -> JsonValue:
    """`true`, `8788`, `["a","b"]` are JSON; anything else is a plain string."""
    try:
        value: JsonValue = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return value


# Settings that are a name -> value map: `relay config set webex.bots.vscode <account>` adds an entry.
MAP_SETTINGS = frozenset({"webex.bots"})


def with_setting(config: RelayConfig, key: str, raw: str) -> RelayConfig:
    data: dict[str, JsonValue] = config.model_dump(mode="json")
    *sections, leaf = key.split(".")
    node: dict[str, JsonValue] = data
    for section in sections:
        child = node.get(section)
        if not isinstance(child, dict):
            raise ValueError(f"unknown section {section!r} in {key!r}")
        node = child
    if leaf not in node and ".".join(sections) not in MAP_SETTINGS:
        raise ValueError(f"unknown setting {key!r}")
    node[leaf] = parse_value(raw)
    return RelayConfig.model_validate(data)


@app.command("set")
def set_(
    key: Annotated[str, typer.Argument(help="Dotted key, e.g. webex.allowed_email")],
    value: Annotated[str, typer.Argument(help="Value; JSON for numbers, booleans and lists")],
) -> None:
    """Change one setting in relay.toml (validated before it is saved)."""
    try:
        updated = with_setting(load_config(), key, value)
    except (ValueError, ValidationError) as exc:
        raise fail(str(exc)) from exc
    out(f"wrote {save_config(updated)}")
    note("restart relayd to apply: `relay start`")


@app.command("show")
def show() -> None:
    """Print the effective relay.toml settings (no secrets live there)."""
    out(json.dumps(load_config().model_dump(mode="json"), indent=2))
