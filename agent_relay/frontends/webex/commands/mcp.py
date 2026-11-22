"""/mcp: the MCP servers behind the gateway, and signing in or out from the phone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_relay.frontends.commands import Command
from agent_relay.frontends.registry import CommandSpec, register
from agent_relay.gateway.oauth import OAuthError

if TYPE_CHECKING:
    from agent_relay.frontends.webex.handlers import Context, Handlers

OFF = "The MCP gateway is off. `relay config set gateway.enabled true`, then restart relayd."
NONE = "No MCP server goes through the gateway yet. On the Mac: `relay gateway add <name>`."


async def _mcp(h: Handlers, ctx: Context, cmd: Command) -> None:
    signin = h.mcp
    if signin is None:
        await h.reply(ctx, OFF)
        return
    if not cmd.args:
        rows = [f"- **{s.name}**: {s.state}" for s in signin.status()]
        await h.reply(ctx, "\n".join(rows) if rows else NONE)
        return
    action, name = cmd.args[0], cmd.args[1] if len(cmd.args) > 1 else ""
    known = {s.name for s in signin.status()}
    if name not in known:
        listing = ", ".join(sorted(known)) or "none"
        await h.reply(ctx, f"⚠️ Say which server: `/mcp {action} <name>`. Known: {listing}.")
        return
    if action == "signout":
        signin.sign_out(name)
        await h.reply(ctx, f"🔓 {name} is signed out.")
        return
    try:
        await signin.begin(name, "needs you to sign in")
    except OAuthError as exc:
        await h.reply(ctx, f"⚠️ {name}: {exc}.")


register(
    CommandSpec(
        "mcp",
        _mcp,
        "MCP servers: sign-in state, `signin <name>`, `signout <name>`",
        scope="top",
        usage="/mcp [signin|signout <name>]",
        max_args=2,
        choices=("signin", "signout"),
        order=34,
    )
)
