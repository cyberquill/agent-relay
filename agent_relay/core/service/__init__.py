"""The relay's core API. Webex, the PWA and relay-mcp all call this; none talk to adapters directly."""

from __future__ import annotations

from collections.abc import Sequence

from agent_relay.adapters.base import Decision, ModelInfo
from agent_relay.core.keys import RELEASE_KEY, pending_key, session_signal
from agent_relay.core.lifecycle import Lifecycle
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service.approvals import Approvals
from agent_relay.core.service.away import AwayControl
from agent_relay.core.service.common import (
    APP_ALIASES,
    APP_LABELS,
    DESK_DECISION,
    DIFF_LIMIT_BYTES,
    STICKY_MODEL_KEY,
    AppInfo,
    DeliveryError,
    Deps,
    RelayError,
    resolve_app,
)
from agent_relay.core.service.discovery import Discovery
from agent_relay.core.service.sessions import Sessions
from agent_relay.core.signals import Signals
from agent_relay.models.attachments import Attachment
from agent_relay.models.config import RelayConfig, Workspace
from agent_relay.models.events import Harness, ResolvedBy
from agent_relay.models.sessions import SessionRecord

__all__ = [
    "APP_ALIASES",
    "APP_LABELS",
    "DESK_DECISION",
    "DIFF_LIMIT_BYTES",
    "RELEASE_KEY",
    "STICKY_MODEL_KEY",
    "AppInfo",
    "DeliveryError",
    "RelayError",
    "RelayService",
    "ResolvedBy",
    "pending_key",
    "resolve_app",
    "session_signal",
]


class RelayService:
    """Facade over the parts: discovery, sessions, away control and approvals."""

    def __init__(
        self,
        registry: Registry,
        router: Router,
        signals: Signals,
        config: RelayConfig,
        lifecycle: Lifecycle | None = None,
    ) -> None:
        self.registry = registry
        self.router = router
        self.signals = signals
        self.config = config
        self.lifecycle = lifecycle or Lifecycle(registry, router, signals)
        deps = Deps(registry, router, signals, config, self.lifecycle)
        self._discovery = Discovery(deps)
        self._sessions = Sessions(deps, self._discovery)
        self._approvals = Approvals(deps)
        self._away = AwayControl(deps, self._discovery, self._approvals)

    # discovery

    async def apps(self) -> list[AppInfo]:
        return await self._discovery.apps()

    def workspaces(self) -> list[Workspace]:
        return self._discovery.workspaces()

    async def models(self, app: str) -> list[ModelInfo]:
        return await self._discovery.models(app)

    async def sessions(self) -> list[SessionRecord]:
        return await self._discovery.sessions()

    async def session(self, session_key: str) -> SessionRecord:
        return await self._discovery.session(session_key)

    async def sticky_model(self, harness: Harness) -> str | None:
        return await self._discovery.sticky_model(harness)

    # starting and talking

    async def start(
        self,
        app: str,
        workspace: str,
        prompt: str,
        model: str | None = None,
        attachments: Sequence[Attachment] = (),
    ) -> SessionRecord:
        return await self._sessions.start(app, workspace, prompt, model, attachments)

    async def prompt(self, session_key: str, text: str, attachments: Sequence[Attachment] = ()) -> str:
        return await self._sessions.prompt(session_key, text, attachments)

    async def set_model(self, session_key: str, model: str) -> None:
        await self._sessions.set_model(session_key, model)

    async def stop(self, session_key: str) -> None:
        await self._sessions.stop(session_key)

    async def status(self, session_key: str) -> str:
        return await self._sessions.status(session_key)

    async def diff(self, session_key: str) -> bytes:
        return await self._sessions.diff(session_key)

    # away, back, panic

    async def set_away(self, enabled: bool, session_key: str | None = None) -> str:
        return await self._away.set_away(enabled, session_key)

    async def set_away_for(self, enabled: bool, workspace: str | None) -> str:
        return await self._away.set_away_for(enabled, workspace)

    async def release(self, session_key: str, reason: str) -> None:
        await self._away.release(session_key, reason)

    async def back(self, session_key: str | None = None) -> str:
        return await self._away.back(session_key)

    async def panic(self) -> str:
        return await self._away.panic()

    # approvals and questions

    async def answer_approval(self, request_id: str, decision: Decision, by: ResolvedBy = "phone") -> bool:
        return await self._approvals.answer_approval(request_id, decision, by)

    async def answer_question(self, request_id: str, answer: str | None, by: ResolvedBy = "phone") -> bool:
        return await self._approvals.answer_question(request_id, answer, by)
