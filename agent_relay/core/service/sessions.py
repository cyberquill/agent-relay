"""Starting and talking to sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from agent_relay import paths
from agent_relay.adapters.base import Adapter, StartRequest, UnsupportedError, with_attachments
from agent_relay.core import away, updates
from agent_relay.core.keys import session_signal
from agent_relay.core.service.common import (
    APP_LABELS,
    DIFF_LIMIT_BYTES,
    STICKY_MODEL_KEY,
    Deps,
    RelayError,
    resolve_app,
)
from agent_relay.core.service.discovery import Discovery
from agent_relay.core.service.queue import HOOKED, QUEUE_MODE_KEY, QueueDrain, mode_of, parse_mode
from agent_relay.log import get_logger
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capability
from agent_relay.models.config import QueueMode
from agent_relay.models.events import Origin, SessionStarted
from agent_relay.models.sessions import QueuedMessage, SessionRecord, SessionStatus
from agent_relay.settings import load_workspaces, resolve_workspace

log = get_logger(__name__)

_CHUNK = 64 * 1024
NUMSTAT_LIMIT_BYTES = 256 * 1024
NUMSTAT_TIMEOUT_S = 10


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int | None  # None for a binary file
    removed: int | None


@dataclass(frozen=True)
class Changes:
    """The workspace's uncommitted changes against HEAD, biggest first."""

    files: tuple[FileChange, ...]
    added: int
    removed: int


def parse_numstat(raw: bytes) -> Changes:
    files: list[FileChange] = []
    for line in raw.decode(errors="replace").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[2]:
            continue
        added, removed, name = parts
        files.append(
            FileChange(
                name,
                int(added) if added.isdigit() else None,
                int(removed) if removed.isdigit() else None,
            )
        )
    files.sort(key=lambda f: (-((f.added or 0) + (f.removed or 0)), f.path))
    return Changes(tuple(files), sum(f.added or 0 for f in files), sum(f.removed or 0 for f in files))


