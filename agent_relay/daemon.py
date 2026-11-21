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
from pydantic import JsonValue

from agent_relay import doctor, paths
from agent_relay.adapters.factory import build_adapters
from agent_relay.adapters.selftests import build_selftests
from agent_relay.adapters.vscode.hub import BridgeHub
from agent_relay.core import away, phone
from agent_relay.core.approvals import ApprovalBroker
from agent_relay.core.hooks import HookService
from agent_relay.core.housekeeping import housekeeper, reconcile_sessions
from agent_relay.core.lifecycle import Lifecycle
from agent_relay.core.parking import ParkManager
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.send_file import SendFileError, validate_file
from agent_relay.core.service import RelayError, RelayService
from agent_relay.core.signals import Signals
from agent_relay.core.updates import UpdateWatcher
from agent_relay.doctor.daemon import DaemonView, WebexStatus, daemon_checks
from agent_relay.doctor.model import Check
from agent_relay.frontends.pwa.server import build_pwa
from agent_relay.frontends.webex.bot import build_frontend
from agent_relay.frontends.webex.host import MAX_BACKOFF_S, MIN_BACKOFF_S, Built, PostedFile, WebexHost
from agent_relay.frontends.webex.ws_client import Connector, default_connector
from agent_relay.ipc import IpcServer
from agent_relay.log import configure, default_log_file, get_logger, set_debug_bodies
from agent_relay.models.config import RelayConfig
from agent_relay.models.sessions import SessionRecord
from agent_relay.settings import (
    KeychainLockedError,
    MissingSecretError,
    Secret,
    get_secret,
    load_config,
    require_secret,
)

log = get_logger(__name__)

type JsonObj = dict[str, object]
type OpHandler = Callable[[JsonObj], Awaitable[JsonObj]]
type NotifyTarget = Callable[[SessionRecord, str], Awaitable[None]]
type FileTarget = Callable[[SessionRecord, str, PostedFile], Awaitable[bool]]
type TokenSource = Callable[[], str | None]

