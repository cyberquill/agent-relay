"""The last `/new` wizard step: ask what the session should do."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import MAX_PROMPT_CHARS, card, input_text, submit, text


def prompt(carry: dict[str, JsonValue], *, nonce: str) -> dict[str, JsonValue]:
    return card(
        [text("What should it do?", weight="Bolder"), input_text("value", "Prompt", MAX_PROMPT_CHARS)],
        [submit("Start", {"relay": "new", "step": "prompt", "nonce": nonce, **carry}, "positive")],
    )
