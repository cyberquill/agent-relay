"""`relay selftest cursor-cdp`: check every Cursor composer selector against the live window."""

from __future__ import annotations

import asyncio

import typer

from agent_relay import paths
from agent_relay.adapters.base import AdapterUnavailableError
from agent_relay.adapters.cursor_cdp.selectors import load_selectors
from agent_relay.adapters.cursor_cdp.selftest import SelftestReport, run_live
from agent_relay.cli.output import fail, note, out
from agent_relay.core.registry import Registry
from agent_relay.settings import load_config

app = typer.Typer(
    help="Self-tests that gate adapters which depend on an app's internals.", no_args_is_help=True
)

DISABLED = (
    "the Cursor CDP adapter is off, and its debug port only runs while it is on. "
    "Enable it first: `relay config set cdp.enabled true`"
)


async def cursor_cdp_report(port: int) -> SelftestReport:
    registry = Registry(paths.db_path())
    await registry.open()
    try:
        return await run_live(registry, port, load_selectors())
    finally:
        await registry.close()


@app.command("cursor-cdp")
def cursor_cdp() -> None:
    """Start Cursor with its debug port if needed, then match every selector. Open the chat pane first."""
    config = load_config()
    if not config.cdp.enabled:
        raise fail(DISABLED)
    try:
        report = asyncio.run(cursor_cdp_report(config.cdp.port))
    except AdapterUnavailableError as exc:
        raise fail(str(exc)) from exc
    out(report.render())
    if not report.ok:
        raise fail("self-test failed; the CDP adapter stays disabled for this Cursor version")
    note("recorded; relayd enables the CDP adapter for this Cursor version")