EXIT_CONFIG = 2
EXIT_RUNNING = 3
ASK_MAX_S = 900
# relayd.err.log only gets what escapes logging (a crash on import); a runaway one is cut at start.
ERR_LOG_MAX_BYTES = 10 * 1024 * 1024
NOT_CONNECTED = "Webex is not connected right now; the file was not sent"


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
    notify_targets: list[NotifyTarget] = field(default_factory=list)
    # Where `send_phone_file` goes: each target says whether it delivered the file.
    file_targets: list[FileTarget] = field(default_factory=list)
    webex: WebexStatus | None = None

    def __post_init__(self) -> None:
        self.lifecycle = Lifecycle(self.registry, self.router, self.signals, self.router.adapter)
        self.router.bind_lifecycle(self.lifecycle)
        self.service = RelayService(self.registry, self.router, self.signals, self.config, self.lifecycle)
        self.broker = ApprovalBroker(
            self.registry, self.router, self.signals, self.service, self.config.questions
        )
        self.router.subscribe(self.broker, desk=True)
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
            "send_file": self._op_send_file,
            "ask": self._op_ask,
            "doctor": self._op_doctor,
        }

    async def daemon_checks(self) -> list[Check]:
        view = DaemonView(self.service, self.registry, self.config, self.webex)
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
        """Away mode is one global flag; a `workspace` from an old caller is ignored."""
        enabled = message.get("enabled")
        if isinstance(enabled, bool):
            await self.service.set_away_for(enabled)
        return {"away": await away.is_away(self.registry)}

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

    async def _op_send_file(self, message: JsonObj) -> JsonObj:
        path, workspace, caption = message.get("path"), message.get("workspace"), message.get("caption")
        if not isinstance(path, str) or not path.strip() or not isinstance(workspace, str):
            return {"error": "send_file needs path and workspace"}
        session, reason = await phone.reachable(self.registry, workspace)
        if session is None:
            return {"delivered": False, "reason": reason}
        # A relative path is the agent's own (its working folder); the jail is the session's workspace.
        base = paths.expand_user(workspace).resolve()
        target = path if path.startswith("~") or os.path.isabs(path) else str(base / path)
        try:
            sendable = validate_file(
                session.workspace or workspace, target, self.config.attachments.max_bytes
            )
        except SendFileError as exc:
            return {"delivered": False, "error": str(exc)}
        text = caption.strip() if isinstance(caption, str) and caption.strip() else sendable.name
        file = (sendable.name, sendable.data, sendable.content_type)
        delivered = False
        for target_fn in self.file_targets:
            try:
                delivered = await target_fn(session, text, file) or delivered
            except Exception:
                log.exception("file target failed")
        if not delivered:
            return {"delivered": False, "reason": NOT_CONNECTED}
        return {"delivered": True, "session": session.session_key, "name": sendable.name}

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
    token_source: TokenSource | None = None,
    webex_backoff: tuple[float, float] = (MIN_BACKOFF_S, MAX_BACKOFF_S),
) -> None:
    """Run relayd until `stop` is set.

    A Webex failure never ends it: the IPC socket keeps serving (hooks fail open fast) while
    `WebexHost` retries with backoff. `token_source` is asked for the token before every attempt, so a
    token stored with `relay setup token` is picked up without a restart; `token` is the fallback.
    """
    registry = Registry(paths.db_path())
    await registry.open()
    core = Core(config, registry)
    recovered = await core.parking.recover()
    if recovered:
        log.info(f"{recovered} session(s) were parked when relayd stopped; marked idle")
    reconciled = await reconcile_sessions(registry, core.lifecycle)
    if reconciled:
        log.info(f"{reconciled} session(s) were running with no live turn; marked idle")
    expired = await core.broker.sweep()
    if expired:
        log.info(f"expired {expired} pending request(s) left from the last run")
    host = WebexHost(backoff=webex_backoff)

    async def build() -> Built:
        return await build_frontend(
            (token_source() if token_source is not None else None) or token,
            config,
            registry,
            core.service,
            http,
            connector,
            doctor=core.doctor_text,
        )

    core.webex = host
    core.router.subscribe(host)
    core.notify_targets.append(lambda session, text: _post(host, session, text))
    core.file_targets.append(lambda session, caption, file: _post_file(host, session, caption, file))
    hub = BridgeHub()
    for adapter in build_adapters(config, registry, hub):
        core.router.add_adapter(adapter)
    ipc = IpcServer(paths.socket_path(), core.handle_ipc, expected_uid=os.getuid(), streams=hub.streams())
    await ipc.start()
    # Update and self-test outcomes go to the log and the doctor; nothing is posted to the phone.
    watcher = UpdateWatcher(registry, build_selftests(config, registry))
    tasks = [
        asyncio.create_task(core.broker.sweeper(), name="pending-sweeper"),
        asyncio.create_task(housekeeper(registry, lifecycle=core.lifecycle), name="housekeeper"),
        asyncio.create_task(watcher.run(), name="update-watcher"),
        asyncio.create_task(host.run(build), name="webex"),
    ]
    pwa = build_pwa(config.pwa, core.service, core.router, pwa_token, core.doctor_text)
    if pwa is not None:
        tasks.append(asyncio.create_task(pwa.server.run(), name="pwa"))
    paused = " (paused by /panic; `/away` resumes)" if paths.pause_flag().exists() else ""
    log.info(f"relayd ready{paused}")
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception() if task is not waiter else None
            if exc is not None:
                log.error(f"{task.get_name()} stopped: {type(exc).__name__}: {exc}", exc_info=exc)
    finally:
        waiter.cancel()
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


async def _post(host: WebexHost, session: SessionRecord, text: str) -> None:
    await host.post_in_thread(session, text)


async def _post_file(host: WebexHost, session: SessionRecord, caption: str, file: PostedFile) -> bool:
    return await host.post_in_thread(session, caption, file=file) is not None


def preflight() -> tuple[RelayConfig, str]:
    """Load config and the bot token, or raise with the command that fixes it."""
    config = load_config()
    token = require_secret(Secret.WEBEX_BOT_TOKEN)
    if not config.webex.allowed_email:
        raise ConfigError(
            "webex.allowed_email is not set. Run: relay config set webex.allowed_email you@example.com"
        )
    return config, token


def trim_err_log(limit: int = ERR_LOG_MAX_BYTES) -> bool:
    """Empty `relayd.err.log` when it is over `limit`: it only holds what escaped logging, never history."""
    file = paths.logs_dir() / "relayd.err.log"
    try:
        if file.stat().st_size <= limit:
            return False
        file.write_bytes(b"")
    except OSError:
        return False
    return True


def main() -> int:
    trim_err_log()
    try:
        config, token = preflight()
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
        await serve(
            config,
            token,
            stop,
            pwa_token=pwa_token,
            token_source=lambda: get_secret(Secret.WEBEX_BOT_TOKEN),
        )

    try:
        asyncio.run(_run())
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
