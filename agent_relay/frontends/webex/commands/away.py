"""/away, /back and /panic."""

from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue

from agent_relay.core.service import RelayService
from agent_relay.frontends import card_state
from agent_relay.frontends.actions import Outcome, register_action
from agent_relay.frontends.card_state import InvalidInput
from agent_relay.frontends.commands import Command
from agent_relay.frontends.registry import CommandSpec, register
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.handlers import Context, Handlers
from agent_relay.models.events import utcnow

PANIC_ISSUED_PREFIX = "panic.issued:"  # when the confirm card was sent; the nonce itself is spent on submit


async def _away(h: Handlers, ctx: Context, cmd: Command) -> None:
    """Away mode is one global switch: `/away` and `/back` mean the same in a thread and at the top."""
    enabled = not cmd.args or cmd.args[0] == "on"
    await h.reply(ctx, await h.service.set_away_for(enabled, None))


async def _back(h: Handlers, ctx: Context, cmd: Command) -> None:
    released = await h.service.back(None)
    switched = await h.service.set_away_for(False, None)
    await h.reply(ctx, f"{released}\n{switched}" if released.strip() else switched)


async def _panic(h: Handlers, ctx: Context, cmd: Command) -> None:
    if cmd.args:
        await h.reply(ctx, await h.service.panic())
        return
    nonce = await card_state.issue_nonce(h.service.registry)
    await h.service.registry.kv_set(f"{PANIC_ISSUED_PREFIX}{nonce}", utcnow().isoformat())
    await h.card(ctx, "Cancel everything?", cards.panic_confirm(nonce=nonce))


def _fresh(issued: str | None) -> bool:
    if issued is None:
        return False
    try:
        age = utcnow() - datetime.fromisoformat(issued)
    except ValueError:
        return False
    return age.total_seconds() <= cards.CONFIRM_WINDOW_S


async def _panic_action(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    op, nonce = inputs.get("op"), inputs.get("nonce")
    if op not in ("confirm", "cancel"):
        raise InvalidInput("That is not an action this card offers.")
    issued = (
        await service.registry.kv_pop(f"{PANIC_ISSUED_PREFIX}{nonce}") if isinstance(nonce, str) else None
    )
    top = Context(None)
    if op == "cancel":
        await handlers.reply(top, "Left everything as it is.")
    elif not _fresh(issued):
        await handlers.reply(
            top, f"That confirmation expired ({cards.CONFIRM_WINDOW_S} s). Send `/panic` again."
        )
        return "expired"
    else:
        await handlers.reply(top, await service.panic())
    return "done"


# A failed panic may have cancelled some sessions already; run `/panic` again rather than replay a card.
register_action("panic", _panic_action, retryable=False)

register(
    CommandSpec(
        "away",
        _away,
        "away mode on or off: your desk chats come to the phone while it is on",
        usage="/away [on|off]",
        max_args=1,
        choices=("on", "off"),
        order=20,
    )
)
register(CommandSpec("back", _back, "away mode off: release parked desk chats", order=21))
register(
    CommandSpec(
        "panic",
        _panic,
        "cancel everything and pause (asks first; `/panic now` does not)",
        usage="/panic [now]",
        max_args=1,
        choices=("now",),
        order=22,
    )
)
