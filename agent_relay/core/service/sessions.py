"""Starting and talking to sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

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
from agent_relay.log import get_logger
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capability
from agent_relay.models.events import Origin, SessionStarted
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import load_workspaces, resolve_workspace

log = get_logger(__name__)

_CHUNK = 64 * 1024


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

    async def prompt(self, session_key: str, text: str, attachments: Sequence[Attachment] = ()) -> str:
        """Deliver a phone message to a session. Returns a short receipt for the user."""
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
            if adapter is not None:
                try:
                    await adapter.send_prompt(session, text, attachments)
                    return "sent"
                except UnsupportedError:
                    log.debug(f"{session.harness.value} has no mid-turn prompt; queueing as steering")
            await deps.registry.enqueue(session_key, "steer", with_attachments(text, attachments))
            return "queued as steering for the next tool call"
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
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.pager=cat",
            "-C",
            session.workspace,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            (out, more), (err, _) = await asyncio.wait_for(
                asyncio.gather(
                    _read_capped(proc.stdout, DIFF_LIMIT_BYTES), _read_capped(proc.stderr, _CHUNK)
                ),
                30,
            )
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RelayError("`git diff` took too long.") from exc
        if proc.returncode != 0:
            raise RelayError(f"`git diff` failed: {err.decode(errors='replace').strip()[:200]}")
        if more:
            out += f"\n… diff truncated ({more} more bytes)\n".encode()
        return out
