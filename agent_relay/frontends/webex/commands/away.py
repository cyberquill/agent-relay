"""/away, /back and /panic."""

from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue

from agent_relay.core import away
from agent_relay.core.service import RelayService
from agent_relay.frontends import card_state
from agent_relay.frontends.actions import Outcome, register_action
from agent_relay.frontends.card_state import InvalidInput
from agent_relay.frontends.commands import Command
from agent_relay.frontends.registry import CommandSpec, register
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.handlers import Context, Handlers
from agent_relay.models.events import utcnow
from agent_relay.settings import load_workspaces, resolve_workspace

PANIC_ISSUED_PREFIX = "panic.issued:"  # when the confirm card was sent; the nonce itself is spent on submit


async def _state(h: Handlers, workspace: str | None) -> str:
    return "🌙 away" if await away.is_away(h.service.registry, workspace) else "at desk"


async def _away_card(h: Handlers, ctx: Context) -> None:
    choices = [(cards.ALL_WORKSPACES, f"All workspaces · {await _state(h, None)}")]
    for ws in h.service.workspaces():
        choices.append((ws.name, f"{ws.name} · {await _state(h, ws.path)}"))
    nonce = await card_state.issue_nonce(h.service.registry)
    await h.card(ctx, "Away mode", cards.away_scope(choices, nonce=nonce))


async def _away(h: Handlers, ctx: Context, cmd: Command) -> None:
    if ctx.session is None and not cmd.args:
        await _away_card(h, ctx)
        return
    key = ctx.session.session_key if ctx.session else None
    await h.reply(ctx, await h.service.set_away(not cmd.args or cmd.args[0] == "on", key))


async def _away_action(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    op, scope = inputs.get("op"), inputs.get("scope")
    if op not in ("on", "off"):
        raise InvalidInput("That is not an action this card offers.")
    if not isinstance(scope, str) or not scope:
        raise InvalidInput("Pick all workspaces or one first.")
    workspace: str | None = None
    if scope != cards.ALL_WORKSPACES:
        target = resolve_workspace(scope, load_workspaces())
        if target is None:
            raise InvalidInput(f"Unknown workspace `{scope}`. Try `/ws`.")
        workspace = target.path
    await handlers.reply(Context(None), await service.set_away_for(op == "on", workspace))
    return "done"


async def _back(h: Handlers, ctx: Context, cmd: Command) -> None:
    key = ctx.session.session_key if ctx.session else None
    await h.reply(ctx, await h.service.back(key))


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
register_action("away", _away_action)
register_action("panic", _panic_action, retryable=False)

register(
    CommandSpec(
        "away",
        _away,
        "away mode on or off (bare `/away` asks which workspaces at the top level, means on in a thread)",
        usage="/away [on|off]",
        max_args=1,
        choices=("on", "off"),
        order=20,
    )
)
register(CommandSpec("back", _back, "release parked sessions (this one, in a thread)", order=21))
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
