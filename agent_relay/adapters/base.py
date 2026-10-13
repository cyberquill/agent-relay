"""The contract every harness adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from agent_relay.core.registry import Registry
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capabilities, Capability
from agent_relay.models.events import Event, Harness
from agent_relay.models.sessions import SessionRecord, SessionStatus


class UnsupportedError(RuntimeError):
    """The adapter does not offer this operation. Raised instead of silently doing nothing."""

    def __init__(self, harness: Harness, capability: Capability | str) -> None:
        super().__init__(f"{harness.value} does not support {capability}")
        self.harness = harness
        self.capability = capability


class AdapterUnavailableError(RuntimeError):
    """The adapter cannot reach its harness right now (app closed, auth needed, ...)."""


class ModelInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    default: bool = False


class StartRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace: str
    prompt: str
    model: str | None = None
    isolation: bool = False
    workspace_file: str | None = None


Decision = Literal["allow", "allow_session", "deny"]


class EventPublisher(Protocol):
    async def publish(self, event: Event) -> None: ...


class StatusReporter(Protocol):
    """What an adapter needs from the session lifecycle: report a status it observed."""

    async def observe(self, session_key: str, to: SessionStatus, reason: str) -> SessionRecord | None: ...


def with_attachments(text: str, attachments: Sequence[Attachment]) -> str:
    """The prompt with one `Attached file: <path>` line per file, for apps that cannot take files."""
    if not attachments:
        return text
    lines = "\n".join(f"Attached file: {a.local_path}" for a in attachments)
    return f"{text}\n\n{lines}" if text.strip() else lines


class Adapter(ABC):
    harness: Harness
    capabilities: Capabilities
    # Set by `Router.add_adapter`; every status change an adapter observes goes through it.
    lifecycle: StatusReporter | None = None
    _registry: Registry

    async def report_status(self, session_key: str, status: SessionStatus, reason: str) -> None:
        if self.lifecycle is not None:
            await self.lifecycle.observe(session_key, status, reason)
        else:  # not attached to a router (bare unit rigs): write the status without lifecycle cleanup
            await self._registry.set_session_status(session_key, status)

    def prompt_with_files(self, text: str, attachments: Sequence[Attachment]) -> str:
        """`text` as is for an adapter that takes files natively; otherwise with the file paths appended."""
        if self.capabilities.has(Capability.ATTACH_FILES):
            return text
        return with_attachments(text, attachments)

    @abstractmethod
    async def run(self, publisher: EventPublisher) -> None:
        """Connect and stream native events until cancelled. The router restarts it on failure."""

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """Reachability for `doctor`: (ok, detail)."""

    async def list_models(self) -> list[ModelInfo]:
        raise UnsupportedError(self.harness, Capability.MODEL_SELECT)

    async def start_session(
        self, request: StartRequest, attachments: Sequence[Attachment] = ()
    ) -> SessionRecord:
        raise UnsupportedError(self.harness, Capability.COLD_START)

    async def send_prompt(
        self, session: SessionRecord, text: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        raise UnsupportedError(self.harness, "prompt")

    async def set_model(self, session: SessionRecord, model: str) -> None:
        raise UnsupportedError(self.harness, Capability.MODEL_SELECT)

    async def cancel(self, session: SessionRecord) -> None:
        raise UnsupportedError(self.harness, Capability.CANCEL)

    async def answer_approval(self, session: SessionRecord, request_id: str, decision: Decision) -> None:
        raise UnsupportedError(self.harness, Capability.APPROVE)

    async def answer_question(self, session: SessionRecord, request_id: str, answer: str | None) -> None:
        raise UnsupportedError(self.harness, Capability.QUESTION)

    async def wake(self, session: SessionRecord, text: str) -> None:
        raise UnsupportedError(self.harness, Capability.WAKE_IDLE)

    async def aclose(self) -> None:
        """Release resources. Adapters without resources inherit this no-op deliberately."""
        return None
