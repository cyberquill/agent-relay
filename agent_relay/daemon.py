"""relayd: the long-running daemon (launchd agent). Webex bot, registry, router, hooks and adapters."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import IO

import httpx
from pydantic import JsonValue

from agent_relay import doctor, paths
from agent_relay.adapters.factory import build_adapters
from agent_relay.adapters.selftests import build_selftests
from agent_relay.adapters.vscode.hub import BridgeHub
from agent_relay.core import away, phone
from agent_relay.core.approvals import ApprovalBroker
from agent_relay.core.hooks import HookService
from agent_relay.core.housekeeping import housekeeper
from agent_relay.core.lifecycle import Lifecycle
from agent_relay.core.parking import ParkManager
from agent_relay.core.presence import PresenceMonitor, Probe, probe_lock_state
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayError, RelayService
from agent_relay.core.signals import Signals
from agent_relay.core.updates import UpdateWatcher
from agent_relay.doctor.daemon import DaemonView, WebexLink, daemon_checks
from agent_relay.doctor.model import Check
from agent_relay.frontends.pwa.server import build_pwa
from agent_relay.frontends.webex.bot import WebexFrontend, build_frontend
from agent_relay.frontends.webex.routing import BotRenderers
from agent_relay.frontends.webex.ws_client import Connector, WebexWsClient, default_connector
from agent_relay.ipc import IpcServer
from agent_relay.log import configure, default_log_file, get_logger, set_debug_bodies
from agent_relay.models.config import CONTROL, BotName, RelayConfig
from agent_relay.models.sessions import SessionRecord
from agent_relay.settings import (
    KeychainLockedError,
    MissingSecretError,
    Secret,
    bot_item,
    configured_bots,
    load_config,
    require_secret,
)

log = get_logger(__name__)

type JsonObj = dict[str, object]
type OpHandler = Callable[[JsonObj], Awaitable[JsonObj]]

EXIT_CONFIG = 2
EXIT_RUNNING = 3
ASK_MAX_S = 900


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
    lifecycle: Lifecycle = field(init=False)
    service: RelayService = field(init=False)
    broker: ApprovalBroker = field(init=False)
    parking: ParkManager = field(init=False)
    hooks: HookService = field(init=False)
    notify_targets: list[Callable[[SessionRecord, str], Awaitable[None]]] = field(default_factory=list)
    webex: WebexLink | None = None
    # Every Webex bot's link by name; `webex` is the control bot's.
    webex_bots: dict[str, WebexLink] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.lifecycle = Lifecycle(self.registry, self.router, self.signals, self.router.adapter)
        self.router.bind_lifecycle(self.lifecycle)
        self.service = RelayService(self.registry, self.router, self.signals, self.config, self.lifecycle)
        self.broker = ApprovalBroker(
            self.registry, self.router, self.signals, self.service, self.config.approvals
        )
        self.router.subscribe(self.broker)
        self.parking = ParkManager(
            self.registry, self.router, self.signals, self.config.park, self.notify, self.lifecycle
        )
        self.hooks = HookService(self.registry, self.router, self.broker, self.parking, self.lifecycle)

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
            "sessions": self._op_sessions,
            "ping": self._op_ping,
            "notify": self._op_notify,
            "ask": self._op_ask,
            "doctor": self._op_doctor,
        }

    async def daemon_checks(self) -> list[Check]:
        view = DaemonView(self.service, self.registry, self.config, self.webex, self.webex_bots)
        return await daemon_checks(view)

    async def doctor_text(self) -> str:
        report = await doctor.run(self.config, sys.executable, self.daemon_checks)
        return report.render()

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
        return dict(await self.hooks.handle(message))

    async def _op_away(self, message: JsonObj) -> JsonObj:
        enabled = message.get("enabled")
        workspace = message.get("workspace")
        if isinstance(enabled, bool):
            await self.service.set_away_for(enabled, workspace if isinstance(workspace, str) else None)
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

    async def _op_sessions(self, _: JsonObj) -> JsonObj:
        sessions: list[JsonValue] = [
            {
                "session_key": s.session_key,
                "harness": s.harness.value,
                "status": s.status.value,
                "origin": s.origin.value,
                "workspace": s.workspace,
                "updated": s.updated.isoformat(),
            }
            for s in await self.registry.list_sessions()
        ]
        return {"sessions": sessions}

    async def _op_ping(self, _: JsonObj) -> JsonObj:
        return {"ok": True, "pid": os.getpid()}

    async def _op_doctor(self, _: JsonObj) -> JsonObj:
        return {"checks": [check.model_dump() for check in await self.daemon_checks()]}

    # relay-mcp

    async def _op_notify(self, message: JsonObj) -> JsonObj:
        text, workspace = message.get("text"), message.get("workspace")
        if not isinstance(text, str) or not text.strip() or not isinstance(workspace, str):
            return {"error": "notify needs text and workspace"}
        session, reason = await phone.reachable(self.registry, workspace)
        if session is None:
            return {"delivered": False, "reason": reason}
        await self.notify(session, text)
        return {"delivered": True, "session": session.session_key}

    async def _op_ask(self, message: JsonObj) -> JsonObj:
        question, workspace = message.get("question"), message.get("workspace")
        if not isinstance(question, str) or not question.strip() or not isinstance(workspace, str):
            return {"error": "ask needs question and workspace"}
        raw_options = message.get("options")
        options = tuple(str(o) for o in raw_options) if isinstance(raw_options, list) else ()
        raw_timeout = message.get("timeout_s")
        timeout = float(raw_timeout) if isinstance(raw_timeout, int | float) else float(ASK_MAX_S)
        timeout = min(max(timeout, 1.0), float(ASK_MAX_S))
        session, reason = await phone.reachable(self.registry, workspace)
        if session is None:
            return {"outcome": "denied", "reason": reason}
        answer, by = await self.broker.ask_outcome(session, question, options, timeout, source="mcp")
        if by == "deadline":
            return {"outcome": "timeout"}
        if answer is None:
            return {"outcome": "denied", "reason": f"skipped ({by})"}
        return {"outcome": "answered", "answer": answer}


async def serve(
    config: RelayConfig,
    token: str,
    stop: asyncio.Event,
    http: httpx.AsyncClient | None = None,
    connector: Connector = default_connector,
    pwa_token: str | None = None,
    lock_probe: Probe = probe_lock_state,
    bot_tokens: Mapping[BotName, str] | None = None,
    bot_http: Mapping[BotName, httpx.AsyncClient] | None = None,
) -> None:
    """Run relayd. `token` is the control bot's; `bot_tokens` holds one per app bot in `webex.bots`."""
    bots = configured_bots(config.webex)
    tokens: dict[BotName, str] = {CONTROL: token, **(bot_tokens or {})}
    if missing := [bot for bot in bots if bot not in tokens]:
        raise ConfigError(f"no Webex token for bot(s): {', '.join(missing)}")
    registry = Registry(paths.db_path())
    await registry.open()
    core = Core(config, registry)
    recovered = await core.parking.recover()
    if recovered:
        log.info(f"{recovered} session(s) were parked when relayd stopped; marked idle")
    expired = await core.broker.sweep()
    if expired:
        log.info(f"expired {expired} pending request(s) left from the last run")
    renderers = BotRenderers(registry)
    frontends: dict[BotName, WebexFrontend] = {}
    sockets: dict[BotName, WebexWsClient] = {}
    for bot in bots:
        frontends[bot], sockets[bot] = await build_frontend(
            tokens[bot],
            config,
            registry,
            core.service,
            http if bot == CONTROL else (bot_http or {}).get(bot),
            connector,
            doctor=core.doctor_text,
            bot=bot,
            renderers=renderers,
        )
    core.webex = sockets[CONTROL]
    for bot, link in sockets.items():
        core.webex_bots[bot] = link
    core.router.subscribe(renderers)
    core.notify_targets.append(lambda s, text: _post(renderers, s, text))
    hub = BridgeHub()
    for adapter in build_adapters(config, registry, hub):
        core.router.add_adapter(adapter)
    ipc = IpcServer(paths.socket_path(), core.handle_ipc, expected_uid=os.getuid(), streams=hub.streams())
    await ipc.start()

    async def post_top(text: str) -> None:
        await renderers.post_top(text)

    for bot, frontend in frontends.items():
        await _hello(frontend, bot)

    watcher = UpdateWatcher(registry, build_selftests(config, registry), post_top)
    tasks = [
        asyncio.create_task(core.broker.sweeper(), name="pending-sweeper"),
        asyncio.create_task(housekeeper(registry), name="housekeeper"),
        asyncio.create_task(watcher.run(), name="update-watcher"),
    ]
    for bot in bots:
        suffix = "" if bot == CONTROL else f":{bot}"
        tasks.append(asyncio.create_task(sockets[bot].run(), name=f"webex-ws{suffix}"))
        tasks.append(asyncio.create_task(frontends[bot].ticker(), name=f"renderer-tick{suffix}"))
    pwa = build_pwa(config.pwa, core.service, core.router, pwa_token, core.doctor_text)
    if pwa is not None:
        tasks.append(asyncio.create_task(pwa.server.run(), name="pwa"))
    if config.presence.auto_away_on_lock:
        monitor = PresenceMonitor(registry, core.service, config.presence, post_top, lock_probe)
        tasks.append(asyncio.create_task(monitor.run(), name="presence"))
    log.info("relayd ready")
    announce = asyncio.create_task(_announce(post_top, online_message(recovered)), name="online")
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception() if task is not waiter else None
            if exc is not None:
                log.error(f"{task.get_name()} stopped: {type(exc).__name__}: {exc}", exc_info=exc)
    finally:
        waiter.cancel()
        announce.cancel()
        if pwa is not None:
            pwa.server.stop()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await ipc.stop()
        await core.router.stop()
        await registry.close()
        log.info("relayd stopped")


