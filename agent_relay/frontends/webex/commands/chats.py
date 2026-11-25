"""/chats and /takeover: pick up a chat that was started at the desk (D146)."""

from __future__ import annotations

import re

from pydantic import JsonValue

from agent_relay.core.chats import APP_WORDS, DEFAULT_DAYS, MAX_DAYS
from agent_relay.core.service import RelayError, RelayService
from agent_relay.frontends import card_state
from agent_relay.frontends.actions import Outcome, register_action
from agent_relay.frontends.card_state import InvalidInput
from agent_relay.frontends.commands import Command
from agent_relay.frontends.registry import CommandSpec, register
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.handlers import Context, Handlers
from agent_relay.models.events import Harness

# The last list shown, for `/takeover N`: the days first, then one chat reference per number.
LAST_OFFER = "chats.last"
_SPAN = re.compile(r"(\d{1,3})([dw])")


def parse_filters(
    words: tuple[str, ...], workspaces: frozenset[str]
) -> tuple[int, frozenset[Harness] | None, str | None, tuple[str, ...]]:
    """`3d` or `2w` is the age, an app word or a workspace name narrows the list, anything else is text."""
    days: int = DEFAULT_DAYS
    apps: frozenset[Harness] | None = None
    workspace: str | None = None
    rest: list[str] = []
    for word in words:
        span = _SPAN.fullmatch(word.lower())
        if span:
            days = int(span.group(1)) * (7 if span.group(2) == "w" else 1)
        elif word.lower() in APP_WORDS:
            apps = APP_WORDS[word.lower()]
        elif word in workspaces:
            workspace = word
        else:
            rest.append(word)
    if not 1 <= days <= MAX_DAYS:
        raise RelayError(f"Ask for 1 to {MAX_DAYS} days.")
    return days, apps, workspace, tuple(rest)


async def _chats(h: Handlers, ctx: Context, cmd: Command) -> None:
    names = frozenset(w.name for w in h.service.workspaces())
    days, apps, workspace, words = parse_filters(cmd.args, names)
    page = await h.service.chats(days=days, apps=apps, workspace=workspace, words=words)
    if not page.chats:
        outside = (
            f" ({page.outside_workspaces} in folders that are not workspaces; `/ws` adds one)"
            if page.outside_workspaces
            else ""
        )
        await h.reply(ctx, f"No desk chats in the last {page.days} day(s){outside}. Try `/chats 14d`.")
        return
    nonce = await card_state.issue_nonce(h.service.registry)
    offer = (f"days:{page.days}", *(c.ref for c in page.chats))
    await card_state.store_offer(h.service.registry, nonce, offer)
    await card_state.store_offer(h.service.registry, LAST_OFFER, offer)
    await h.card(ctx, f"Desk chats ({len(page.chats)})", cards.chats(page, nonce=nonce))


def _days_of(offer: list[str]) -> int:
    head = offer[0] if offer else ""
    return (
        int(head.removeprefix("days:")) if head.startswith("days:") and head[5:].isdigit() else DEFAULT_DAYS
    )


async def take(h: Handlers, offer: list[str], number: int) -> None:
    """Take over the chat at `number` (1-based) of the offer, and open its thread with the recap."""
    refs = offer[1:]
    if not 1 <= number <= len(refs):
        raise InvalidInput("That is not one of the chats listed.")
    taken = await h.service.take_chat(refs[number - 1], _days_of(offer))
    await h.reply(Context(taken.session), taken.recap)


async def _takeover(h: Handlers, ctx: Context, cmd: Command) -> None:
    if not cmd.args[0].isdigit():
        raise RelayError("Give the number from the list: `/takeover 2`. `/chats` shows the list.")
    offer = await card_state.load_offer(h.service.registry, LAST_OFFER)
    if not offer:
        raise RelayError("No list yet. Send `/chats` first.")
    await take(h, offer, int(cmd.args[0]))


async def _chats_action(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    nonce, pick = inputs.get("nonce"), inputs.get("pick")
    if not isinstance(nonce, str) or not isinstance(pick, str) or not pick.isdigit():
        raise InvalidInput("Pick a chat first.")
    offer = await card_state.load_offer(service.registry, nonce)
    if not offer:
        raise InvalidInput(card_state.EXPIRED)
    await take(handlers, offer, int(pick))
    await card_state.drop_offer(service.registry, nonce)
    return "done"


register_action("chats", _chats_action)

register(
    CommandSpec(
        "chats",
        _chats,
        "desk chats to carry on from the phone (last 7 days; `3d`, `2w`, an app or a word narrows)",
        usage="/chats [days] [app] [workspace] [words]",
        scope="top",
        order=17,
    )
)
register(
    CommandSpec(
        "takeover",
        _takeover,
        "carry on with chat N from the last `/chats` list",
        usage="/takeover <number>",
        aliases=("continue",),
        scope="top",
        min_args=1,
        max_args=1,
        order=18,
    )
)
