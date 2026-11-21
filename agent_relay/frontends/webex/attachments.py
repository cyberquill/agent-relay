"""Files sent to the bot: download them, hold them until their text arrives, say what happened."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

import httpx

from agent_relay.core.inbox import TOP, Inbox, SkipFileError, folder_name, skipped_line
from agent_relay.frontends import commands
from agent_relay.frontends.commands import Command, HelpHint, Prompt
from agent_relay.frontends.webex.api import Message, WebexApi
from agent_relay.log import get_logger
from agent_relay.models.attachments import Received
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

OFF_NOTICE = "📎 Attachments are turned off (`attachments.enabled`). Your message was handled without them."
NO_WORKSPACE_NOTICE = "📎 That session has no workspace folder, so there is nowhere to save files."
PARKED_FILES_TEXT = "Files from the phone:"


@dataclass(frozen=True)
class Intake:
    """What to do with one message: files to deliver with its text, a reply, and whether to run the text.

    `text`, when set, stands in for the message's own text (a files-only reply to a parked session).
    """

    received: Received | None = None
    notice: str | None = None
    handle_text: bool = True
    text: str | None = None


def held_notice(received: Received) -> str:
    count = len(received.attachments)
    return f"📎 Got {count} file{'s' if count != 1 else ''}; they'll go with your next message."


def top_held_notice(received: Received, hold_s: int) -> str:
    count = len(received.attachments)
    wait = f"{hold_s // 60} min" if hold_s >= 60 and hold_s % 60 == 0 else f"{hold_s} s"
    what = "it" if count == 1 else "they"
    return (
        f"📎 Got {count} file{'s' if count != 1 else ''}; {what}'ll go with your next /new (within {wait})."
    )


def _join(lines: list[str]) -> str | None:
    return "\n".join(line for line in lines if line) or None


class AttachmentIntake:
    def __init__(self, api: WebexApi, inbox: Inbox) -> None:
        self._api = api
        self.inbox = inbox

    async def accept(self, message: Message, session: SessionRecord | None, text: str) -> Intake:
        parsed = commands.parse(text, in_thread=session is not None)
        if session is None:
            return await self._top(message, parsed)
        return await self._thread(message, session, parsed, text)

    async def _download(
        self, urls: list[str], workspace: Path | None, folder: str, session_key: str | None
    ) -> tuple[Received, list[str]]:
        """The files saved, and what could not be taken (the caller says so in one `⚠️` line)."""
        saved = []
        skipped: list[str] = []
        for url in urls:
            try:
                async with self._api.download(url, self.inbox.max_bytes) as incoming:
                    saved.append(
                        await self.inbox.store(
                            incoming, workspace=workspace, folder=folder, session_key=session_key
                        )
                    )
            except SkipFileError as exc:
                skipped.append(exc.describe())
            except (httpx.HTTPError, OSError, ValueError) as exc:
                log.warning(f"could not take a phone file: {type(exc).__name__}: {exc}")
                skipped.append("file (could not be downloaded)")
        return Received(attachments=tuple(saved)), skipped

    async def _top(self, message: Message, parsed: commands.Parsed) -> Intake:
        """A file at the top level is held for the next `/new`, never refused."""
        if not message.files:
            return Intake()
        runnable = not isinstance(parsed, HelpHint | Prompt)
        if not self.inbox.enabled:
            return Intake(notice=OFF_NOTICE, handle_text=runnable)
        received, skipped = await self._download(message.files, None, f"top-{secrets.token_hex(4)}", None)
        lines = [skipped_line(skipped)]
        if received.attachments:
            await self.inbox.hold(TOP, received, staged=True)
            if not (isinstance(parsed, Command) and parsed.name == "new"):
                lines.append(top_held_notice(received, self.inbox.hold_s))
        return Intake(notice=_join(lines), handle_text=runnable)

    async def _thread(
        self, message: Message, session: SessionRecord, parsed: commands.Parsed, text: str
    ) -> Intake:
        thread = message.parent_id or ""
        fresh = Received()
        lines: list[str] = []
        if message.files and not self.inbox.enabled:
            lines.append(OFF_NOTICE)
        elif message.files and not session.workspace:
            lines.append(NO_WORKSPACE_NOTICE)
        elif message.files:
            fresh, skipped = await self._download(
                message.files,
                Path(session.workspace or ""),
                folder_name(session.session_key),
                session.session_key,
            )
            lines.append(skipped_line(skipped))
        if isinstance(parsed, Prompt):
            return await self._with_text(thread, fresh, lines)
        if fresh.attachments and not text.strip() and session.status is SessionStatus.PARKED:
            # Hooks cannot carry files: the parked chat gets their paths in its continuation text.
            return await self._with_text(thread, fresh, lines, PARKED_FILES_TEXT)
        if fresh.attachments:
            await self.inbox.hold(thread, fresh)
            lines.append(held_notice(fresh))
        return Intake(notice=_join(lines), handle_text=not isinstance(parsed, HelpHint))

    async def _with_text(
        self, thread: str, fresh: Received, lines: list[str], text: str | None = None
    ) -> Intake:
        held = await self.inbox.take(thread)
        combined = held.merged(fresh) if held else fresh
        return Intake(received=None if combined.empty else combined, notice=_join(lines), text=text)
