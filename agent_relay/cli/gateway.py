"""`relay gateway`: choose the MCP servers that go through the gateway, and manage their sign-ins.

Signing in itself happens from the phone (`/mcp signin <name>`), or by opening the link on the Mac.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from agent_relay import paths
from agent_relay.cli.output import fail, note, out
from agent_relay.gateway.oauth import ClientInfo
from agent_relay.gateway.service import CatalogServers
from agent_relay.gateway.signin import CALLBACK_PATH
from agent_relay.gateway.store import Credentials, CredentialStore
from agent_relay.settings import load_config
from agent_relay.sync import catalog
from agent_relay.sync.model import Catalog, CatalogEntry

app = typer.Typer(help="MCP servers behind the sign-in gateway (off by default).", no_args_is_help=True)

Name = Annotated[str, typer.Argument(help="The server's name in ~/.agents/mcp.yaml.")]
NEXT_STEPS = (
    "Next: `relay config set gateway.enabled true`, `relay config sync --apply`, "
    "`relay stop && relay start`, then `/mcp signin <name>` on the phone."
)


def _write(updated: Catalog) -> None:
    file = paths.mcp_yaml()
    file.write_text(catalog.dump(updated), encoding="utf-8")


def _entry(source: Catalog, name: str) -> CatalogEntry:
    entry = source.servers.get(name)
    if entry is None:
        known = ", ".join(sorted(source.servers)) or "none"
        raise fail(f"No MCP server named {name} in {paths.mcp_yaml()}. Known: {known}.")
    return entry


@app.command("add")
def add(
    name: Name,
    url: Annotated[
        str | None,
        typer.Option("--url", help="The server's real MCP URL, for a server run through a bridge."),
    ] = None,
) -> None:
    """Send this server through the gateway."""
    source = catalog.load()
    entry = _entry(source, name)
    changed = entry.model_copy(update={"gateway": True, "gateway_url": url or entry.gateway_url})
    if changed.upstream() is None:
        raise fail(f"{name} is not a remote server: pass its real address with --url https://…")
    servers = {**source.servers, name: changed}
    _write(source.model_copy(update={"servers": servers}))
    out(f"{name}: through the gateway, upstream {changed.upstream()}")
    note(NEXT_STEPS)


@app.command("remove")
def remove(name: Name) -> None:
    """Send this server directly again (its sign-in stays stored)."""
    source = catalog.load()
    entry = _entry(source, name)
    servers = {**source.servers, name: entry.model_copy(update={"gateway": False})}
    _write(source.model_copy(update={"servers": servers}))
    out(f"{name}: direct again")
    note("Run `relay config sync --apply` to write the apps' files.")


@app.command("status")
def status() -> None:
    """Which servers go through the gateway and whether each is signed in."""
    config = load_config()
    store = CredentialStore()
    found = CatalogServers()()
    out(f"gateway: {'on' if config.gateway.enabled else 'off'} (port {config.gateway.port})")
    for name, upstream in sorted(found.items()):
        tokens = store.load(name).tokens
        state = "signed out" if tokens is None else "signed in"
        out(f"{name}: {state}, upstream {upstream}")
    if not found:
        out("no MCP server goes through the gateway")


@app.command("client")
def client(
    name: Name,
    client_id: Annotated[str, typer.Option("--client-id", help="The OAuth client id you registered.")],
    redirect_uri: Annotated[
        str | None, typer.Option("--redirect-uri", help="The redirect address registered with that client.")
    ] = None,
    secret_stdin: Annotated[
        bool, typer.Option("--secret-stdin", help="Read the client secret from standard input.")
    ] = False,
) -> None:
    """Use an OAuth client you registered (a server without open registration, such as Webex)."""
    if name not in CatalogServers()():
        raise fail(f"{name} is not a gatewayed server: `relay gateway add {name}` first.")
    secret = None
    if secret_stdin:
        if sys.stdin.isatty():
            raise fail(
                "Pipe the secret in: pbpaste | relay gateway client <name> --client-id … --secret-stdin"
            )
        secret = sys.stdin.read().strip() or None
    port = load_config().gateway.port
    chosen = redirect_uri or f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    store = CredentialStore()
    current = store.load(name)
    info = ClientInfo(client_id=client_id, client_secret=secret, redirect_uri=chosen)
    store.save(name, Credentials(discovery=current.discovery, client=info, tokens=None))
    out(f"{name}: client {client_id} stored, redirect {chosen}")


@app.command("signout")
def signout(name: Name) -> None:
    """Forget a server's tokens."""
    CredentialStore().sign_out(name)
    out(f"{name}: signed out")
