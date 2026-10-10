"""The contract every harness adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from agent_relay.models.capabilities import Capabilities, Capability
from agent_relay.models.events import Event, Harness
from agent_relay.models.sessions import SessionRecord


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


class Adapter(ABC):
    harness: Harness
    capabilities: Capabilities

    @abstractmethod
    async def run(self, publisher: EventPublisher) -> None:
        """Connect and stream native events until cancelled. The router restarts it on failure."""

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """Reachability for `doctor`: (ok, detail)."""

    async def list_models(self) -> list[ModelInfo]:
        raise UnsupportedError(self.harness, Capability.MODEL_SELECT)

    async def start_session(self, request: StartRequest) -> SessionRecord:
        raise UnsupportedError(self.harness, Capability.COLD_START)

    async def send_prompt(self, session: SessionRecord, text: str) -> None:
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
