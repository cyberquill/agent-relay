"""Build the adapters enabled in relay.toml. Each one degrades on its own when its app is missing."""

from __future__ import annotations

from agent_relay.adapters.ahp.adapter import AhpAdapter
from agent_relay.adapters.base import Adapter
from agent_relay.adapters.opencode.adapter import OpenCodeAdapter
from agent_relay.core.registry import Registry
from agent_relay.log import get_logger
from agent_relay.models.config import RelayConfig
from agent_relay.settings import Secret, get_secret

log = get_logger(__name__)


def build_adapters(config: RelayConfig, registry: Registry) -> list[Adapter]:
    adapters: list[Adapter] = []
    if config.adapters.ahp:
        adapters.append(AhpAdapter(registry))
    if config.adapters.opencode:
        password = get_secret(Secret.OPENCODE_PASSWORD)
        if password is None:
            log.info("no OpenCode server password in Keychain; connecting without basic auth")
        adapters.append(OpenCodeAdapter(config.opencode, registry, password))
    return adapters
