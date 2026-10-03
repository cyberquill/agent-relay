"""VS Code Local agent chats, driven through the relay-bridge extension.

Hooks carry everything the chat does (tools, approvals, the final reply); this adapter only opens,
steers and cancels chats. Without a bridge window it falls back to `code chat -m agent -r`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from agent_relay import paths
from agent_relay.adapters.base import (
    Adapter,
    AdapterUnavailableError,
    Decision,
    EventPublisher,
    ModelInfo,
    StartRequest,
    UnsupportedError,
)
from agent_relay.adapters.vscode_local.hub import BridgeEvent, BridgeHub, BridgeWindow
from agent_relay.core.claims import UNCLAIMED_PREFIX, make_claim
from agent_relay.core.registry import Registry
from agent_relay.log import get_logger
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.events import ErrorEvent, Harness, Origin
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

type Launcher = Callable[[Sequence[str], Path], Awaitable[int]]

WINDOW_KEY = "local_window"
VIA_KEY = "local_via"
VIA_BRIDGE = "bridge"
VIA_CLI = "cli"
OPEN_TIMEOUT_S = 20.0
LAUNCH_TIMEOUT_S = 20.0
NO_WINDOW = "No VS Code window with the relay-bridge extension is connected."


async def run_code(argv: Sequence[str], cwd: Path) -> int:
    """Run the `code` CLI; it returns once the running VS Code has taken the request."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        return await asyncio.wait_for(proc.wait(), LAUNCH_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise AdapterUnavailableError("the VS Code CLI did not return in time") from None


