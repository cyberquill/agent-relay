"""Read an Adaptive Card the relay posted and build the `inputs` a tap on it would submit.

Webex delivers a submit as the Action.Submit `data` merged with every input's value, so the driver does
the same: the action's data first, the inputs on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from scripts.e2e.model import Msg, ScenarioFailed

CARD_TYPE = "application/vnd.microsoft.card.adaptive"
_APPROVAL_WORDS = re.compile(r"\b(allow|deny|approve)\b", re.I)


class CardMismatch(ScenarioFailed):
    """The card is not shaped the way the scenario needs."""


@dataclass(frozen=True)
class Choice:
    title: str
    value: str


@dataclass(frozen=True)
class InputSpec:
    id: str
    kind: Literal["text", "choice", "toggle"]
    value: str
    choices: tuple[Choice, ...] = ()
    required: bool = False


@dataclass(frozen=True)
class SubmitAction:
    title: str
    data: dict[str, JsonValue]


@dataclass
class CardView:
    inputs: list[InputSpec] = field(default_factory=list)
    actions: list[SubmitAction] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)

    def input(self, input_id: str) -> InputSpec | None:
        return next((i for i in self.inputs if i.id == input_id), None)

    def kind_of(self) -> str | None:
        for action in self.actions:
            relay = action.data.get("relay")
            if isinstance(relay, str):
                return relay
        return None

    def action(self, **match: str) -> SubmitAction | None:
        for action in self.actions:
            if all(action.data.get(k) == v for k, v in match.items()):
                return action
        return None


def _str(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _walk(node: JsonValue, view: CardView) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, view)
        return
    if not isinstance(node, dict):
        return
    kind = _str(node.get("type"))
    if kind == "TextBlock":
        view.texts.append(_str(node.get("text")))
    elif kind == "Input.Text":
        view.inputs.append(
            InputSpec(
                _str(node.get("id")), "text", _str(node.get("value")), required=node.get("isRequired") is True
            )
        )
    elif kind == "Input.Toggle":
        view.inputs.append(InputSpec(_str(node.get("id")), "toggle", _str(node.get("value"))))
    elif kind == "Input.ChoiceSet":
        raw = node.get("choices")
        choices = (
            tuple(Choice(_str(c.get("title")), _str(c.get("value"))) for c in raw if isinstance(c, dict))
            if isinstance(raw, list)
            else ()
        )
        view.inputs.append(
            InputSpec(
                _str(node.get("id")),
                "choice",
                _str(node.get("value")),
                choices,
                required=node.get("isRequired") is True,
            )
        )
    elif kind == "Action.Submit":
        data = node.get("data")
        view.actions.append(SubmitAction(_str(node.get("title")), data if isinstance(data, dict) else {}))
    for key in ("body", "items", "columns", "actions", "card"):
        _walk(node.get(key), view)


def card_of(message: Msg) -> CardView | None:
    """The first Adaptive Card on the message, or None."""
    for attachment in message.attachments:
        if attachment.get("contentType") == CARD_TYPE:
            view = CardView()
            _walk(attachment.get("content"), view)
            return view
    return None


def has_card(message: Msg, kind: str) -> bool:
    view = card_of(message)
    return view is not None and view.kind_of() == kind


def is_approval_card(message: Msg) -> bool:
    """A card that asks to allow or deny something: the relay must never post one."""
    view = card_of(message)
    if view is None:
        return False
    if view.kind_of() == "approval":
        return True
    return any(_APPROVAL_WORDS.search(a.title) for a in view.actions)


def build_inputs(
    view: CardView, action: SubmitAction, overrides: dict[str, str] | None = None
) -> dict[str, JsonValue]:
    """What a tap on `action` submits: its data, then each input's default, then `overrides`."""
    chosen = dict(overrides or {})
    unknown = sorted(set(chosen) - {i.id for i in view.inputs})
    if unknown:
        raise CardMismatch(f"the card has no input {', '.join(unknown)}")
    inputs: dict[str, JsonValue] = dict(action.data)
    for spec in view.inputs:
        inputs[spec.id] = chosen.get(spec.id, spec.value)
    for spec in view.inputs:
        value = inputs[spec.id]
        if spec.kind == "choice" and spec.choices and value not in {c.value for c in spec.choices}:
            raise CardMismatch(f"`{value}` is not one of the choices of input {spec.id}")
        if spec.required and not value:
            raise CardMismatch(f"input {spec.id} is required and empty")
    return inputs


def _is_scratch(choice: Choice) -> bool:
    value = choice.value.rstrip("/")
    name = value.rsplit("/", 1)[-1].lower()
    return name in ("scratch", ".scratch") or choice.title.lower().lstrip().startswith("scratch")


def pick_workspace(spec: InputSpec) -> str:
    """The `scratch` workspace, exact name first."""
    exact = [c for c in spec.choices if c.value.lower() == "scratch"]
    found = exact or [c for c in spec.choices if _is_scratch(c)]
    if not found:
        raise CardMismatch("the workspace list has no `scratch` entry")
    return found[0].value


def pick_agent(spec: InputSpec, harness: str) -> str:
    """The `<harness>:default` entry, else the first `<harness>:<model>` one."""
    mine = [c for c in spec.choices if c.value.split(":", 1)[0] == harness]
    if not mine:
        raise CardMismatch(f"the agent list has no {harness} entry")
    for choice in mine:
        if choice.value == f"{harness}:default":
            return choice.value
    return mine[0].value


def fill_new(view: CardView, *, harness: str, prompt: str) -> dict[str, JsonValue]:
    """Inputs for the `/new` card: workspace `scratch`, the app's default model, and the prompt."""
    action = view.action(relay="new")
    if action is None:
        raise CardMismatch("the /new card has no Start action")
    choice_inputs = [i for i in view.inputs if i.kind == "choice"]
    agent = next((i for i in choice_inputs if any(":" in c.value for c in i.choices)), None)
    workspace = next((i for i in choice_inputs if i is not agent), None)
    prompt_input = next((i for i in view.inputs if i.kind == "text"), None)
    if agent is None or workspace is None or prompt_input is None:
        raise CardMismatch("the /new card needs a workspace list, an agent list and a prompt box")
    chosen = {
        workspace.id: pick_workspace(workspace),
        agent.id: pick_agent(agent, harness),
        prompt_input.id: prompt,
    }
    return build_inputs(view, action, chosen)


def is_receipt(message: Msg) -> bool:
    """The relay's one-line acknowledgement of a file the owner sent."""
    return message.body.lstrip().startswith("📎")
