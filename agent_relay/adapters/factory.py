"""Build the adapters enabled in relay.toml."""

from __future__ import annotations

from agent_relay.adapters.base import Adapter
from agent_relay.core.registry import Registry
from agent_relay.models.config import RelayConfig


def build_adapters(config: RelayConfig, registry: Registry) -> list[Adapter]:
    adapters: list[Adapter] = []
    del config, registry
    return adapters
