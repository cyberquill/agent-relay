"""`relay`: set up, inspect and drive Agent Relay from the Mac's terminal."""

from __future__ import annotations

import typer

from agent_relay.cli import config, pwa, skills
from agent_relay.cli.doctor import doctor

app = typer.Typer(help="Agent Relay: drive your coding agents from your phone.", no_args_is_help=True)
app.add_typer(config.app, name="config")
app.add_typer(skills.app, name="skills")
app.add_typer(pwa.app, name="pwa")
app.command("doctor")(doctor)
