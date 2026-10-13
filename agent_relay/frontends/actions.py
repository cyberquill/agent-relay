"""Card submissions (approve, answer, `/new` steps), shared by the Webex and PWA front ends.

A submit first spends the card's nonce. Failures show the user a reply; a failure that did nothing
gives the nonce back so the same card can be tapped again.
"""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue

from agent_relay.adapters.base import Decision
from agent_relay.core.registry import Registry
from agent_relay.core.service import RelayError, RelayService
from agent_relay.frontends import card_state
from agent_relay.frontends.card_state import InvalidInput
from agent_relay.frontends.webex.cards.common import MAX_ANSWER_CHARS
from agent_relay.frontends.webex.handlers import Context, Handlers

type Outcome = Literal["done", "already_answered", "ignored", "expired", "failed"]

DECISIONS: dict[str, Decision] = {"allow": "allow", "allow_session": "allow_session", "deny": "deny"}
KINDS = ("approval", "question", "new")


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


async def _act(
    service: RelayService, handlers: Handlers, kind: object, inputs: dict[str, JsonValue]
) -> Outcome:
    request_id = inputs.get("request_id")
    if kind == "approval" and isinstance(request_id, str):
        decision = DECISIONS.get(str(inputs.get("decision")))
        if decision is None:
            raise InvalidInput("That is not a decision this card offers.")
        return "done" if await service.answer_approval(request_id, decision) else "already_answered"
    if kind == "question" and isinstance(request_id, str):
        if inputs.get("op") not in ("answer", "skip"):
            raise InvalidInput("That is not an action this card offers.")
        request = await service.registry.get_pending(request_id)
        if request is None or request.kind != "question" or request.status != "open":
            return "already_answered"
        answer = await _answer(service.registry, request_id, inputs)
        return "done" if await service.answer_question(request_id, answer) else "already_answered"
    if kind == "new":
        await handlers.new_step(inputs)
        return "done"
    return "ignored"


async def submit(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    kind = inputs.get("relay")
    if kind not in KINDS:
        return "ignored"
    registry = service.registry
    where = await _where(registry, inputs.get("request_id"))
    nonce = inputs.get("nonce")
    issued = await card_state.consume_nonce(registry, nonce)
    if issued is None:
        await handlers.reply(where, card_state.EXPIRED)
        return "expired"
    try:
        return await _act(service, handlers, kind, inputs)
    except RelayError as exc:
        await card_state.restore_nonce(registry, nonce, issued)
        await handlers.reply(where, f"⚠️ {exc}")
        return "failed"
    except Exception:
        # A `/new` may have started a session before it failed, so only idempotent answers can be retried.
        if kind != "new":
            await card_state.restore_nonce(registry, nonce, issued)
        raise
