"""/away, /back and /panic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_relay.frontends.commands import Command
from agent_relay.frontends.registry import CommandSpec, register

if TYPE_CHECKING:
    from agent_relay.frontends.webex.handlers import Context, Handlers


async def _away(h: Handlers, ctx: Context, cmd: Command) -> None:
    key = ctx.session.session_key if ctx.session else None
    await h.reply(ctx, await h.service.set_away(not cmd.args or cmd.args[0] == "on", key))


async def _back(h: Handlers, ctx: Context, cmd: Command) -> None:
    key = ctx.session.session_key if ctx.session else None
    await h.reply(ctx, await h.service.back(key))


async def _panic(h: Handlers, ctx: Context, cmd: Command) -> None:
    await h.reply(ctx, await h.service.panic())


register(
    CommandSpec(
        "away",
        _away,
        "away mode on or off, in a thread or globally (bare `/away` is on)",
        usage="/away [on|off]",
        max_args=1,
        choices=("on", "off"),
        order=20,
    )
)
register(CommandSpec("back", _back, "release parked sessions (this one, in a thread)", order=21))
register(CommandSpec("panic", _panic, "cancel everything and pause", order=22))
