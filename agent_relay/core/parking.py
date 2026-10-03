"""Parked sessions: a stop hook blocks until the phone replies, is released, or the deadline passes.

Parking is sliced: each hook call blocks at most `park.slice_s`, then returns a "parking renewed"
continuation, so the agent stops again at once and the next stop hook resumes the same deadline.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from agent_relay.core import away
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RELEASE_KEY, session_signal
from agent_relay.core.signals import Signals
from agent_relay.log import get_logger
from agent_relay.models.config import ParkConfig
from agent_relay.models.events import Harness, SessionParked, SessionReleased
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

ParkKind = Literal["continue", "renewed", "released", "deadline", "limit", "skipped"]
RENEW_TEXT = (
    "Parking renewed: the user is away and has not replied yet. Do nothing else and end your turn now "
    "with the single word: waiting."
)
PHONE_PREFIX = "Message from the user, sent from their phone:\n\n"
LIMIT_TEXT = "Parking limit reached for this session. Reply here to wake it."
BLOCKS_KEY = "stop_blocks"
DEADLINE_KEY = "park_deadline"

type Notify = Callable[[SessionRecord, str], Awaitable[None]]


@dataclass(frozen=True)
class ParkResult:
    kind: ParkKind
    text: str = ""

    @property
    def blocks(self) -> bool:
        return self.kind in ("continue", "renewed")


def phone_text(texts: list[str]) -> str:
    return PHONE_PREFIX + "\n\n".join(texts)


class ParkManager:
    def __init__(
        self,
        registry: Registry,
        router: Router,
        signals: Signals,
        config: ParkConfig,
        notify: Notify,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._registry = registry
        self._router = router
        self._signals = signals
        self._config = config
        self._notify = notify
        self._clock = clock

    def block_limit(self, harness: Harness) -> int | None:
        if harness in (Harness.COPILOT_CLI, Harness.VSCODE_COPILOT):
            return self._config.copilot_max_blocks
        if harness is Harness.VSCODE_LOCAL:
            return self._config.local_max_continuations
        return None

    async def reset_blocks(self, session_key: str) -> None:
        await self._registry.set_session_extra(session_key, BLOCKS_KEY, 0)

    async def _set(
        self, session: SessionRecord, status: SessionStatus, blocks: int, deadline: str | None
    ) -> None:
        await self._registry.set_session_fields(session.session_key, status=status.value)
        await self._registry.set_session_extra(session.session_key, BLOCKS_KEY, blocks)
        await self._registry.set_session_extra(session.session_key, DEADLINE_KEY, deadline)

    async def park(self, session: SessionRecord, stop_hook_active: bool) -> ParkResult:
        raw_blocks = session.extra.get(BLOCKS_KEY)
        blocks = raw_blocks if isinstance(raw_blocks, int) else 0
        if stop_hook_active and blocks == 0:
            log.info(f"{session.session_key}: another stop hook is already continuing; not parking")
            return ParkResult("skipped")
        if not await away.is_away(self._registry, session.workspace):
            await self._set(session, SessionStatus.IDLE, 0, None)
            return ParkResult("skipped")
        limit = self.block_limit(session.harness)
        if limit is not None and blocks + 1 >= limit:
            await self._set(session, SessionStatus.IDLE, 0, None)
            await self._registry.set_session_extra(session.session_key, "wake_needed", True)
            await self._notify(session, LIMIT_TEXT)
            log.info(f"{session.session_key}: parking limit ({limit}) reached")
            return ParkResult("limit")

        now = self._clock()
        stored = session.extra.get(DEADLINE_KEY)
        deadline = datetime.fromisoformat(stored) if isinstance(stored, str) else None
        if deadline is None or deadline <= now:
            deadline = now + timedelta(seconds=self._config.deadline_s)
            await self._set(session, SessionStatus.PARKED, blocks, deadline.isoformat())
            await self._router.publish(
                SessionParked(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    deadline=deadline,
                )
            )
        else:
            await self._registry.set_session_fields(session.session_key, status=SessionStatus.PARKED.value)
        slice_end = now + timedelta(seconds=self._config.slice_s)
        return await self._wait(session, blocks, deadline, slice_end)

    async def _wait(
        self, session: SessionRecord, blocks: int, deadline: datetime, slice_end: datetime
    ) -> ParkResult:
        key = session.session_key
        release_key = RELEASE_KEY.format(session_key=key)
        while True:
            reason = await self._registry.kv_get(release_key)
            if reason is not None:
                await self._registry.kv_delete(release_key)
                await self._set(session, SessionStatus.IDLE, 0, None)
                return ParkResult("released", reason)
            if not await away.is_away(self._registry, session.workspace):
                await self._set(session, SessionStatus.IDLE, 0, None)
                return ParkResult("released", "away mode off")
            queued = await self._registry.pop_queued(key, ("prompt", "steer"))
            if queued:
                await self._set(session, SessionStatus.RUNNING, blocks + 1, None)
                return ParkResult("continue", phone_text([q.text for q in queued]))
            now = self._clock()
            if now >= deadline:
                await self._set(session, SessionStatus.IDLE, 0, None)
                await self._router.publish(
                    SessionReleased(
                        harness=session.harness,
                        session_key=key,
                        workspace=session.workspace,
                        reason="deadline",
                    )
                )
                return ParkResult("deadline")
            if now >= slice_end:
                await self._registry.set_session_extra(key, BLOCKS_KEY, blocks + 1)
                return ParkResult("renewed", RENEW_TEXT)
            remaining = (min(deadline, slice_end) - now).total_seconds()
            await self._signals.wait(session_signal(key), max(0.01, min(remaining, 30.0)))

    async def recover(self) -> int:
        """After a relayd restart no hook is still blocked, so parked sessions are idle."""
        count = 0
        for session in await self._registry.list_sessions():
            if session.status is SessionStatus.PARKED:
                await self._set(session, SessionStatus.IDLE, 0, None)
                count += 1
        return count
