"""Taking over a chat that was started at the desk (D146).

The phone can carry on with a desk chat in one of three ways, chosen per chat:

- **drive**: the app's own API addresses the chat by id, so the phone sends its next turn
  (VS Code Copilot through the Agent Host, the Cursor CLI over ACP, OpenCode through `opencode serve`);
- **arm**: the chat is running at the desk right now, so its next stop hook parks it and the phone's
  reply continues the same turn (every app whose hooks the relay installs);
- **handoff**: the app has no way to address the chat, so a new phone session in the same workspace
  reads its transcript and carries on. It is labelled as new, never as a continuation.

A chat the relay cannot continue in place is never refused for that: it falls back to a handoff and
says why.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from agent_relay.adapters.base import Adapter, AdapterUnavailableError, UnsupportedError
from agent_relay.core import away
from agent_relay.core.chats import DEFAULT_DAYS, MAX_DAYS, ChatCatalog, ChatPage, DeskChat, TakeMode, ago
from agent_relay.core.chats import text as chat_text
from agent_relay.core.service.common import Deps, RelayError
from agent_relay.core.service.sessions import Sessions
from agent_relay.log import get_logger
from agent_relay.models.capabilities import Capability
from agent_relay.models.events import Harness, Origin, SessionStarted
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import load_workspaces, workspace_for_path

log = get_logger(__name__)

TAKE_MODE = "take_mode"
HANDOFF_OF = "handoff_of"
# Apps whose hooks the relay installs: a running chat there parks at its next stop.
HOOKED_APPS = frozenset({Harness.CURSOR, Harness.VSCODE, Harness.VSCODE_COPILOT, Harness.COPILOT_CLI})
# Apps whose own API can send a finished chat its next turn.
DRIVEN_APPS = frozenset({Harness.VSCODE_COPILOT, Harness.CURSOR_CLI, Harness.OPENCODE})
# The app a handoff starts its new session in, for a chat that cannot be started from.
HANDOFF_IN: dict[Harness, Harness] = {
    Harness.COPILOT_CLI: Harness.VSCODE_COPILOT,
    Harness.CURSOR: Harness.CURSOR_CLI,
}
FAMILIES: dict[Harness, tuple[Harness, ...]] = {
    Harness.VSCODE_COPILOT: (Harness.VSCODE_COPILOT, Harness.COPILOT_CLI),
    Harness.COPILOT_CLI: (Harness.COPILOT_CLI, Harness.VSCODE_COPILOT),
    Harness.CURSOR: (Harness.CURSOR, Harness.CURSOR_CLI),
    Harness.CURSOR_CLI: (Harness.CURSOR_CLI, Harness.CURSOR),
}
LABELS: dict[Harness, str] = {
    Harness.VSCODE_COPILOT: "VS Code Copilot",
    Harness.VSCODE: "VS Code chat",
    Harness.COPILOT_CLI: "Copilot CLI",
    Harness.CURSOR: "Cursor",
    Harness.CURSOR_CLI: "Cursor CLI",
    Harness.OPENCODE: "OpenCode",
}
RECAP_PROMPT_CHARS = 300
RECAP_REPLY_CHARS = 1200
HANDOFF_PROMPT_CHARS = 1500
HANDOFF_REPLY_CHARS = 3000


@dataclass(frozen=True)
class Taken:
    """What a takeover did: the session the thread belongs to, how, and the lines to show first."""

    session: SessionRecord
    mode: TakeMode
    chat: DeskChat
    recap: str


def label_of(app: Harness) -> str:
    return LABELS.get(app, app.value)


def _clip(text: str | None, size: int) -> str:
    return chat_text.short(text, size) if text else ""


def recap_markdown(chat: DeskChat, mode: TakeMode, why: str | None) -> str:
    """The first message in the thread: where the chat stands, and what a reply there will do."""
    lines = [f"📲 **Picked up from the desk** · {label_of(chat.app)} · {ago(chat.modified)}"]
    if chat.last_prompt:
        lines.append(f"**You asked:** {_clip(chat.last_prompt, RECAP_PROMPT_CHARS)}")
    if chat.last_reply:
        lines.append(f"**It said:** {_clip(chat.last_reply, RECAP_REPLY_CHARS)}")
    match mode:
        case "drive":
            lines.append("Reply here to send this chat its next turn. `/back` hands it back to the desk.")
        case "arm":
            lines.append(
                "It is running at the desk. When this turn ends the chat holds (🅿️) and your reply here "
                "continues it. `/back` lets go."
            )
        case _:
            lines.append(
                f"{why or 'This app cannot be continued in place.'} This is a **new** session in the same "
                "workspace, reading the chat and summarising where it stands. The desk chat is untouched."
            )
    return "\n\n".join(lines)


def handoff_prompt(chat: DeskChat) -> str:
    where = (
        f"Its transcript is {chat.transcript} (read it, never modify it)."
        if chat.transcript
        else f"{label_of(chat.app)} keeps it in its own database; the `agent-relay-recall` skill reads it."
    )
    parts = [
        f"You are carrying on from an earlier {label_of(chat.app)} chat in this workspace.",
        f"Its title was: {chat.title}",
        where,
    ]
    if chat.last_prompt:
        parts.append(f"The last thing asked in it: {_clip(chat.last_prompt, HANDOFF_PROMPT_CHARS)}")
    if chat.last_reply:
        parts.append(f"The last thing it answered: {_clip(chat.last_reply, HANDOFF_REPLY_CHARS)}")
    parts.append(
        "Read the end of the transcript if you need more. Then say in three sentences or fewer where things "
        "stand and what you would do next, and wait for my instructions. Change nothing yet."
    )
    return "\n\n".join(parts)


class Takeover:
    def __init__(self, deps: Deps, sessions: Sessions, catalog: ChatCatalog | None = None) -> None:
        self._deps = deps
        self._sessions = sessions
        self._catalog = catalog or ChatCatalog()

    async def chats(
        self,
        *,
        days: int = DEFAULT_DAYS,
        apps: frozenset[Harness] | None = None,
        workspace: str | None = None,
        words: tuple[str, ...] = (),
    ) -> ChatPage:
        if not 1 <= days <= MAX_DAYS:
            raise RelayError(f"Ask for 1 to {MAX_DAYS} days.")
        workspaces = load_workspaces().workspaces
        if workspace is not None and workspace not in {w.name for w in workspaces}:
            raise RelayError(f"Unknown workspace `{workspace}`. Try `/ws`.")
        return await self._catalog.page(workspaces, days=days, apps=apps, workspace=workspace, words=words)

    async def take(self, ref: str, days: int = DEFAULT_DAYS) -> Taken:
        workspaces = load_workspaces().workspaces
        chat = await self._catalog.find(ref, workspaces, days)
        if chat is None:
            raise RelayError("That chat is not in the list any more. Send `/chats` again.")
        adapter = self._deps.router.adapter(chat.app)
        why: str | None = None
        mode = self._plan(chat, adapter)
        session: SessionRecord | None = None
        if mode == "arm":
            session = await self._arm(chat)
        elif mode == "drive":
            try:
                session = await self._drive(chat, adapter)
            except (AdapterUnavailableError, UnsupportedError, RelayError) as exc:
                why = f"{exc}."
                mode = "handoff"
                log.info(f"{ref} cannot be driven in place ({exc}); handing off")
        if session is None:
            session = await self._handoff(chat, why)
        else:
            await self._announce(session)
        return Taken(session, mode, chat, recap_markdown(chat, mode, why))

    # choosing a way

    @staticmethod
    def _plan(chat: DeskChat, adapter: Adapter | None) -> TakeMode:
        if chat.running:
            if chat.app in HOOKED_APPS:
                return "arm"
            raise RelayError(
                f"That {label_of(chat.app)} chat is running at the desk now. Ask again when its turn ends."
            )
        if chat.app in DRIVEN_APPS and adapter is not None and adapter.capabilities.has(Capability.ADOPT):
            return "drive"
        return "handoff"

    async def _existing(self, chat: DeskChat) -> SessionRecord | None:
        for harness in FAMILIES.get(chat.app, (chat.app,)):
            found = await self._deps.registry.find_session(harness, chat.native_id)
            if found is not None:
                return found
        return None

    async def _mark(self, session: SessionRecord, mode: TakeMode, armed: bool) -> SessionRecord:
        registry = self._deps.registry
        for key, value in ((away.TAKEN_OVER, True), (away.ARMED, armed), (TAKE_MODE, mode)):
            await registry.set_session_extra(session.session_key, key, value)
        return await registry.get_session(session.session_key) or session

    async def _revive(self, session: SessionRecord) -> SessionRecord:
        if session.status is SessionStatus.ENDED:
            return await self._deps.lifecycle.revive(session.session_key, "taken over from the phone")
        return session

    async def _announce(self, session: SessionRecord) -> None:
        await self._deps.router.publish(
            SessionStarted(
                harness=session.harness,
                session_key=session.session_key,
                workspace=session.workspace,
                origin=Origin.PHONE,
                title=session.title,
                model=session.model,
            )
        )

    # the three ways

    async def _arm(self, chat: DeskChat) -> SessionRecord:
        registry = self._deps.registry
        found = await self._existing(chat)
        if found is None:
            found = await registry.create_session(
                SessionRecord(
                    session_key=f"{chat.app.value}:{chat.native_id}",
                    harness=chat.app,
                    harness_id=chat.native_id,
                    workspace=chat.workspace,
                    title=chat.title,
                    status=SessionStatus.RUNNING,
                    origin=Origin.DESK,
                )
            )
        session = await self._revive(found)
        if session.status not in (SessionStatus.RUNNING, SessionStatus.PARKED):
            session = (
                await self._deps.lifecycle.observe(
                    session.session_key, SessionStatus.RUNNING, "running at the desk"
                )
                or session
            )
        return await self._mark(session, "arm", True)

    async def _drive(self, chat: DeskChat, adapter: Adapter | None) -> SessionRecord:
        if adapter is None:
            raise AdapterUnavailableError(f"{label_of(chat.app)} is not connected")
        found = await self._existing(chat)
        if found is None:
            found = await adapter.adopt(chat.native_id, chat.workspace, chat.title)
        session = await self._revive(found)
        return await self._mark(session, "drive", False)

    async def _handoff(self, chat: DeskChat, why: str | None) -> SessionRecord:
        target = HANDOFF_IN.get(chat.app, chat.app)
        workspace = workspace_for_path(chat.workspace, load_workspaces())
        if workspace is None:
            raise RelayError("That chat's folder is not a registered workspace. Try `/ws`.")
        try:
            record = await self._sessions.start(target.value, workspace.name, handoff_prompt(chat))
        except RelayError as exc:
            lead = f"{why} " if why else ""
            raise RelayError(f"{lead}A new session could not start in {label_of(target)}: {exc}") from exc
        registry = self._deps.registry
        await registry.update_session_fields(record.session_key, title=f"↪ {chat.title}"[:80])
        extra: dict[str, JsonValue] = {HANDOFF_OF: chat.ref, TAKE_MODE: "handoff"}
        for key, value in extra.items():
            await registry.set_session_extra(record.session_key, key, value)
        return await registry.get_session(record.session_key) or record
