"""What the relay can reach: apps, workspaces, models and sessions.

Model lists come through one shared cache (10 minutes) so `/new`, `/model` and a session start never
list the same app twice. Cursor CDP is the exception: listing its models opens a menu in the desktop UI,
so only an explicit `/models` does that, and the list is saved for the cards to use.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agent_relay.adapters.base import Adapter, AdapterUnavailableError, ModelInfo
from agent_relay.core import updates
from agent_relay.core.service.common import (
    APP_LABELS,
    STICKY_MODEL_KEY,
    AppInfo,
    Deps,
    RelayError,
    resolve_app,
)
from agent_relay.log import get_logger
from agent_relay.models.capabilities import Capability
from agent_relay.models.config import Workspace
from agent_relay.models.events import Harness
from agent_relay.models.sessions import SessionRecord
from agent_relay.settings import load_workspaces

log = get_logger(__name__)

MODELS_TTL_S = 600.0
MODELS_TIMEOUT_S = 15.0  # an explicit `/models` or `/model`
OFFER_MODEL_TIMEOUT_S = 3.0  # per app, while a card is being built
HEALTH_TIMEOUT_S = 5.0
MODELS_PER_APP = 8
OFFER_MAX_ENTRIES = 40
DEFAULT_CHOICE = "default"
CDP_MODELS_KEY = "models.cursor_cdp"  # the list saved by the last explicit listing (or the self-test)
LAST_APP_KEY = "new.last_app"
HIDDEN_MODEL_MARKERS = ("copilot-utility", "dictation")  # the app's own helpers, never user choices

_MODEL_LIST = TypeAdapter(list[ModelInfo])


def is_hidden_model(model_id: str, label: str = "") -> bool:
    return any(marker in f"{model_id} {label}".lower() for marker in HIDDEN_MODEL_MARKERS)


def visible_models(models: list[ModelInfo]) -> list[ModelInfo]:
    return [m for m in models if not is_hidden_model(m.id, m.label)]


class AgentEntry(BaseModel):
    """One line of the `/new` agent list: an app with a model, or with its own default."""

    model_config = ConfigDict(frozen=True)

    value: str  # "<harness>:<model>" or "<harness>:default"
    label: str  # "<app> · <model>", without the app emoji
    harness: str
    model: str | None = None


class AgentOffer(BaseModel):
    """What `/new` may start: the entries, the apps left out (with why) and the entry to preselect."""

    model_config = ConfigDict(frozen=True)

    entries: list[AgentEntry] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)
    preselected: str | None = None

    def find(self, value: object) -> AgentEntry | None:
        return next((e for e in self.entries if e.value == value), None)


@dataclass(frozen=True)
class _Candidate:
    harness: Harness
    label: str
    models: list[ModelInfo]
    sticky: str | None
    down: str | None


def agent_value(harness: str, model: str | None) -> str:
    return f"{harness}:{model or DEFAULT_CHOICE}"


def _app_entries(candidate: _Candidate) -> tuple[AgentEntry, list[AgentEntry]]:
    harness, label = candidate.harness.value, candidate.label
    usable = visible_models(candidate.models)
    standard = next((m for m in usable if m.default), None)
    named = f" ({standard.label})" if standard is not None else ""
    base = AgentEntry(value=agent_value(harness, None), label=f"{label} · Default{named}", harness=harness)
    listed = [m for m in usable if not m.default]
    # A model the user picked before stays on the card even when the app lists it late.
    held = next((i for i, m in enumerate(listed) if m.id == candidate.sticky), None)
    if held is not None and held >= MODELS_PER_APP:
        listed.insert(0, listed.pop(held))
    entries = [
        AgentEntry(
            value=agent_value(harness, m.id), label=f"{label} · {m.label}", harness=harness, model=m.id
        )
        for m in listed[:MODELS_PER_APP]
    ]
    return base, entries


def _cap(apps: list[tuple[AgentEntry, list[AgentEntry]]], limit: int) -> list[AgentEntry]:
    """Every app keeps its Default; the models share what is left, one per app in turn."""
    room = max(limit - len(apps), 0)
    kept = [0] * len(apps)
    while room > 0:
        moved = False
        for i, (_, models) in enumerate(apps):
            if kept[i] < len(models) and room > 0:
                kept[i] += 1
                room -= 1
                moved = True
        if not moved:
            break
    return [e for i, (base, models) in enumerate(apps) for e in (base, *models[: kept[i]])]


class Discovery:
    def __init__(self, deps: Deps, clock: Callable[[], float] = time.monotonic) -> None:
        self._deps = deps
        self.clock = clock
        self._cache: dict[Harness, tuple[float, list[ModelInfo]]] = {}
        self._inflight: dict[Harness, asyncio.Task[list[ModelInfo]]] = {}

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

    async def _health(self, adapter: Adapter) -> tuple[bool, str]:
        try:
            return await asyncio.wait_for(adapter.health(), HEALTH_TIMEOUT_S)
        except Exception as exc:
            return False, f"health check failed: {type(exc).__name__}"

    async def _info(self, harness: Harness, adapter: Adapter) -> AppInfo:
        reason = await updates.degraded_reason(self._deps.registry, harness)
        healthy, detail = await self._health(adapter)
        if reason is not None:
            healthy, detail = False, f"{updates.DEGRADED_VERB.get(harness, 'disabled')}: {reason}"
        return AppInfo(
            harness,
            APP_LABELS.get(harness, harness.value),
            adapter.capabilities.supported(),
            healthy,
            detail,
        )

    def _adapters(self) -> list[tuple[Harness, Adapter]]:
        return sorted(self._deps.router.adapters().items(), key=lambda kv: kv[0].value)

    async def apps(self) -> list[AppInfo]:
        """Every enabled app; the health checks run side by side."""
        return list(await asyncio.gather(*(self._info(h, a) for h, a in self._adapters())))

    def workspaces(self) -> list[Workspace]:
        return list(load_workspaces().workspaces)

    # models

    async def _saved_cdp_models(self) -> list[ModelInfo]:
        raw = await self._deps.registry.kv_get(CDP_MODELS_KEY)
        if raw is None:
            return []
        try:
            return _MODEL_LIST.validate_python(json.loads(raw))
        except (
            ValueError
        ):  # JSON that does not parse, or entries that are not models (ValidationError is one)
            return []

    async def _load(self, harness: Harness, adapter: Adapter) -> list[ModelInfo]:
        try:
            found = list(await adapter.list_models())
        except AdapterUnavailableError as exc:
            raise RelayError(str(exc)) from exc
        self._cache[harness] = (self.clock(), found)
        if harness is Harness.CURSOR_CDP:
            await self._deps.registry.kv_set(CDP_MODELS_KEY, _MODEL_LIST.dump_json(found).decode())
        return found

    def _fetch(self, harness: Harness, adapter: Adapter) -> asyncio.Task[list[ModelInfo]]:
        """One listing per app at a time; it keeps running (and fills the cache) after a caller gave up."""
        task = self._inflight.get(harness)
        if task is None or task.done():
            task = asyncio.create_task(self._load(harness, adapter))
            self._inflight[harness] = task
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return task

    async def models(
        self, app: str, *, refresh: bool = False, timeout: float = MODELS_TIMEOUT_S
    ) -> list[ModelInfo]:
        harness = resolve_app(app)
        adapter = await self.usable(harness)
        label = APP_LABELS.get(harness, harness.value)
        if not adapter.capabilities.has(Capability.MODEL_SELECT):
            raise RelayError(f"{label} does not offer model selection.")
        if harness is Harness.CURSOR_CDP and not refresh:
            return await self._saved_cdp_models()
        held = self._cache.get(harness)
        if held is not None and not refresh and self.clock() - held[0] < MODELS_TTL_S:
            return list(held[1])
        try:
            return list(await asyncio.wait_for(asyncio.shield(self._fetch(harness, adapter)), timeout))
        except TimeoutError:
            raise RelayError(f"{label} did not list its models in time.") from None

    async def _models_or_none(self, harness: Harness, adapter: Adapter) -> list[ModelInfo]:
        if not adapter.capabilities.has(Capability.MODEL_SELECT):
            return []
        try:
            return await self.models(harness.value, timeout=OFFER_MODEL_TIMEOUT_S)
        except Exception as exc:
            log.info(f"no model list for {harness.value}: {type(exc).__name__}: {exc}")
            return []

    # the /new agent list

    async def _candidate(self, harness: Harness, adapter: Adapter) -> _Candidate:
        label = APP_LABELS.get(harness, harness.value)
        reason = await updates.degraded_reason(self._deps.registry, harness)
        if reason is not None:
            verb = updates.DEGRADED_VERB.get(harness, "disabled")
            return _Candidate(harness, label, [], None, f"{label} ({verb})")
        (healthy, _), models = await asyncio.gather(
            self._health(adapter), self._models_or_none(harness, adapter)
        )
        if not healthy:
            return _Candidate(harness, label, [], None, f"{label} (down)")
        sticky = await self._deps.registry.kv_get(STICKY_MODEL_KEY.format(harness=harness.value))
        return _Candidate(harness, label, models, sticky, None)

    async def agent_offer(self) -> AgentOffer:
        """Every app that can start a session, with its models, for the `/new` card.

        Apps are asked side by side; one that is down or slow is left out and named in `unavailable`.
        """
        cold = [(h, a) for h, a in self._adapters() if a.capabilities.has(Capability.COLD_START)]
        found = await asyncio.gather(*(self._candidate(h, a) for h, a in cold))
        unavailable = [c.down for c in found if c.down is not None]
        up = [c for c in found if c.down is None]
        entries = _cap([_app_entries(c) for c in up], OFFER_MAX_ENTRIES)
        offer = AgentOffer(entries=entries, unavailable=unavailable)
        return offer.model_copy(update={"preselected": await self._preselect(offer, up)})

    async def _preselect(self, offer: AgentOffer, up: list[_Candidate]) -> str | None:
        last = await self._deps.registry.kv_get(LAST_APP_KEY)
        chosen = next((c for c in up if c.harness.value == last), None)
        if chosen is not None:
            sticky = agent_value(chosen.harness.value, chosen.sticky)
            for value in (sticky, agent_value(chosen.harness.value, None)):
                if offer.find(value) is not None:
                    return value
        return offer.entries[0].value if offer.entries else None

    # sessions

    async def sessions(self) -> list[SessionRecord]:
        return await self._deps.registry.list_sessions()

    async def session(self, session_key: str) -> SessionRecord:
        found = await self._deps.registry.get_session(session_key)
        if found is None:
            raise RelayError("That session is no longer known to the relay.")
        return found

    async def sticky_model(self, harness: Harness) -> str | None:
        """The model last used with this app, unless the app no longer lists it."""
        sticky = await self._deps.registry.kv_get(STICKY_MODEL_KEY.format(harness=harness.value))
        adapter = self._deps.router.adapter(harness)
        if sticky is None or adapter is None or not adapter.capabilities.has(Capability.MODEL_SELECT):
            return sticky
        try:
            listed = {m.id for m in await self.models(harness.value)}
        except RelayError:
            return sticky
        return sticky if not listed or sticky in listed else None
