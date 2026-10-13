"""Fake adapters for router and core tests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from agent_relay.adapters.base import Adapter, Decision, EventPublisher, ModelInfo, StartRequest
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.events import Harness, Origin, TextDelta
from agent_relay.models.sessions import SessionRecord


class CrashingAdapter(Adapter):
    harness = Harness.CURSOR_CDP
    capabilities = Capabilities()

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, publisher: EventPublisher) -> None:
        self.runs += 1
        raise RuntimeError("boom")

    async def health(self) -> tuple[bool, str]:
        return False, "always crashes"


class EmittingAdapter(Adapter):
    harness = Harness.OPENCODE
    capabilities = Capabilities(cold_start=True, model_select=True, cancel=True, approve=True, question=True)

    def __init__(self, count: int = 5) -> None:
        self.count = count
        self.started: list[StartRequest] = []
        self.start_attachments: list[Sequence[Attachment]] = []
        self.prompts: list[tuple[str, str]] = []
        self.prompt_attachments: list[Sequence[Attachment]] = []
        self.cancelled: list[str] = []
        self.approvals: list[tuple[str, Decision]] = []
        self.reasons: list[tuple[str, str]] = []
        self.answers: list[tuple[str, str | None]] = []
        self.models: list[tuple[str, str]] = []

    async def run(self, publisher: EventPublisher) -> None:
        for i in range(self.count):
            await publisher.publish(TextDelta(harness=self.harness, session_key="ok", text=f"t{i}"))
        await asyncio.Event().wait()

    async def health(self) -> tuple[bool, str]:
        return True, "fake"

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="m1", label="Model One", default=True), ModelInfo(id="m2", label="Model Two")]

    async def start_session(
        self, request: StartRequest, attachments: Sequence[Attachment] = ()
    ) -> SessionRecord:
        self.started.append(request)
        self.start_attachments.append(attachments)
        return SessionRecord(
            session_key=f"opencode:fake{len(self.started)}",
            harness=self.harness,
            harness_id=f"fake{len(self.started)}",
            workspace=request.workspace,
            model=request.model,
            origin=Origin.PHONE,
        )

    async def send_prompt(
        self, session: SessionRecord, text: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        self.prompts.append((session.session_key, text))
        self.prompt_attachments.append(attachments)

    async def set_model(self, session: SessionRecord, model: str) -> None:
        self.models.append((session.session_key, model))

    async def cancel(self, session: SessionRecord) -> None:
        self.cancelled.append(session.session_key)

    async def answer_approval(
        self, session: SessionRecord, request_id: str, decision: Decision, reason: str | None = None
    ) -> None:
        self.approvals.append((request_id, decision))
        if reason is not None:
            self.reasons.append((request_id, reason))

    async def answer_question(self, session: SessionRecord, request_id: str, answer: str | None) -> None:
        self.answers.append((request_id, answer))
