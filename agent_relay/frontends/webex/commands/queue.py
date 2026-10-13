"""/queue and /steer, and the card submits that act on queued messages and finished turns."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue

from agent_relay.core.service import RelayService
from agent_relay.core.service.queue import MODES
from agent_relay.frontends import card_state
from agent_relay.frontends.actions import Outcome, register_action
from agent_relay.frontends.card_state import InvalidInput
from agent_relay.frontends.commands import Command, Invalid
from agent_relay.frontends.registry import CommandSpec, register
from agent_relay.frontends.webex.cards import MAX_PROMPT_CHARS
from agent_relay.frontends.webex.cards.completion import REPLY_FIELD
from agent_relay.frontends.webex.cards.queue import MAX_ITEMS, queue_card
from agent_relay.frontends.webex.handlers import Context, Handlers
from agent_relay.models.config import QueueMode

if TYPE_CHECKING:
    from agent_relay.models.sessions import SessionRecord

MODE_HELP: dict[QueueMode, str] = {
    "queue": "a message sent mid-turn waits and goes in as the next turn.",
    "steer": "a message sent mid-turn nudges the running turn.",
    "interrupt": "a message sent mid-turn stops the turn, then goes in as a new one.",
}


def _count(n: int) -> str:
    return f"{n} message{'' if n == 1 else 's'}"


async def _set_mode(h: Handlers, ctx: Context, session: SessionRecord, word: str) -> None:
    mode = await h.service.set_queue_mode(session.session_key, word)
    lines = [f"Queue mode: **{mode}**: {MODE_HELP[mode]}"]
    warning = await h.service.queue_mode_warning(session.session_key, mode)
    if warning:
        lines.append(f"⚠️ {warning}")
    await h.reply(ctx, "\n".join(lines))


async def _clear(h: Handlers, ctx: Context, session: SessionRecord) -> None:
    cleared = await h.service.clear_queued(session.session_key)
    await h.reply(ctx, f"Cleared {_count(cleared)}." if cleared else "Nothing was queued.")


async def _show(h: Handlers, ctx: Context, session: SessionRecord) -> None:
    service = h.service
    key = session.session_key
    mode = await service.queue_mode(key)
    items = [(q.id, q.text) for q in await service.queued(key)]
    nonces = {"mode": await card_state.issue_nonce(service.registry)}
    if items:
        nonces["clear"] = await card_state.issue_nonce(service.registry)
    for item_id, _ in items[:MAX_ITEMS]:
        nonces[f"remove:{item_id}"] = await card_state.issue_nonce(service.registry)
    card = queue_card(key, mode, items, nonces=nonces)
    await h.card(ctx, f"Queue mode: {mode} · {len(items)} waiting", card)


async def _queue(h: Handlers, ctx: Context, cmd: Command) -> None:
    assert ctx.session is not None
    word = cmd.args[0] if cmd.args else None
    if word == "clear":
        await _clear(h, ctx, ctx.session)
    elif word is not None:
        await _set_mode(h, ctx, ctx.session, word)
    else:
        await _show(h, ctx, ctx.session)


def _parse_steer(rest: str) -> Command | Invalid:
    if not rest:
        return Invalid("steer", "usage: `/steer <message>`")
    return Command("steer", (), rest)


async def _steer(h: Handlers, ctx: Context, cmd: Command) -> None:
    assert ctx.session is not None
    receipt = await h.service.steer(ctx.session.session_key, cmd.prompt or "")
    if receipt != "sent":
        await h.reply(ctx, f"📨 {receipt}")


async def _queue_action(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    key = inputs.get("session_key")
    if not isinstance(key, str):
        return "ignored"
    ctx = Context(await service.session(key))
    assert ctx.session is not None
    match inputs.get("op"):
        case "mode":
            word = inputs.get("mode")
            if not isinstance(word, str):
                raise InvalidInput("Pick a mode first.")
            await _set_mode(handlers, ctx, ctx.session, word)
        case "clear":
            await _clear(handlers, ctx, ctx.session)
        case "remove":
            item_id = inputs.get("id")
            if not isinstance(item_id, int) or isinstance(item_id, bool):
                raise InvalidInput("That is not a message this card listed.")
            removed = await service.remove_queued(key, item_id)
            await handlers.reply(ctx, "Removed." if removed else "That message was already sent or removed.")
        case _:
            raise InvalidInput("That is not an action this card offers.")
    return "done"


async def _completion_action(
    service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]
) -> Outcome:
    key = inputs.get("session_key")
    if not isinstance(key, str):
        return "ignored"
    ctx = Context(await service.session(key))
    match inputs.get("op"):
        case "diff":
            diff = await service.diff(key)
            if diff.strip():
                await handlers.reply(ctx, "📎 changes.diff", file=("changes.diff", diff, "text/x-diff"))
            else:
                await handlers.reply(ctx, "No changes.")
        case "continue":
            await _send(service, handlers, ctx, key, "continue")
        case "back":
            await handlers.reply(ctx, await service.back(key))
        case "send":
            reply = inputs.get(REPLY_FIELD)
            if not isinstance(reply, str) or not reply.strip():
                raise InvalidInput("Type a reply first.")
            if len(reply) > MAX_PROMPT_CHARS:
                raise InvalidInput(f"That reply is too long ({MAX_PROMPT_CHARS} characters at most).")
            await _send(service, handlers, ctx, key, reply.strip())
        case _:
            raise InvalidInput("That is not an action this card offers.")
    return "done"


async def _send(service: RelayService, handlers: Handlers, ctx: Context, key: str, text: str) -> None:
    receipt = await service.prompt(key, text)
    if receipt != "sent":
        await handlers.reply(ctx, f"📨 {receipt}")


register(
    CommandSpec(
        "queue",
        _queue,
        "queue mode and the waiting messages (`queue`, `steer`, `interrupt`, `clear`)",
        scope="thread",
        usage="/queue [" + "|".join(MODES) + "|clear]",
        max_args=1,
        choices=(*MODES, "clear"),
        order=17,
    )
)
register(
    CommandSpec(
        "steer",
        _steer,
        "steer the running turn with this message, whatever the queue mode",
        scope="thread",
        usage="/steer <message>",
        parse=_parse_steer,
        order=18,
    )
)
register_action("queue", _queue_action)
# Continue and Send may start a turn before failing, so a retry could send twice.
register_action("completion", _completion_action, retryable=False)
