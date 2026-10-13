"""The question card: pick an option or type an answer, or skip."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import card, submit, text


def question(
    request_id: str, session_label: str, question_text: str, options: tuple[str, ...]
) -> dict[str, JsonValue]:
    body: list[JsonValue] = [
        text("Question", weight="Bolder"),
        text(session_label, subtle=True),
        text(question_text),
    ]
    if options:
        body.append(
            {
                "type": "Input.ChoiceSet",
                "id": "choice",
                "style": "expanded",
                "choices": [{"title": o, "value": o} for o in options],
            }
        )
    body.append(
        {"type": "Input.Text", "id": "custom", "isMultiline": True, "placeholder": "Or type an answer"}
    )
    base: dict[str, JsonValue] = {"relay": "question", "request_id": request_id}
    return card(
        body,
        [submit("Answer", {**base, "op": "answer"}, "positive"), submit("Skip", {**base, "op": "skip"})],
    )
