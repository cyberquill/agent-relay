"""`relay`: set up, inspect and drive Agent Relay from the Mac's terminal."""

from __future__ import annotations

import typer

from agent_relay.cli import config, daemon, pwa, selftest, skills, workspaces
from agent_relay.cli.doctor import doctor
from agent_relay.cli.install import install, uninstall

app = typer.Typer(help="Agent Relay: drive your coding agents from your phone.", no_args_is_help=True)
app.add_typer(config.app, name="config")
app.add_typer(skills.app, name="skills")
app.add_typer(pwa.app, name="pwa")
app.add_typer(workspaces.app, name="ws")
app.add_typer(selftest.app, name="selftest")
app.command("doctor")(doctor)
app.command("install")(install)
app.command("uninstall")(uninstall)
app.command("start")(daemon.start)
app.command("stop")(daemon.stop)
app.command("status")(daemon.status)
app.command("away")(daemon.away)
app.command("sync", help="Same as `relay config sync`.")(config.sync)
