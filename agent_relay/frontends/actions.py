"""Card submissions (approve, answer, `/new` steps), shared by the Webex and PWA front ends."""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue

from agent_relay.adapters.base import Decision
from agent_relay.core.service import RelayService
from agent_relay.frontends.webex.handlers import Handlers

type Outcome = Literal["done", "already_answered", "ignored"]

DECISIONS: dict[str, Decision] = {"allow": "allow", "allow_session": "allow_session", "deny": "deny"}


def _answer(inputs: dict[str, JsonValue]) -> str | None:
    if inputs.get("op") == "skip":
        return None
    custom, choice = inputs.get("custom"), inputs.get("choice")
    if isinstance(custom, str) and custom.strip():
        return custom
    return choice if isinstance(choice, str) and choice else None


async def submit(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    kind, request_id = inputs.get("relay"), inputs.get("request_id")
    if kind == "approval" and isinstance(request_id, str):
        decision = DECISIONS.get(str(inputs.get("decision")))
        if decision is None:
            return "ignored"
        return "done" if await service.answer_approval(request_id, decision) else "already_answered"
    if kind == "question" and isinstance(request_id, str):
        answered = await service.answer_question(request_id, _answer(inputs))
        return "done" if answered else "already_answered"
    if kind == "new":
        await handlers.new_step(inputs)
        return "done"
    return "ignored"
