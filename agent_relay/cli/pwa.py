"""`relay pwa expose`: print the Tailscale command for the PWA. The relay never runs it."""

from __future__ import annotations

import typer

from agent_relay.cli.output import note, out
from agent_relay.frontends.pwa.server import LOOPBACK
from agent_relay.settings import SECRET_SETUP_COMMAND, Secret, load_config

app = typer.Typer(help="The optional browser front end (off by default).", no_args_is_help=True)


def serve_command(port: int) -> str:
    return f"tailscale serve --bg --https=443 http://{LOOPBACK}:{port}"


@app.command("expose")
def expose() -> None:
    """Print the `tailscale serve` command that publishes the PWA to your tailnet only."""
    config = load_config()
    if not config.pwa.enabled:
        note("The PWA is off. Set pwa.enabled = true in relay.toml and restart relayd first.")
    note(f"Token: {SECRET_SETUP_COMMAND[Secret.PWA_TOKEN]}")
    note("Run this only if you use Tailscale. Never use Funnel.")
    out(serve_command(config.pwa.port))
