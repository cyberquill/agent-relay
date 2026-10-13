"""`relay doctor` and `/doctor`: every check, with a status, what was found and the command that fixes it.

Local checks read this Mac; daemon checks come from relayd (over IPC from the CLI, directly in `/doctor`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from agent_relay import paths
from agent_relay.adapters.ahp.endpoints import run_cli
from agent_relay.doctor import local
from agent_relay.doctor.model import Check, Report
from agent_relay.ipc import FrameError, async_request
from agent_relay.models.config import RelayConfig

type DaemonChecks = Callable[[], Awaitable[list[Check]]]

IPC_TIMEOUT_S = 20.0


def _sync_checks(config: RelayConfig, python: str | None) -> list[Check]:
    checks = local.hook_checks(config, python)
    checks += local.mcp_checks(python)
    checks += local.skill_checks()
    checks += local.keychain_checks(config)
    return checks


async def local_checks(config: RelayConfig, python: str, runner: local.Runner | None = None) -> list[Check]:
    runner = runner or run_cli
    checks = await local.version_checks(runner)
    checks += await asyncio.to_thread(_sync_checks, config, python)
    checks += await local.launchd_checks(python, runner)
    checks.append(await local.presence_check(config))
    return checks


async def over_ipc() -> list[Check]:
    """The daemon half, fetched from a running relayd."""
    try:
        reply = await async_request({"op": "doctor"}, paths.socket_path(), timeout=IPC_TIMEOUT_S)
    except (OSError, TimeoutError, FrameError) as exc:
        return [
            Check(
                area="relayd",
                name="daemon",
                status="fail",
                detail=f"not reachable ({type(exc).__name__}); adapter, Webex and away checks skipped",
                fix="relay start",
            )
        ]
    raw = reply.get("checks")
    if not isinstance(raw, list):
        return [Check(area="relayd", name="daemon", status="fail", detail=f"bad reply: {reply.get('error')}")]
    return [Check(area="relayd", name="daemon", status="ok", detail="running")] + [
        Check.model_validate(item) for item in raw
    ]


async def run(
    config: RelayConfig,
    python: str,
    daemon: DaemonChecks = over_ipc,
    runner: local.Runner | None = None,
) -> Report:
    return Report(checks=[*await daemon(), *await local_checks(config, python, runner)])
