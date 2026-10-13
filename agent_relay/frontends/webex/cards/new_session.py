"""`/new` as one card (workspace, prompt, away, then one ShowCard per app), or two with `layout == "two"`."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import (
    MAX_ACTIONS_ON_CARD,
    MAX_CHOICES,
    MAX_PROMPT_CHARS,
    action_set,
    card,
    choice_set,
    input_text,
    input_toggle,
    show_card,
    submit,
    text,
)

AWAY_TOGGLE = "Away mode on for this workspace"
AWAY_NOTE = "Leave it off to keep the current away setting."
PREFILL_MAX_CHARS = 4_000


@dataclass(frozen=True)
class AppOption:
    """One app on the card. `models` is (value, label) pairs; empty means the app picks its own."""

    harness: str
    label: str
    emoji: str
    models: tuple[tuple[str, str], ...] = ()
    model: str | None = None


def _prompt_input(prompt: str | None) -> list[JsonValue]:
    long = prompt is not None and len(prompt) > PREFILL_MAX_CHARS
    body: list[JsonValue] = [text("Prompt", weight="Bolder")]
    if long:
        body.append(text("Your prompt was too long to prefill. Paste it again.", subtle=True))
    body.append(input_text("prompt", "What should it do?", MAX_PROMPT_CHARS, None if long else prompt))
    return body


def _common(workspaces: list[tuple[str, str]], workspace: str | None, prompt: str | None) -> list[JsonValue]:
    body: list[JsonValue] = [
        text("New session", weight="Bolder"),
        text("Workspace", weight="Bolder"),
        choice_set("ws", workspaces, "compact", workspace),
    ]
    if len(workspaces) > MAX_CHOICES:
        body.append(
            text(f"Showing {MAX_CHOICES} of {len(workspaces)}. `/new <app> <ws>` picks any.", subtle=True)
        )
    body += _prompt_input(prompt)
    body += [input_toggle("away", AWAY_TOGGLE), text(AWAY_NOTE, subtle=True)]
    return body


def _app_card(option: AppOption, nonce: str) -> JsonValue:
    body: list[JsonValue] = []
    if option.models:
        body += [
            text("Model", weight="Bolder"),
            choice_set(f"model_{option.harness}", list(option.models), "compact", option.model),
        ]
        if len(option.models) > MAX_CHOICES:
            body.append(
                text(
                    f"Showing {MAX_CHOICES} of {len(option.models)}. `/new` with a model picks any.",
                    subtle=True,
                )
            )
    else:
        body.append(text("This app picks its own model.", subtle=True))
    data: dict[str, JsonValue] = {"relay": "new", "step": "start", "app": option.harness, "nonce": nonce}
    return show_card(f"{option.emoji} {option.label}", body, [submit("Start", data, "positive")])


def new_session(
    workspaces: list[tuple[str, str]],
    workspace: str | None,
    prompt: str | None,
    apps: list[AppOption],
    *,
    nonce: str,
) -> dict[str, JsonValue]:
    """Everything on one card. Each app's ShowCard holds its model list and a Start button."""
    body = _common(workspaces, workspace, prompt)
    shows = [_app_card(option, nonce) for option in apps]
    if len(shows) > MAX_ACTIONS_ON_CARD:
        body += [text("Pick an app to choose its model and start:", subtle=True), action_set(shows)]
        return card(body, [])
    body.append(text("Pick an app to choose its model and start:", subtle=True))
    return card(body, shows)


def new_details(
    workspaces: list[tuple[str, str]],
    workspace: str | None,
    prompt: str | None,
    apps: list[AppOption],
    app: str | None,
    *,
    nonce: str,
) -> dict[str, JsonValue]:
    """Card 1 of 2: app, workspace, prompt and the away toggle."""
    choices = [(a.harness, f"{a.emoji} {a.label}") for a in apps]
    body: list[JsonValue] = [
        text("New session (1 of 2)", weight="Bolder"),
        text("App", weight="Bolder"),
        choice_set("app", choices, "compact", app or (choices[0][0] if choices else None)),
        *_common(workspaces, workspace, prompt)[1:],
    ]
    data: dict[str, JsonValue] = {"relay": "new", "step": "details", "nonce": nonce}
    return card(body, [submit("Next", data, "positive")])


def new_model(option: AppOption, *, nonce: str) -> dict[str, JsonValue]:
    """Card 2 of 2: the model, then Start."""
    body: list[JsonValue] = [
        text(f"New session (2 of 2): {option.emoji} {option.label}", weight="Bolder"),
        text("Model", weight="Bolder"),
        choice_set("model", list(option.models), "compact", option.model),
    ]
    data: dict[str, JsonValue] = {"relay": "new", "step": "go", "nonce": nonce}
    return card(body, [submit("Start", data, "positive")])
