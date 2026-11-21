"""`/new` as one flat card: workspace, agent (app and model in one list), prompt, Start.

No Action.ShowCard and no input that depends on another, so the card renders the same on every client.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import (
    MAX_CHOICES,
    MAX_PROMPT_CHARS,
    card,
    choice_set,
    input_text,
    submit,
    text,
)

AGENT_LIMIT = 40
PREFILL_MAX_CHARS = 4_000
PROMPT_REQUIRED = "Say what the session should do."


def attached_line(files: int) -> str:
    return f"📎 {files} file{'' if files == 1 else 's'} will be attached"


def new_session(
    workspaces: list[tuple[str, str]],
    workspace: str | None,
    agents: list[tuple[str, str]],
    agent: str | None,
    prompt: str | None,
    *,
    nonce: str,
    unavailable: Sequence[str] = (),
    files: int = 0,
) -> dict[str, JsonValue]:
    """`workspaces` and `agents` are (value, title) pairs; an agent value is `<harness>:<model>`."""
    long = prompt is not None and len(prompt) > PREFILL_MAX_CHARS
    body: list[JsonValue] = [text("New session", weight="Bolder")]
    if files:
        body.append(text(attached_line(files), subtle=True))
    body.append(
        choice_set(
            "ws", workspaces, "compact", workspace, label="Workspace", required_message="Pick a workspace."
        )
    )
    if len(workspaces) > MAX_CHOICES:
        body.append(
            text(f"Showing {MAX_CHOICES} of {len(workspaces)}. `/new <app> <ws>` picks any.", subtle=True)
        )
    body.append(
        choice_set(
            "agent",
            agents,
            "compact",
            agent,
            limit=AGENT_LIMIT,
            label="Agent",
            required_message="Pick an agent.",
        )
    )
    if unavailable:
        body.append(text(f"Not available: {', '.join(unavailable)}", subtle=True))
    if long:
        body.append(text("Your prompt was too long to prefill. Paste it again.", subtle=True))
    body.append(
        input_text(
            "prompt",
            "What should it do?",
            MAX_PROMPT_CHARS,
            None if long else prompt,
            label="Prompt",
            required_message=PROMPT_REQUIRED,
        )
    )
    data: dict[str, JsonValue] = {"relay": "new", "nonce": nonce}
    return card(body, [submit("Start", data, "positive")])
