"""The relay's core API. Webex, the PWA and relay-mcp all call this; none talk to adapters directly."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

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
from agent_relay.core.service.workspaces import NOT_ENABLED as WS_NOT_ENABLED
from agent_relay.core.service.workspaces import Started, WorkspaceAdmin
from agent_relay.core.signals import Signals
from agent_relay.core.workspaces import FolderOffer
from agent_relay.models.attachments import Attachment
from agent_relay.models.config import QueueMode, RelayConfig, Workspace
from agent_relay.models.events import Harness, ResolvedBy
from agent_relay.models.sessions import QueuedMessage, SessionRecord

__all__ = [
    "APP_ALIASES",
    "APP_LABELS",
    "DESK_DECISION",
    "DIFF_LIMIT_BYTES",
    "RELEASE_KEY",
    "STICKY_MODEL_KEY",
    "WS_NOT_ENABLED",
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
        self.lifecycle = lifecycle or Lifecycle(registry, router, signals, router.adapter)
        deps = Deps(registry, router, signals, config, self.lifecycle)
        self._discovery = Discovery(deps)
        self._sessions = Sessions(deps, self._discovery)
        self._approvals = Approvals(deps)
        self._away = AwayControl(deps, self._discovery, self._approvals)
        self._workspaces = WorkspaceAdmin(deps)

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

    async def answer_approval(
        self, request_id: str, decision: Decision, by: ResolvedBy = "phone", reason: str | None = None
    ) -> bool:
        return await self._approvals.answer_approval(request_id, decision, by, reason)

    async def answer_question(self, request_id: str, answer: str | None, by: ResolvedBy = "phone") -> bool:
        return await self._approvals.answer_question(request_id, answer, by)

    # workspaces and repos from the phone

    def ws_roots(self) -> list[Path]:
        return self._workspaces.roots()

    async def ws_folders(self, workspace: str | None = None) -> FolderOffer:
        return await self._workspaces.folders(workspace)

    async def ws_add_folder(self, folder: object, name: str | None = None) -> Workspace:
        return await self._workspaces.add_folder(folder, name)

    async def ws_clone(
        self, url: object, name: str | None, root: object, started: Started | None = None
    ) -> Workspace:
        return await self._workspaces.clone(url, name, root, started)

    async def ws_add_repo_folder(self, workspace: object, folder: object) -> Workspace:
        return await self._workspaces.add_repo_folder(workspace, folder)

    async def ws_add_repo_clone(
        self, workspace: object, url: object, root: object, started: Started | None = None
    ) -> Workspace:
        return await self._workspaces.add_repo_clone(workspace, url, root, started)

    # queue modes

    async def steer(self, session_key: str, text: str, attachments: Sequence[Attachment] = ()) -> str:
        return await self._sessions.prompt(session_key, text, attachments, steer=True)

    async def queue_mode(self, session_key: str) -> QueueMode:
        return await self._sessions.queue_mode(session_key)

    async def set_queue_mode(self, session_key: str, mode: str) -> QueueMode:
        return await self._sessions.set_queue_mode(session_key, mode)

    async def queue_mode_warning(self, session_key: str, mode: QueueMode) -> str | None:
        return await self._sessions.mode_warning(session_key, mode)

    async def queued(self, session_key: str) -> list[QueuedMessage]:
        return await self._sessions.queued(session_key)

    async def remove_queued(self, session_key: str, item_id: int) -> bool:
        return await self._sessions.remove_queued(session_key, item_id)

    async def clear_queued(self, session_key: str) -> int:
        return await self._sessions.clear_queued(session_key)
