"""What the relay can reach: apps, workspaces, models and sessions."""

from __future__ import annotations

import asyncio

from agent_relay.adapters.base import Adapter, ModelInfo
from agent_relay.core import updates
from agent_relay.core.service.common import (
    APP_LABELS,
    STICKY_MODEL_KEY,
    AppInfo,
    Deps,
    RelayError,
    resolve_app,
)
from agent_relay.models.capabilities import Capability
from agent_relay.models.config import Workspace
from agent_relay.models.events import Harness
from agent_relay.models.sessions import SessionRecord
from agent_relay.settings import load_workspaces


class Discovery:
    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def adapter(self, harness: Harness) -> Adapter:
        adapter = self._deps.router.adapter(harness)
        if adapter is None:
            raise RelayError(f"{APP_LABELS.get(harness, harness.value)} is not enabled.")
        return adapter

    async def usable(self, harness: Harness) -> Adapter:
        """The adapter, unless an update made its self-test fail (then the reason is shown)."""
        adapter = self.adapter(harness)
        reason = await updates.degraded_reason(self._deps.registry, harness)
        if reason is not None:
            verb = updates.DEGRADED_VERB.get(harness, "disabled")
            raise RelayError(f"{APP_LABELS.get(harness, harness.value)} is {verb}: {reason}.")
        return adapter

    async def apps(self) -> list[AppInfo]:
        found: list[AppInfo] = []
        for harness, adapter in sorted(self._deps.router.adapters().items(), key=lambda kv: kv[0].value):
            reason = await updates.degraded_reason(self._deps.registry, harness)
            try:
                healthy, detail = await asyncio.wait_for(adapter.health(), 5)
            except Exception as exc:
                healthy, detail = False, f"health check failed: {type(exc).__name__}"
            if reason is not None:
                healthy, detail = False, f"{updates.DEGRADED_VERB.get(harness, 'disabled')}: {reason}"
            found.append(
                AppInfo(
                    harness,
                    APP_LABELS.get(harness, harness.value),
                    adapter.capabilities.supported(),
                    healthy,
                    detail,
                )
            )
        return found

    def workspaces(self) -> list[Workspace]:
        return list(load_workspaces().workspaces)

    async def models(self, app: str) -> list[ModelInfo]:
        harness = resolve_app(app)
        adapter = await self.usable(harness)
        if not adapter.capabilities.has(Capability.MODEL_SELECT):
            raise RelayError(f"{APP_LABELS[harness]} does not offer model selection.")
        return await adapter.list_models()

    async def sessions(self) -> list[SessionRecord]:
        return await self._deps.registry.list_sessions()

    async def session(self, session_key: str) -> SessionRecord:
        found = await self._deps.registry.get_session(session_key)
        if found is None:
            raise RelayError("That session is no longer known to the relay.")
        return found

    async def sticky_model(self, harness: Harness) -> str | None:
        sticky = await self._deps.registry.kv_get(STICKY_MODEL_KEY.format(harness=harness.value))
        adapter = self._deps.router.adapter(harness)
        if sticky is None or adapter is None or not adapter.capabilities.has(Capability.MODEL_SELECT):
            return sticky
        listed = {m.id for m in await self.models(harness.value)}
        return sticky if not listed or sticky in listed else None
