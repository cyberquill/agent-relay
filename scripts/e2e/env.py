"""What every scenario shares: posting, watching, and the "exactly one message" check."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from scripts.e2e.cardkit import is_approval_card, is_receipt
from scripts.e2e.model import (
    BudgetExceeded,
    Clock,
    Msg,
    Person,
    Redactor,
    ScenarioFailed,
    WebexUser,
)
from scripts.e2e.watch import Watch, settle

OK_PROMPT = "Reply with exactly: OK"
OK_WORD = re.compile(r"^\s*OK\b")
SESSION_CAP = 3
SUMMARY_CHARS = 70


@dataclass(frozen=True)
class Settings:
    command_s: float = 30.0  # a command's reply
    card_s: float = 45.0  # a card appearing, or disappearing
    turn_s: float = 240.0  # a turn's final message
    quiet_s: float = 8.0  # how long to keep watching for a second message
    poll_s: float = 2.0
    away_wait_s: float = 180.0  # for the human's desk turn
    scenario_max_s: float = 1_200.0


class SessionBudget:
    """At most `limit` live sessions per adapter in one run (AGENTS.md)."""

    def __init__(self, limit: int = SESSION_CAP) -> None:
        self.limit = limit
        self.used: dict[str, int] = {}

    def check(self, app: str) -> None:
        if self.used.get(app, 0) >= self.limit:
            raise BudgetExceeded(f"session cap reached for {app}: {self.limit} per run")

    def charge(self, app: str) -> None:
        self.check(app)
        self.used[app] = self.used.get(app, 0) + 1


@dataclass(frozen=True)
class Thread:
    app: str
    root: Msg


@dataclass(frozen=True)
class TurnResult:
    final: Msg
    thread: list[Msg]


@dataclass
class Env:
    user: WebexUser
    clock: Clock
    room_id: str
    bot_email: str
    me: Person
    settings: Settings
    budget: SessionBudget
    redact: Redactor
    say: Callable[[str], None]
    tmp_dir: Path
    can_submit: bool = True
    posted: list[str] = field(default_factory=list)
    threads: dict[str, Thread] = field(default_factory=dict)

    def watch(self, parent_id: str | None = None) -> Watch:
        return Watch(
            self.user,
            self.clock,
            self.room_id,
            self.bot_email,
            parent_id=parent_id,
            poll_s=self.settings.poll_s,
        )

    async def post(self, text: str, *, parent_id: str | None = None, file: Path | None = None) -> Msg:
        """Send as the owner; the id is remembered so the run deletes it at the end."""
        message = await self.user.send(self.room_id, text, parent_id=parent_id, file=file)
        self.posted.append(message.id)
        return message

    async def settle(self, watches: list[Watch]) -> None:
        await settle(watches, self.clock, self.settings.quiet_s, self.settings.poll_s)

    def summary(self, message: Msg) -> str:
        if message.attachments:
            return "a card"
        text = " ".join(message.body.split())
        clipped = text if len(text) <= SUMMARY_CHARS else f"{text[: SUMMARY_CHARS - 1]}…"
        return self.redact(clipped)

    async def card_deleted(self, message_id: str) -> None:
        """The card message is gone (the relay deletes a one-shot card after use)."""
        deadline = self.clock.now() + self.settings.card_s
        while await self.user.get(message_id) is not None:
            if self.clock.now() >= deadline:
                raise ScenarioFailed(f"the card was not deleted within {self.settings.card_s:.0f}s")
            await self.clock.sleep(self.settings.poll_s)

    async def root_text(self, root: Msg, until: Callable[[str], bool], what: str) -> str:
        """The root message's current text once `until` holds (the relay edits it silently)."""
        deadline = self.clock.now() + self.settings.card_s
        text = ""
        while True:
            current = await self.user.get(root.id)
            text = current.body if current is not None else ""
            if current is not None and until(text):
                return text
            if self.clock.now() >= deadline:
                raise ScenarioFailed(f"the thread root never showed {what}; it reads: {self.summary(root)}")
            await self.clock.sleep(self.settings.poll_s)

    def expect_clean(self, thread: list[Msg], top: list[Msg]) -> Msg:
        """One answer in the thread, nothing at the top, no approval card anywhere; returns the answer.

        A file receipt ("📎 ...") is the reply to the user's own upload, so it is not counted."""
        thread = [m for m in thread if not is_receipt(m)]
        for message in [*thread, *top]:
            if is_approval_card(message):
                raise ScenarioFailed("the relay posted an approval card")
        if len(thread) != 1:
            shown = "; ".join(self.summary(m) for m in thread) or "none"
            raise ScenarioFailed(f"{len(thread)} messages in the thread, expected exactly one ({shown})")
        if top:
            shown = "; ".join(self.summary(m) for m in top)
            raise ScenarioFailed(f"{len(top)} unexpected top-level message(s): {shown}")
        final = thread[0]
        if final.body.lstrip().startswith("⚠️"):
            raise ScenarioFailed(f"the turn failed: {self.summary(final)}")
        return final

    async def turn(self, thread: Thread, send: Callable[[], Awaitable[object]]) -> TurnResult:
        """Run `send`, then expect exactly one final message in the thread and nothing at the top."""
        replies = await self.watch(thread.root.id).start()
        top = await self.watch().start()
        await send()
        await replies.wait_for(lambda m: not is_receipt(m), self.settings.turn_s, "the turn's final message")
        await self.settle([replies, top])
        final = self.expect_clean(replies.messages, top.messages)
        return TurnResult(final, list(replies.messages))