def online_message(recovered: int) -> str:
    lines = ["🖥️ **Mac online** — relayd started."]
    if recovered:
        lines.append(f"{recovered} session(s) were parked when it stopped; they are idle now.")
    if paths.pause_flag().exists():
        lines.append("Relaying is paused (/panic). `/away on` resumes.")
    return "\n\n".join(lines)


async def _announce(post_top: Callable[[str], Awaitable[None]], text: str) -> None:
    try:
        await post_top(text)
    except Exception as exc:
        log.warning(f"could not post 'Mac online': {exc.__class__.__name__}")


async def _post(renderers: BotRenderers, session: SessionRecord, text: str) -> None:
    await renderers.post_in_thread(session, text)


async def _hello(frontend: WebexFrontend, bot: BotName) -> None:
    """Open an app bot's space on its first start; a failure is logged and tried again next start."""
    try:
        if await frontend.hello():
            log.info(f"opened the {bot} bot's space")
    except Exception as exc:
        log.warning(f"could not open the {bot} bot's space: {exc.__class__.__name__}")


def preflight() -> tuple[RelayConfig, str]:
    """Load config and the bot token, or raise with the command that fixes it."""
    config = load_config()
    token = require_secret(bot_item(config.webex, CONTROL))
    if not config.webex.allowed_email:
        raise ConfigError(
            "webex.allowed_email is not set. Run: relay config set webex.allowed_email you@example.com"
        )
    return config, token


def preflight_bots(config: RelayConfig) -> dict[BotName, str]:
    """The token of every app bot in `webex.bots`, or raise with the command that stores it."""
    return {
        bot: require_secret(bot_item(config.webex, bot))
        for bot in configured_bots(config.webex)
        if bot != CONTROL
    }


def main() -> int:
    try:
        config, token = preflight()
        bot_tokens = preflight_bots(config)
        pwa_token = require_secret(Secret.PWA_TOKEN) if config.pwa.enabled else None
    except (MissingSecretError, KeychainLockedError, ConfigError) as exc:
        sys.stderr.write(f"relayd: {exc}\n")
        return EXIT_CONFIG
    level = logging.getLevelNamesMapping().get(config.log_level.upper(), logging.INFO)
    configure(level, default_log_file("relayd"), console=sys.stderr.isatty())
    set_debug_bodies(config.debug_bodies)
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
        await serve(config, token, stop, pwa_token=pwa_token, bot_tokens=bot_tokens)

    try:
        asyncio.run(_run())
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