async def _git_diff(workspace: str, args: Sequence[str], limit: int, timeout: float) -> tuple[bytes, int]:
    """`git diff` with no shell, repo-configured programs or pager. Returns (kept output, bytes dropped)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "core.pager=cat",
        "-C",
        workspace,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        (out, more), (err, _) = await asyncio.wait_for(
            asyncio.gather(_read_capped(proc.stdout, limit), _read_capped(proc.stderr, _CHUNK)), timeout
        )
        await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise RelayError("`git diff` took too long.") from exc
    if proc.returncode != 0:
        raise RelayError(f"`git diff` failed: {err.decode(errors='replace').strip()[:200]}")
    return out, more


async def git_changes(workspace: str | None) -> Changes | None:
    """The changed files and line counts, or None when there is no workspace or git cannot say."""
    if not workspace:
        return None
    try:
        out, _ = await _git_diff(
            workspace, ("--numstat", "HEAD", "--"), NUMSTAT_LIMIT_BYTES, NUMSTAT_TIMEOUT_S
        )
    except (RelayError, OSError) as exc:
        log.debug(f"no change summary for {workspace}: {exc}")
        return None
    return parse_numstat(out)


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, int]:
    """Keep the first `limit` bytes; count the rest while draining so the process can exit."""
    if stream is None:
        raise RelayError("`git diff` has no output to read.")
    kept = bytearray()
    more = 0
    while chunk := await stream.read(_CHUNK):
        room = limit - len(kept)
        kept += chunk[: max(room, 0)]
        more += max(len(chunk) - max(room, 0), 0)
    return bytes(kept), more


class Sessions:
    def __init__(self, deps: Deps, discovery: Discovery) -> None:
        self._deps = deps
        self._discovery = discovery
        deps.router.subscribe(QueueDrain(deps))

    async def start(
        self,
        app: str,
        workspace: str,
        prompt: str,
        model: str | None = None,
        attachments: Sequence[Attachment] = (),
    ) -> SessionRecord:
        deps = self._deps
        harness = resolve_app(app)
        adapter = await self._discovery.usable(harness)
        if not adapter.capabilities.has(Capability.COLD_START):
            raise RelayError(f"{APP_LABELS[harness]} cannot start sessions from the phone.")
        target = resolve_workspace(workspace, load_workspaces())
        if target is None:
            raise RelayError(f"Unknown workspace `{workspace}`. Try `/ws`.")
        chosen = model or await self._discovery.sticky_model(harness)
        record = await adapter.start_session(
            StartRequest(
                workspace=str(paths.expand_user(target.path)),
                prompt=prompt,
                model=chosen,
                workspace_file=str(paths.expand_user(target.file)) if target.file else None,
            ),
            attachments,
        )
        stored = await deps.registry.get_session(record.session_key)
        if stored is not None:
            record = stored
        else:
            record = await deps.registry.create_session(
                record.model_copy(update={"origin": Origin.PHONE, "status": SessionStatus.RUNNING})
            )
        if chosen:
            await deps.registry.kv_set(STICKY_MODEL_KEY.format(harness=harness.value), chosen)
        await deps.router.publish(
            SessionStarted(
                harness=harness,
                session_key=record.session_key,
                workspace=record.workspace,
                origin=Origin.PHONE,
                title=record.title or prompt[:80],
                model=record.model,
            )
        )
        log.info(f"started {harness.value} session {record.session_key}")
        return record

    async def prompt(
        self, session_key: str, text: str, attachments: Sequence[Attachment] = (), *, steer: bool = False
    ) -> str:
        """Deliver a phone message to a session. Returns a short receipt for the user.

        While the session is mid-turn its queue mode decides; `steer=True` steers this one message.
        """
        deps = self._deps
        session = await self._discovery.session(session_key)
        degraded = await updates.degraded_reason(deps.registry, session.harness)
        adapter = None if degraded is not None else deps.router.adapter(session.harness)
        if session.status is SessionStatus.ENDED:
            return await self._resume(session, adapter, text, attachments)
        if session.status is SessionStatus.PARKED:
            await deps.registry.enqueue(session_key, "prompt", with_attachments(text, attachments))
            deps.signals.notify(session_signal(session_key))
            return "delivered to the parked session"
        if session.status is SessionStatus.RUNNING:
            mode: QueueMode = "steer" if steer else mode_of(session, deps.config.queue.default_mode)
            return await self._while_running(session, adapter, mode, text, attachments)
        if adapter is not None:
            receipt = await self._start_turn(session, adapter, text, attachments)
            if receipt is not None:
                return receipt
        await deps.registry.enqueue(session_key, "prompt", with_attachments(text, attachments))
        return "queued; it is delivered the next time the session stops or calls a tool"

    async def _start_turn(
        self, session: SessionRecord, adapter: Adapter, text: str, attachments: Sequence[Attachment]
    ) -> str | None:
        """Send or wake. None when the adapter can do neither."""
        lifecycle = self._deps.lifecycle
        try:
            await adapter.send_prompt(session, text, attachments)
            await lifecycle.observe(session.session_key, SessionStatus.RUNNING, "prompt from the phone")
            return "sent"
        except UnsupportedError:
            log.debug(f"{session.harness.value} cannot take a prompt directly; trying wake")
        try:
            await adapter.wake(session, adapter.prompt_with_files(text, attachments))
            await lifecycle.observe(session.session_key, SessionStatus.RUNNING, "woken from the phone")
            return "woke the session"
        except UnsupportedError:
            log.debug(f"{session.harness.value} cannot be woken")
        return None

    async def _resume(
        self, session: SessionRecord, adapter: Adapter | None, text: str, attachments: Sequence[Attachment]
    ) -> str:
        """A reply in an ended session's thread: revive it and start a turn, or say plainly why not."""
        label = APP_LABELS.get(session.harness, session.harness.value)
        if adapter is None:
            raise RelayError(f"That session has ended and {label} cannot be resumed from the phone.")
        lifecycle = self._deps.lifecycle
        revived = await lifecycle.revive(session.session_key, "reply from the phone")
        try:
            receipt = await self._start_turn(revived, adapter, text, attachments)
        except BaseException:
            await lifecycle.observe(session.session_key, SessionStatus.ENDED, "could not resume")
            raise
        if receipt is None:
            await lifecycle.observe(session.session_key, SessionStatus.ENDED, "could not resume")
            raise RelayError(f"That session has ended and {label} cannot be resumed from the phone.")
        return f"{receipt} (session resumed)"

    async def set_model(self, session_key: str, model: str) -> None:
        registry = self._deps.registry
        session = await self._discovery.session(session_key)
        adapter = await self._discovery.usable(session.harness)
        if not adapter.capabilities.has(Capability.MODEL_SELECT):
            raise RelayError(f"{APP_LABELS[session.harness]} does not offer model selection.")
        await adapter.set_model(session, model)
        await registry.update_session_fields(session_key, model=model)
        await registry.kv_set(STICKY_MODEL_KEY.format(harness=session.harness.value), model)

    async def stop(self, session_key: str) -> None:
        session = await self._discovery.session(session_key)
        adapter = self._discovery.adapter(session.harness)
        if not adapter.capabilities.has(Capability.CANCEL):
            raise RelayError(f"{APP_LABELS[session.harness]} cannot be cancelled from the phone.")
        await adapter.cancel(session)
        await self._deps.lifecycle.stop(session_key, "stopped from the phone")

    async def status(self, session_key: str) -> str:
        registry = self._deps.registry
        session = await self._discovery.session(session_key)
        pending = [p for p in await registry.open_pending() if p.session_key == session_key]
        is_away = await away.is_away(registry, session.workspace)
        lines = [
            f"**{APP_LABELS.get(session.harness, session.harness.value)}** · `{session.workspace or '?'}`",
            f"status: {session.status.value} · model: {session.model or 'default'}"
            f" · away: {'on' if is_away else 'off'}",
            f"pending: {len(pending)} · started from: {session.origin.value}",
        ]
        return "\n".join(lines)

    async def diff(self, session_key: str) -> bytes:
        session = await self._discovery.session(session_key)
        if not session.workspace:
            raise RelayError("This session has no workspace.")
        out, more = await _git_diff(session.workspace, ("HEAD", "--"), DIFF_LIMIT_BYTES, 30)
        if more:
            out += f"\n… diff truncated ({more} more bytes)\n".encode()
        return out

    # queue modes

    async def queue_mode(self, session_key: str) -> QueueMode:
        session = await self._discovery.session(session_key)
        return mode_of(session, self._deps.config.queue.default_mode)

    async def set_queue_mode(self, session_key: str, mode: str) -> QueueMode:
        chosen = parse_mode(mode)
        await self._discovery.session(session_key)
        await self._deps.registry.set_session_extra(session_key, QUEUE_MODE_KEY, chosen)
        return chosen

    async def mode_warning(self, session_key: str, mode: QueueMode) -> str | None:
        """Why a mode will not do what it says for this session's app, if so."""
        session = await self._discovery.session(session_key)
        if mode == "interrupt" and not self._can_cancel(session, self._deps.router.adapter(session.harness)):
            return (
                f"{self._label(session)} cannot be interrupted from the phone; messages will queue instead."
            )
        return None

    async def queued(self, session_key: str) -> list[QueuedMessage]:
        return await self._deps.registry.list_queued(session_key)

    async def remove_queued(self, session_key: str, item_id: int) -> bool:
        return await self._deps.registry.delete_queued(session_key, item_id)

    async def clear_queued(self, session_key: str) -> int:
        return await self._deps.registry.clear_queued(session_key)

    @staticmethod
    def _label(session: SessionRecord) -> str:
        return APP_LABELS.get(session.harness, session.harness.value)

    @staticmethod
    def _can_cancel(session: SessionRecord, adapter: Adapter | None) -> bool:
        return adapter is not None and adapter.capabilities.has(Capability.CANCEL)

    async def _hold(self, session: SessionRecord, text: str, note: str = "") -> str:
        """Queue the message for the next turn."""
        registry = self._deps.registry
        await registry.enqueue(session.session_key, "prompt", text)
        waiting = len(await registry.list_queued(session.session_key))
        return f"Queued ({waiting} waiting){f'. {note}' if note else ''}"

    async def _while_running(
        self,
        session: SessionRecord,
        adapter: Adapter | None,
        mode: QueueMode,
        text: str,
        attachments: Sequence[Attachment],
    ) -> str:
        body = with_attachments(text, attachments)
        label = self._label(session)
        if mode == "queue":
            return await self._hold(session, body)
        if mode == "interrupt":
            if adapter is None or not self._can_cancel(session, adapter):
                return await self._hold(
                    session,
                    body,
                    f"{label} cannot be interrupted from the phone, so it waits for this turn to end",
                )
            return await self._interrupt(session, adapter, body)
        if adapter is not None:
            try:
                await adapter.send_prompt(session, text, attachments)
                return "sent"
            except UnsupportedError:
                log.debug(f"{session.harness.value} has no mid-turn prompt")
            if session.extra.get(HOOKED) is not True:
                return await self._hold(
                    session, body, f"{label} cannot be steered mid-turn, so it goes in when this turn ends"
                )
        await self._deps.registry.enqueue(session.session_key, "steer", body)
        return "queued as steering for the next tool call"

    async def _interrupt(self, session: SessionRecord, adapter: Adapter, body: str) -> str:
        """Cancel the turn and let the turn-end path send the message as a new turn."""
        registry = self._deps.registry
        # Queued first: a fast adapter can end the turn before cancel() returns.
        await registry.enqueue(session.session_key, "prompt", body)
        try:
            await adapter.cancel(session)
        except Exception as exc:
            log.warning(f"{session.session_key}: interrupt failed ({type(exc).__name__}: {exc})")
            waiting = len(await registry.list_queued(session.session_key))
            return f"Queued ({waiting} waiting). Could not interrupt ({exc}); it goes in when this turn ends."
        return "Interrupting; your message goes in as soon as the turn stops."
