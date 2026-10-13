"""What each adapter can do. The UI hides anything an adapter does not declare."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Capability(StrEnum):
    COLD_START = "cold_start"
    MODEL_SELECT = "model_select"
    TOKEN_STREAM = "token_stream"
    APPROVE = "approve"
    QUESTION = "question"
    CANCEL = "cancel"
    PARK = "park"
    WAKE_IDLE = "wake_idle"
    ATTACH_FILES = "attach_files"


class Capabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    cold_start: bool = False
    model_select: bool = False
    token_stream: bool = False
    approve: bool = False
    question: bool = False
    cancel: bool = False
    park: bool = False
    wake_idle: bool = False
    attach_files: bool = False

    def has(self, capability: Capability) -> bool:
        return bool(getattr(self, capability.value))

    def supported(self) -> tuple[Capability, ...]:
        return tuple(c for c in Capability if self.has(c))


COMMAND_REQUIRES: dict[str, Capability] = {
    "/model": Capability.MODEL_SELECT,
    "/models": Capability.MODEL_SELECT,
    "/stop": Capability.CANCEL,
}
