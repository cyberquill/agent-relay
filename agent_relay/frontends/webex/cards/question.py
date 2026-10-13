"""The question card: pick an option or type an answer, or skip."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import (
    MAX_ANSWER_CHARS,
    card,
    choice_set,
    input_text,
    submit,
    text,
)


def question(
    request_id: str, session_label: str, question_text: str, options: tuple[str, ...], *, nonce: str
) -> dict[str, JsonValue]:
    body: list[JsonValue] = [
        text("Question", weight="Bolder"),
        text(session_label, subtle=True),
        text(question_text),
    ]
    if options:
        body.append(choice_set("choice", [(o, o) for o in options], "expanded"))
    body.append(input_text("custom", "Or type an answer", MAX_ANSWER_CHARS))
    base: dict[str, JsonValue] = {"relay": "question", "request_id": request_id, "nonce": nonce}
    return card(
        body,
        [submit("Answer", {**base, "op": "answer"}, "positive"), submit("Skip", {**base, "op": "skip"})],
    )
