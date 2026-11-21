"""Apps for the discovery and `/new` tests: each can list models, count its listings, or hang."""

from __future__ import annotations

import asyncio

from agent_relay.adapters.base import AdapterUnavailableError, ModelInfo
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.events import Harness
from tests.fakes.adapters import EmittingAdapter

PICKS = Capabilities(cold_start=True, model_select=True, cancel=True)


def models(count: int, *, default: int | None = 0) -> list[ModelInfo]:
    return [ModelInfo(id=f"m{i}", label=f"Model {i}", default=i == default) for i in range(count)]


class App(EmittingAdapter):
    """An app on any harness with a model list that counts how often it is asked."""

    def __init__(
        self,
        harness: Harness,
        listed: list[ModelInfo] | None = None,
        *,
        picks: bool = True,
        healthy: bool = True,
    ) -> None:
        super().__init__(count=0)
        self.harness = harness
        self.capabilities = PICKS if picks else Capabilities(cold_start=True)
        self.listed = models(3) if listed is None else listed
        self.healthy = healthy
        self.listings = 0

    async def health(self) -> tuple[bool, str]:
        return self.healthy, "fake"

    async def list_models(self) -> list[ModelInfo]:
        self.listings += 1
        await asyncio.sleep(0)
        return list(self.listed)


class Hanging(App):
    """Models arrive only after `release` is set; health can hang too."""

    def __init__(self, harness: Harness, *, hang_health: bool = False) -> None:
        super().__init__(harness)
        self.release = asyncio.Event()
        self.hang_health = hang_health

    async def health(self) -> tuple[bool, str]:
        if self.hang_health:
            await asyncio.Event().wait()
        return True, "fake"

    async def list_models(self) -> list[ModelInfo]:
        self.listings += 1
        await self.release.wait()
        return list(self.listed)


class Failing(App):
    async def list_models(self) -> list[ModelInfo]:
        self.listings += 1
        raise AdapterUnavailableError("the app is closed")


class Raising(App):
    async def health(self) -> tuple[bool, str]:
        raise RuntimeError("no answer")
