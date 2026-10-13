"""Files sent to the bot: download them, hold them until their text arrives, say what happened."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

import httpx

from agent_relay.core.inbox import TOP, Inbox, SkipFileError, folder_name
from agent_relay.frontends import commands
from agent_relay.frontends.commands import Command, HelpHint, Prompt
from agent_relay.frontends.webex.api import Message, WebexApi
from agent_relay.log import get_logger
from agent_relay.models.attachments import Received
from agent_relay.models.sessions import SessionRecord

log = get_logger(__name__)

OFF_NOTICE = "📎 Attachments are turned off (`attachments.enabled`). Your message was handled without them."
TOP_NOTICE = "📎 Send files inside a session thread, or with /new."
NO_WORKSPACE_NOTICE = "📎 That session has no workspace folder, so there is nowhere to save files."


@dataclass(frozen=True)
class Intake:
    """What to do with one message: files to deliver with its text, a reply, and whether to run the text."""

    received: Received | None = None
    notice: str | None = None
    handle_text: bool = True


def held_notice(received: Received) -> str:
    count = len(received.attachments)
    head = f"📎 Got {count} file{'s' if count != 1 else ''}; they'll go with your next message."
    if received.skipped:
        head += f" Skipped {', '.join(received.skipped)}."
    return head


class AttachmentIntake:
    def __init__(self, api: WebexApi, inbox: Inbox) -> None:
        self._api = api
        self.inbox = inbox

    async def accept(self, message: Message, session: SessionRecord | None, text: str) -> Intake:
        parsed = commands.parse(text, in_thread=session is not None)
        if session is None:
            return await self._top(message, parsed)
        return await self._thread(message, session, parsed)

    async def _download(
        self, urls: list[str], workspace: Path | None, folder: str, session_key: str | None
    ) -> Received:
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
        return Received(attachments=tuple(saved), skipped=tuple(skipped))

    async def _top(self, message: Message, parsed: commands.Parsed) -> Intake:
        if not message.files:
            return Intake()
        runnable = not isinstance(parsed, HelpHint | Prompt)
        if not self.inbox.enabled:
            return Intake(notice=OFF_NOTICE, handle_text=runnable)
        if not (isinstance(parsed, Command) and parsed.name == "new"):
            return Intake(notice=TOP_NOTICE, handle_text=runnable)
        received = await self._download(message.files, None, f"top-{secrets.token_hex(4)}", None)
        if not received.empty:
            await self.inbox.hold(TOP, received, staged=True)
        return Intake()

    async def _thread(self, message: Message, session: SessionRecord, parsed: commands.Parsed) -> Intake:
        thread = message.parent_id or ""
        fresh = Received()
        notice: str | None = None
        if message.files and not self.inbox.enabled:
            notice = OFF_NOTICE
        elif message.files and not session.workspace:
            notice = NO_WORKSPACE_NOTICE
        elif message.files:
            fresh = await self._download(
                message.files,
                Path(session.workspace or ""),
                folder_name(session.session_key),
                session.session_key,
            )
        if isinstance(parsed, Prompt):
            held = await self.inbox.take(thread)
            combined = held.merged(fresh) if held else fresh
            return Intake(received=None if combined.empty else combined, notice=notice)
        if not fresh.empty:
            await self.inbox.hold(thread, fresh)
            notice = held_notice(fresh)
        return Intake(notice=notice, handle_text=not isinstance(parsed, HelpHint))
