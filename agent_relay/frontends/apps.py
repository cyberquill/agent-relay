"""How each app is shown on the phone."""

from __future__ import annotations

from agent_relay.models.events import Harness

APP_EMOJI: dict[Harness, str] = {
    Harness.VSCODE: "🟦",
    Harness.VSCODE_COPILOT: "🟦",
    Harness.COPILOT_CLI: "🟦",
    Harness.CURSOR: "⬛",
    Harness.CURSOR_CLI: "⬛",
    Harness.CURSOR_CDP: "⬛",
    Harness.OPENCODE: "🟩",
}


def app_emoji(harness: Harness) -> str:
    return APP_EMOJI.get(harness, "▫️")
