"""The chats found in the apps' own stores, newest first, limited to the registered workspaces."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from agent_relay import paths
from agent_relay.core.chats import copilot, cursor, opencode, vscode
from agent_relay.core.chats.model import ChatPage, DeskChat
from agent_relay.log import get_logger
from agent_relay.models.config import Workspace
from agent_relay.models.events import Harness

log = get_logger(__name__)

DEFAULT_DAYS = 7
MAX_DAYS = 90
LISTED = 10
READ_TIMEOUT_S = 8.0

type Reader = Callable[[float, Sequence[Path], float], list[DeskChat]]
type Clock = Callable[[], float]

READERS: tuple[tuple[str, Reader], ...] = (
    ("Cursor", cursor.read),
    ("Copilot", copilot.read),
    ("VS Code", vscode.read),
    ("OpenCode", opencode.read),
)
# What the phone may type to narrow the list to one app.
APP_WORDS: dict[str, frozenset[Harness]] = {
    "cursor": frozenset({Harness.CURSOR, Harness.CURSOR_CLI}),
    "copilot": frozenset({Harness.VSCODE_COPILOT, Harness.COPILOT_CLI}),
    "vscode": frozenset({Harness.VSCODE, Harness.VSCODE_COPILOT}),
    "opencode": frozenset({Harness.OPENCODE}),
}


def _root(workspace: Workspace) -> Path:
    return paths.expand_user(workspace.path).resolve()


def _inside(folder: str, roots: Sequence[Path]) -> Path | None:
    if not folder:
        return None
    target = paths.expand_user(folder).resolve()
    return next((r for r in roots if target == r or r in target.parents), None)


class ChatCatalog:
    def __init__(self, readers: Sequence[tuple[str, Reader]] = READERS, clock: Clock = time.time) -> None:
        self._readers = readers
        self._clock = clock

    async def scan(self, days: int, roots: Sequence[Path]) -> list[DeskChat]:
        """Every chat touched in the last `days` days, in any folder; a slow or broken store is skipped."""
        now = self._clock()
        since = now - days * 86400

        async def one(label: str, reader: Reader) -> list[DeskChat]:
            try:
                return await asyncio.wait_for(asyncio.to_thread(reader, since, roots, now), READ_TIMEOUT_S)
            except TimeoutError:
                log.warning(f"reading {label} chats took over {READ_TIMEOUT_S:.0f}s; skipped")
            except Exception:
                log.exception(f"reading {label} chats failed")
            return []

        batches = await asyncio.gather(*(one(label, reader) for label, reader in self._readers))
        return sorted((c for batch in batches for c in batch), key=lambda c: c.modified, reverse=True)

    async def page(
        self,
        workspaces: Sequence[Workspace],
        *,
        days: int = DEFAULT_DAYS,
        apps: frozenset[Harness] | None = None,
        workspace: str | None = None,
        words: Sequence[str] = (),
        limit: int = LISTED,
    ) -> ChatPage:
        days = max(1, min(days, MAX_DAYS))
        roots = [_root(w) for w in workspaces]
        chosen = [_root(w) for w in workspaces if workspace in (None, w.name)]
        everything = await self.scan(days, roots)
        inside = [c for c in everything if _inside(c.workspace, roots) is not None]
        shown = [
            c
            for c in inside
            if _inside(c.workspace, chosen) is not None
            and (apps is None or c.app in apps)
            and _matches(c, words)
        ]
        return ChatPage(
            chats=shown[:limit],
            outside_workspaces=len(everything) - len(inside),
            days=days,
            more=max(len(shown) - limit, 0),
        )

    async def find(self, ref: str, workspaces: Sequence[Workspace], days: int) -> DeskChat | None:
        """The chat an offer named, read again now (it may have moved on since the list was shown)."""
        roots = [_root(w) for w in workspaces]
        found = next((c for c in await self.scan(max(days, 1), roots) if c.ref == ref), None)
        return found if found is not None and _inside(found.workspace, roots) is not None else None


def _matches(chat: DeskChat, words: Sequence[str]) -> bool:
    haystack = " ".join(
        (chat.title, chat.last_prompt or "", chat.last_reply or "", Path(chat.workspace).name)
    ).lower()
    return all(w.lower() in haystack for w in words)


def ago(moment: datetime, now: datetime | None = None) -> str:
    seconds = int(((now or datetime.now(UTC)) - moment).total_seconds())
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 129600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} d ago"
