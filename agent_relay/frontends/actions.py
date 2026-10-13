"""Card submissions, shared by the Webex and PWA front ends.

A submit first spends the card's nonce. Failures show the user a reply; a failure that did nothing
gives the nonce back so the same card can be tapped again. A card kind registers its handler with
`register_action`, usually next to the command that sends the card. A reusable kind (a dashboard)
spends nothing: its nonce works until it is `card_state.REUSABLE_TTL` old.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import JsonValue

from agent_relay.adapters.base import Decision
from agent_relay.core.registry import Registry
from agent_relay.core.service import RelayError, RelayService
from agent_relay.core.service.approvals import REASON_MAX_CHARS
from agent_relay.frontends import card_state
from agent_relay.frontends import registry as command_table
from agent_relay.frontends.card_state import InvalidInput
from agent_relay.frontends.webex.cards.common import MAX_ANSWER_CHARS
from agent_relay.frontends.webex.handlers import Context, Handlers

type Outcome = Literal["done", "already_answered", "ignored", "expired", "failed"]
type ActionHandler = Callable[[RelayService, Handlers, dict[str, JsonValue]], Awaitable[Outcome]]

DECISIONS: dict[str, Decision] = {
    "allow": "allow",
    "allow_session": "allow_session",
    "allow_workspace": "allow_workspace",
    "deny": "deny",
}

_ACTIONS: dict[str, ActionHandler] = {}
# Kinds whose handler may have acted before failing, so a retry could act twice.
_NOT_RETRYABLE: set[str] = set()
# Kinds whose card stays usable after a success, until its nonce expires.
_REUSABLE: set[str] = set()


def register_action(
    kind: str, handler: ActionHandler, *, retryable: bool = True, reusable: bool = False
) -> None:
    if kind in _ACTIONS:
        raise ValueError(f"card action already registered: {kind}")
    _ACTIONS[kind] = handler
    if not retryable:
        _NOT_RETRYABLE.add(kind)
    if reusable:
        _REUSABLE.add(kind)


def action_kinds() -> frozenset[str]:
    command_table.ensure_loaded()
    return frozenset(_ACTIONS)


async def _where(registry: Registry, request_id: object) -> Context:
    """The thread a request belongs to, so replies land next to its card."""
    if isinstance(request_id, str):
        request = await registry.get_pending(request_id)
        session = await registry.get_session(request.session_key) if request else None
        if session is not None:
            return Context(session)
    return Context(None)


async def _answer(registry: Registry, request_id: str, inputs: dict[str, JsonValue]) -> str | None:
    if inputs.get("op") == "skip":
        return None
    custom, choice = inputs.get("custom") or "", inputs.get("choice") or ""
    if not isinstance(custom, str) or not isinstance(choice, str):
        raise InvalidInput("That answer was not text.")
    if len(custom) > MAX_ANSWER_CHARS:
        raise InvalidInput(f"That answer is too long ({MAX_ANSWER_CHARS} characters at most).")
    if choice and choice not in await card_state.load_offer(registry, request_id):
        raise InvalidInput("That choice was not one of the options offered.")
    return custom if custom.strip() else choice or None


async def _approval(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    request_id = inputs.get("request_id")
    if not isinstance(request_id, str):
        return "ignored"
    decision = DECISIONS.get(str(inputs.get("decision")))
    if decision is None:
        raise InvalidInput("That is not a decision this card offers.")
    reason = inputs.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise InvalidInput("That reason was not text.")
    if reason is not None and len(reason) > REASON_MAX_CHARS:
        raise InvalidInput(f"That reason is too long ({REASON_MAX_CHARS} characters at most).")
    # The other buttons submit the reason box too; only a deny carries it.
    note = reason if decision == "deny" else None
    return "done" if await service.answer_approval(request_id, decision, reason=note) else "already_answered"


async def _question(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    request_id = inputs.get("request_id")
    if not isinstance(request_id, str):
        return "ignored"
    if inputs.get("op") not in ("answer", "skip"):
        raise InvalidInput("That is not an action this card offers.")
    request = await service.registry.get_pending(request_id)
    if request is None or request.kind != "question" or request.status != "open":
        return "already_answered"
    answer = await _answer(service.registry, request_id, inputs)
    return "done" if await service.answer_question(request_id, answer) else "already_answered"


async def _new(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    await handlers.new_step(inputs)
    return "done"


register_action("approval", _approval)
register_action("question", _question)
# A `/new` may have started a session before it failed.
register_action("new", _new, retryable=False)


async def submit(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    kind = inputs.get("relay")
    if not isinstance(kind, str) or kind not in action_kinds():
        return "ignored"
    registry = service.registry
    where = await _where(registry, inputs.get("request_id"))
    nonce = inputs.get("nonce")
    reusable = kind in _REUSABLE
    issued = (
        await card_state.check_nonce(registry, nonce)
        if reusable
        else await card_state.consume_nonce(registry, nonce)
    )
    if issued is None:
        await handlers.reply(where, card_state.EXPIRED)
        return "expired"
    try:
        return await _ACTIONS[kind](service, handlers, inputs)
    except RelayError as exc:
        if not reusable:
            await card_state.restore_nonce(registry, nonce, issued)
        await handlers.reply(where, f"⚠️ {exc}")
        return "failed"
    except Exception:
        if not reusable and kind not in _NOT_RETRYABLE:
            await card_state.restore_nonce(registry, nonce, issued)
        raise