class VsCodeLocalAdapter(Adapter):
    harness = Harness.VSCODE_LOCAL
    capabilities = Capabilities(cold_start=True, model_select=True, approve=True, cancel=True, wake_idle=True)

    def __init__(
        self,
        registry: Registry,
        hub: BridgeHub,
        launcher: Launcher = run_code,
        cli: Path = paths.VSCODE_CLI,
        open_timeout: float = OPEN_TIMEOUT_S,
    ) -> None:
        self._registry = registry
        self._hub = hub
        self._launch = launcher
        self._cli = cli
        self._open_timeout = open_timeout

    # events

    async def run(self, publisher: EventPublisher) -> None:
        while True:
            event = await self._hub.events.get()
            await self._consume(event, publisher)

    async def _consume(self, event: BridgeEvent, publisher: EventPublisher) -> None:
        if event.name != "error":
            return
        session = await self._session_in(event.window_id)
        if session is None:
            log.info(f"relay-bridge window {event.window_id} reported an error outside a phone chat")
            return
        await publisher.publish(
            ErrorEvent(
                harness=self.harness,
                session_key=session.session_key,
                workspace=session.workspace,
                message=f"VS Code could not run the chat: {event.data.get('message') or 'unknown error'}",
            )
        )
        if session.status is SessionStatus.RUNNING:
            await self._registry.set_session_fields(session.session_key, status=SessionStatus.IDLE.value)

    async def _session_in(self, window_id: str) -> SessionRecord | None:
        sessions = [
            s
            for s in await self._registry.list_sessions()
            if s.harness is self.harness and s.extra.get(WINDOW_KEY) == window_id
        ]
        return max(sessions, key=lambda s: s.updated, default=None)

    # contract

    async def health(self) -> tuple[bool, str]:
        count = len(self._hub.windows)
        if count:
            return True, f"{count} VS Code window(s) connected through relay-bridge"
        if self._cli.exists():
            return True, f"{NO_WINDOW} Falling back to `code chat`."
        return False, f"{NO_WINDOW} The VS Code CLI is missing at {self._cli}."

    async def list_models(self) -> list[ModelInfo]:
        window = self._hub.any_window()
        if window is None:
            raise AdapterUnavailableError(f"{NO_WINDOW} Open VS Code to list its models.")
        result = await window.call("models")
        models: list[ModelInfo] = []
        for item in result if isinstance(result, list) else []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                model_id = str(item["id"])
                models.append(ModelInfo(id=model_id, label=str(item.get("label") or model_id)))
        return models

    async def start_session(self, request: StartRequest) -> SessionRecord:
        workspace = request.workspace
        window = self._hub.window_for(workspace)
        if window is None:
            await self._open_workspace(workspace)
            window = await self._hub.wait_for_window(workspace, self._open_timeout)
        local_id = uuid.uuid4().hex
        session_key = f"{self.harness.value}:phone-{local_id}"
        extra = {WINDOW_KEY: window.window_id, VIA_KEY: VIA_BRIDGE} if window else {VIA_KEY: VIA_CLI}
        await self._registry.upsert_session(
            SessionRecord(
                session_key=session_key,
                harness=self.harness,
                harness_id=f"{UNCLAIMED_PREFIX}{local_id}",
                workspace=workspace,
                model=request.model,
                origin=Origin.PHONE,
                status=SessionStatus.RUNNING,
                title=request.prompt[:80] or None,
                extra=dict(extra),
            )
        )
        await make_claim(self._registry, workspace, session_key)
        try:
            if window is not None:
                await window.call("start", {"query": request.prompt, "model": request.model or ""})
            else:
                await self._cli_chat(workspace, request.prompt, request.model)
        except Exception:
            await self._registry.set_session_fields(session_key, status=SessionStatus.ENDED.value)
            raise
        record = await self._registry.get_session(session_key)
        if record is None:
            raise AdapterUnavailableError(f"session {session_key} vanished from the registry")
        log.info(f"opened a Local chat for {session_key} via {extra[VIA_KEY]}")
        return record

    async def send_prompt(self, session: SessionRecord, text: str) -> None:
        if session.status is SessionStatus.RUNNING:
            raise UnsupportedError(self.harness, "steering a running Local chat")
        await self._deliver(session, text)

    async def wake(self, session: SessionRecord, text: str) -> None:
        await self._deliver(session, text)

    def _phone_opened(self, session: SessionRecord) -> bool:
        return session.extra.get(VIA_KEY) in (VIA_BRIDGE, VIA_CLI)

    async def set_model(self, session: SessionRecord, model: str) -> None:
        await self._registry.set_session_fields(session.session_key, model=model)

    async def cancel(self, session: SessionRecord) -> None:
        if not self._phone_opened(session):
            raise UnsupportedError(self.harness, "stopping a Local chat opened at the desk")
        await self._require_window(session).call("cancel")

    async def answer_approval(self, session: SessionRecord, request_id: str, decision: Decision) -> None:
        command = "skipTool" if decision == "deny" else "acceptTool"
        await self._require_window(session).call(command)

    # helpers

    def _window(self, session: SessionRecord) -> BridgeWindow | None:
        pinned = session.extra.get(WINDOW_KEY)
        if isinstance(pinned, str) and pinned in self._hub.windows:
            return self._hub.windows[pinned]
        return self._hub.window_for(session.workspace) if session.workspace else None

    def _require_window(self, session: SessionRecord) -> BridgeWindow:
        window = self._window(session)
        if window is None:
            raise AdapterUnavailableError(NO_WINDOW)
        return window

    async def _deliver(self, session: SessionRecord, text: str) -> None:
        if not self._phone_opened(session):
            raise UnsupportedError(self.harness, "prompting a Local chat opened at the desk")
        if session.harness_id.startswith(UNCLAIMED_PREFIX) and session.workspace:
            await make_claim(self._registry, session.workspace, session.session_key)
        window = self._window(session)
        if window is not None:
            await window.call("prompt", {"query": text, "model": session.model or ""})
            if session.extra.get(WINDOW_KEY) != window.window_id:
                await self._registry.set_session_extra(session.session_key, WINDOW_KEY, window.window_id)
        elif session.workspace:
            await self._cli_chat(session.workspace, text, session.model)
        else:
            raise AdapterUnavailableError(NO_WINDOW)

    async def _open_workspace(self, workspace: str) -> None:
        if not self._cli.exists():
            raise AdapterUnavailableError(f"The VS Code CLI is missing at {self._cli}.")
        code = await self._launch([str(self._cli), workspace], Path(workspace))
        if code != 0:
            raise AdapterUnavailableError(f"`code {workspace}` exited with {code}")

    async def _cli_chat(self, workspace: str, text: str, model: str | None) -> None:
        if not self._cli.exists():
            raise AdapterUnavailableError(f"The VS Code CLI is missing at {self._cli}.")
        if model:
            log.info(f"`code chat` cannot pick a model; {model} is ignored for this prompt")
        prompt = f" {text}" if text.startswith("-") else text
        code = await self._launch([str(self._cli), "chat", "-m", "agent", "-r", prompt], Path(workspace))
        if code != 0:
            raise AdapterUnavailableError(f"`code chat` exited with {code}")
