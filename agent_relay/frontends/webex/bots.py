"""Which Webex bot serves which app, and what each bot's space may show. Pure, no I/O."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from agent_relay.models.config import CONTROL, BotName
from agent_relay.models.events import Harness
from agent_relay.models.sessions import SessionRecord

APP_HARNESSES: dict[BotName, tuple[Harness, ...]] = {
    "vscode": (Harness.VSCODE, Harness.VSCODE_COPILOT, Harness.COPILOT_CLI),
    "cursor": (Harness.CURSOR, Harness.CURSOR_CLI, Harness.CURSOR_CDP),
    "opencode": (Harness.OPENCODE,),
}
BOT_LABELS: dict[BotName, str] = {
    "control": "Control",
    "vscode": "VS Code",
    "cursor": "Cursor",
    "opencode": "OpenCode",
}


def bot_for_harness(harness: Harness, configured: Collection[BotName]) -> BotName:
    """The configured bot that serves `harness`; the control bot when its app has none."""
    for bot, harnesses in APP_HARNESSES.items():
        if harness in harnesses and bot in configured:
            return bot
    return CONTROL


def scoped(key: str, bot: BotName) -> str:
    """A registry key for one bot's state. The control bot keeps the bare key, so an upgrade keeps working."""
    return key if bot == CONTROL else f"{key}:{bot}"


@dataclass(frozen=True)
class BotScope:
    """What one bot's space serves. `harnesses` of None means every app (the control space)."""

    bot: BotName = CONTROL
    harnesses: frozenset[Harness] | None = None

    @classmethod
    def for_bot(cls, bot: BotName) -> BotScope:
        if bot == CONTROL:
            return cls(bot)
        return cls(bot, frozenset(APP_HARNESSES[bot]))

    def serves(self, harness: Harness) -> bool:
        return self.harnesses is None or harness in self.harnesses

    def keep(self, sessions: Iterable[SessionRecord]) -> list[SessionRecord]:
        return [s for s in sessions if self.serves(s.harness)]
