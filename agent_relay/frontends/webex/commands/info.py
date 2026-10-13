"""/help, /apps and /doctor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_relay.frontends import registry
from agent_relay.frontends.commands import Command
from agent_relay.frontends.registry import CommandSpec, register

if TYPE_CHECKING:
    from agent_relay.frontends.webex.handlers import Context, Handlers


async def _help(h: Handlers, ctx: Context, cmd: Command) -> None:
    await h.reply(ctx, registry.help_text())


async def _apps(h: Handlers, ctx: Context, cmd: Command) -> None:
    apps = await h.service.apps()
    lines = [
        f"- {'✅' if a.healthy else '❌'} **{a.label}** (`{a.harness.value}`): "
        f"{', '.join(c.value for c in a.capabilities) or 'observe only'}"
        for a in apps
    ]
    await h.reply(ctx, "\n".join(lines) or "No adapters are enabled.")


async def _doctor(h: Handlers, ctx: Context, cmd: Command) -> None:
    await h.reply(ctx, await h.doctor())


register(CommandSpec("apps", _apps, "list apps", max_args=0, order=30))
register(CommandSpec("doctor", _doctor, "health check", order=32))
register(CommandSpec("help", _help, "this list", aliases=("?",), order=33))
