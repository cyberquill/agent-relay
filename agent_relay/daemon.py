"""relayd: the long-running daemon (launchd agent). Webex bot, registry, router, hooks and adapters."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import IO

import httpx

from agent_relay import paths
from agent_relay.adapters.factory import build_adapters
from agent_relay.adapters.vscode_local.hub import BridgeHub
from agent_relay.core import away
from agent_relay.core.approvals import ApprovalBroker
from agent_relay.core.hooks import HookService
from agent_relay.core.parking import ParkManager
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayError, RelayService
from agent_relay.core.signals import Signals
from agent_relay.frontends.webex.bot import WebexFrontend, build_frontend
from agent_relay.frontends.webex.ws_client import Connector, WebexWsClient, default_connector
from agent_relay.ipc import IpcServer
from agent_relay.log import configure, default_log_file, get_logger
from agent_relay.models.config import RelayConfig
from agent_relay.models.sessions import SessionRecord
from agent_relay.settings import MissingSecretError, Secret, load_config, require_secret

log = get_logger(__name__)

type JsonObj = dict[str, object]
type OpHandler = Callable[[JsonObj], Awaitable[JsonObj]]

EXIT_CONFIG = 2
EXIT_RUNNING = 3


class AlreadyRunningError(RuntimeError):
    pass


class ConfigError(RuntimeError):
    pass


def acquire_lock() -> IO[str]:
    """Single instance: hold an exclusive flock for the life of the process."""
    lock = paths.lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise AlreadyRunningError("relayd is already running") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


@dataclass
class Core:
    """Everything except the Webex front end, so tests and the PWA can share it."""

    config: RelayConfig
    registry: Registry
    router: Router = field(default_factory=Router)
    signals: Signals = field(default_factory=Signals)
    service: RelayService = field(init=False)
    broker: ApprovalBroker = field(init=False)
    parking: ParkManager | None = None
    hooks: HookService | None = None
    notify_targets: list[Callable[[SessionRecord, str], Awaitable[None]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.service = RelayService(self.registry, self.router, self.signals, self.config)
        self.broker = ApprovalBroker(
            self.registry, self.router, self.signals, self.service, self.config.approvals
        )
        self.router.subscribe(self.broker)
        self.parking = ParkManager(self.registry, self.router, self.signals, self.config.park, self.notify)
        self.hooks = HookService(self.registry, self.router, self.broker, self.parking)

    async def notify(self, session: SessionRecord, text: str) -> None:
        for target in self.notify_targets:
            try:
                await target(session, text)
            except Exception:
                log.exception("notify target failed")

    def ops(self) -> dict[str, OpHandler]:
        return {
            "hook": self._op_hook,
            "away": self._op_away,
            "status": self._op_status,
            "ping": self._op_ping,
        }

    async def handle_ipc(self, message: JsonObj) -> JsonObj:
        op = message.get("op")
        handler = self.ops().get(op) if isinstance(op, str) else None
        if handler is None:
            return {"error": f"unknown op {op!r}"}
        try:
            return await handler(message)
        except RelayError as exc:
            return {"error": str(exc)}

    async def _op_hook(self, message: JsonObj) -> JsonObj:
        assert self.hooks is not None
        return dict(await self.hooks.handle(message))

    async def _op_away(self, message: JsonObj) -> JsonObj:
        enabled = message.get("enabled")
        workspace = message.get("workspace")
        if isinstance(enabled, bool):
            await away.set_away(self.registry, enabled, workspace if isinstance(workspace, str) else None)
            if enabled:
                paths.pause_flag().unlink(missing_ok=True)
        current = await away.is_away(self.registry, workspace if isinstance(workspace, str) else None)
        return {"away": current}

    async def _op_status(self, _: JsonObj) -> JsonObj:
        sessions = await self.registry.list_sessions()
        pending = await self.registry.open_pending()
        return {
            "sessions": len(sessions),
            "parked": sum(1 for s in sessions if s.status.value == "parked"),
            "pending": len(pending),
            "adapters": {h.value: self.router.adapter_failures.get(h, 0) for h in self.router.adapters()},
            "paused": paths.pause_flag().exists(),
        }

    async def _op_ping(self, _: JsonObj) -> JsonObj:
        return {"ok": True, "pid": os.getpid()}


async def serve(
    config: RelayConfig,
    token: str,
    stop: asyncio.Event,
    http: httpx.AsyncClient | None = None,
    connector: Connector = default_connector,
) -> None:
    registry = Registry(paths.db_path())
    await registry.open()
    core = Core(config, registry)
    assert core.parking is not None
    recovered = await core.parking.recover()
    if recovered:
        log.info(f"{recovered} session(s) were parked when relayd stopped; marked idle")
    expired = await core.broker.sweep()
    if expired:
        log.info(f"expired {expired} pending request(s) left from the last run")
    frontend: WebexFrontend
    ws: WebexWsClient
    frontend, ws = await build_frontend(token, config, registry, core.service, http, connector)
    core.router.subscribe(frontend.renderer)
    core.notify_targets.append(lambda s, text: _post(frontend, s, text))
    hub = BridgeHub()
    for adapter in build_adapters(config, registry, hub):
        core.router.add_adapter(adapter)
    ipc = IpcServer(paths.socket_path(), core.handle_ipc, expected_uid=os.getuid(), streams=hub.streams())
    await ipc.start()
    tasks = [
        asyncio.create_task(ws.run(), name="webex-ws"),
        asyncio.create_task(frontend.ticker(), name="renderer-tick"),
        asyncio.create_task(core.broker.sweeper(), name="pending-sweeper"),
    ]
    log.info("relayd ready")
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not waiter and task.exception() is not None:
                log.error(f"{task.get_name()} stopped: {task.exception()}")
    finally:
        waiter.cancel()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await ipc.stop()
        await core.router.stop()
        await registry.close()
        log.info("relayd stopped")


async def _post(frontend: WebexFrontend, session: SessionRecord, text: str) -> None:
    await frontend.renderer.post_in_thread(session, text)


def preflight() -> tuple[RelayConfig, str]:
    """Load config and the bot token, or raise with the command that fixes it."""
    config = load_config()
    token = require_secret(Secret.WEBEX_BOT_TOKEN)
    if not config.webex.allowed_email:
        raise ConfigError(
            "webex.allowed_email is not set. Run: relay config set webex.allowed_email you@example.com"
        )
    return config, token


def main() -> int:
    try:
        config, token = preflight()
    except (MissingSecretError, ConfigError) as exc:
        sys.stderr.write(f"relayd: {exc}\n")
        return EXIT_CONFIG
    level = logging.getLevelNamesMapping().get(config.log_level.upper(), logging.INFO)
    configure(level, default_log_file("relayd"), console=True)
    try:
        lock = acquire_lock()
    except AlreadyRunningError as exc:
        sys.stderr.write(f"relayd: {exc}\n")
        return EXIT_RUNNING

    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await serve(config, token, stop)

    try:
        asyncio.run(_run())
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
