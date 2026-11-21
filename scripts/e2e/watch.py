"""Watch the room (or one thread) for new messages from the relay bot."""

from __future__ import annotations

from collections.abc import Callable

from scripts.e2e.model import Clock, Msg, ScenarioFailed, WebexUser

type Predicate = Callable[[Msg], bool]


class Watch:
    """Polls the room, or one thread, and keeps every bot message that was not there at `start`.

    `fresh=True` starts with nothing seen, for a thread that did not exist a moment ago.
    """

    def __init__(
        self,
        user: WebexUser,
        clock: Clock,
        room_id: str,
        bot_email: str,
        *,
        parent_id: str | None = None,
        poll_s: float = 2.0,
    ) -> None:
        self.user = user
        self.clock = clock
        self.room_id = room_id
        self.bot_email = bot_email.lower()
        self.parent_id = parent_id
        self.poll_s = poll_s
        self.messages: list[Msg] = []
        self._seen: set[str] = set()

    async def _fetch(self) -> list[Msg]:
        listed = await self.user.list_messages(self.room_id, parent_id=self.parent_id, limit=100)
        mine = [m for m in listed if m.person_email.lower() == self.bot_email]
        if self.parent_id is None:  # the room listing may include thread replies
            mine = [m for m in mine if m.parent_id is None]
        return list(reversed(mine))  # the API is newest first

    async def start(self, *, fresh: bool = False) -> Watch:
        self.messages = []
        self._seen = set() if fresh else {m.id for m in await self._fetch()}
        return self

    def ignore(self, message_id: str) -> None:
        """Do not report this message (a card the scenario itself is about to act on)."""
        self._seen.add(message_id)
        self.messages = [m for m in self.messages if m.id != message_id]

    async def poll(self) -> list[Msg]:
        fresh = [m for m in await self._fetch() if m.id not in self._seen]
        for message in fresh:
            self._seen.add(message.id)
        self.messages.extend(fresh)
        return fresh

    async def wait_for(self, predicate: Predicate, timeout_s: float, what: str) -> Msg:
        """The first new message matching `predicate`; a ScenarioFailed after `timeout_s`."""
        deadline = self.clock.now() + timeout_s
        scanned = 0
        while True:
            await self.poll()
            for message in self.messages[scanned:]:
                if predicate(message):
                    return message
            scanned = len(self.messages)
            if self.clock.now() >= deadline:
                got = f" ({len(self.messages)} other message(s) arrived)" if self.messages else ""
                raise ScenarioFailed(f"timed out after {timeout_s:.0f}s waiting for {what}{got}")
            await self.clock.sleep(self.poll_s)


async def settle(watches: list[Watch], clock: Clock, quiet_s: float, poll_s: float) -> None:
    """Keep polling every watch for `quiet_s`, so a stray second message is seen."""
    deadline = clock.now() + quiet_s
    while clock.now() < deadline:
        for watch in watches:
            await watch.poll()
        await clock.sleep(poll_s)
    for watch in watches:
        await watch.poll()
