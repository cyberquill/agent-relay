"""Phone files reach an adapter as an optional argument; without native support they become prompt paths."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import ValidationError

from agent_relay.adapters.ahp.adapter import AhpAdapter
from agent_relay.adapters.base import Adapter, EventPublisher, with_attachments
from agent_relay.adapters.cursor_cdp.adapter import CursorCdpAdapter
from agent_relay.adapters.cursor_cli.adapter import CursorCliAdapter
from agent_relay.adapters.opencode.adapter import OpenCodeAdapter
from agent_relay.adapters.vscode.adapter import VsCodeAdapter
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capabilities, Capability
from agent_relay.models.config import RelayConfig, Workspace, WorkspacesConfig
from agent_relay.models.events import Harness, Origin
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import save_workspaces
from tests.fakes.adapters import EmittingAdapter

PNG = Attachment(name="a.png", mime="image/png", size=3, local_path="/tmp/relay/a.png", sha256="ab" * 32)
LOG = Attachment(name="b.log", mime="text/plain", size=9, local_path="/tmp/relay/b.log", sha256="cd" * 32)


class Bare(Adapter):
    harness = Harness.OPENCODE

    def __init__(self, capabilities: Capabilities) -> None:
        self.capabilities = capabilities

    async def run(self, publisher: EventPublisher) -> None:
        return None

    async def health(self) -> tuple[bool, str]:
        return True, ""


def test_attachment_is_a_frozen_wire_model() -> None:
    assert Attachment.model_validate_json(PNG.model_dump_json()) == PNG
    with pytest.raises(ValidationError):
        Attachment(name="x", mime="x", size=-1, local_path="/x", sha256="x")
    with pytest.raises(ValidationError):
        PNG.name = "other"  # type: ignore[misc]


def test_paths_are_appended_one_per_line() -> None:
    assert with_attachments("fix it", ()) == "fix it"
    assert with_attachments("fix it", (PNG, LOG)) == (
        "fix it\n\nAttached file: /tmp/relay/a.png\nAttached file: /tmp/relay/b.log"
    )
    assert with_attachments("  ", (PNG,)) == "Attached file: /tmp/relay/a.png"


def test_an_adapter_with_native_support_gets_the_text_untouched() -> None:
    assert Bare(Capabilities()).prompt_with_files("hi", (PNG,)) == "hi\n\nAttached file: /tmp/relay/a.png"
    native = Bare(Capabilities(attach_files=True))
    assert native.capabilities.has(Capability.ATTACH_FILES)
    assert native.prompt_with_files("hi", (PNG,)) == "hi"


def test_no_adapter_claims_native_file_support_yet() -> None:
    for adapter in (AhpAdapter, CursorCdpAdapter, CursorCliAdapter, OpenCodeAdapter, VsCodeAdapter):
        assert not adapter.capabilities.has(Capability.ATTACH_FILES), adapter.__name__
        assert Capability.ATTACH_FILES not in adapter.capabilities.supported()


@pytest.fixture
async def core(registry: Registry, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Core]:
    save_workspaces(
        WorkspacesConfig(workspaces=[Workspace(name="proj", path=str(tmp_path_factory.mktemp("ws")))])
    )
    built = Core(RelayConfig(), registry)
    yield built
    await built.router.stop()


async def test_the_service_hands_attachments_to_the_adapter(core: Core) -> None:
    adapter = EmittingAdapter(count=0)
    core.router.add_adapter(adapter)
    started = await core.service.start("opencode", "proj", "look at this", attachments=(PNG,))
    assert adapter.start_attachments == [(PNG,)]
    await core.registry.set_session_status(started.session_key, SessionStatus.IDLE)
    assert await core.service.prompt(started.session_key, "and this", (LOG,)) == "sent"
    assert adapter.prompt_attachments == [(LOG,)]


async def test_queued_messages_carry_the_paths_in_their_text(core: Core) -> None:
    key = "opencode:q1"
    await core.registry.create_session(
        SessionRecord(
            session_key=key,
            harness=Harness.OPENCODE,
            harness_id="q1",
            workspace="/w",
            origin=Origin.PHONE,
            status=SessionStatus.IDLE,
        )
    )
    assert (await core.service.prompt(key, "later", (PNG,))).startswith("queued")
    queued = await core.registry.pop_queued(key, ("prompt",))
    assert [q.text for q in queued] == ["later\n\nAttached file: /tmp/relay/a.png"]
