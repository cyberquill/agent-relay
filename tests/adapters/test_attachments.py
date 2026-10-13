"""Phone files reach an adapter as an optional argument; native where the app takes them."""

from __future__ import annotations

import base64
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, ValidationError

from agent_relay.adapters.ahp import uris
from agent_relay.adapters.ahp.adapter import AhpAdapter
from agent_relay.adapters.base import Adapter, EventPublisher, StartRequest, with_attachments
from agent_relay.adapters.cursor_cdp.adapter import CursorCdpAdapter
from agent_relay.adapters.cursor_cli.adapter import CursorCliAdapter
from agent_relay.adapters.cursor_cli.blocks import prompt_blocks
from agent_relay.adapters.cursor_cli.catalog import CursorCli
from agent_relay.adapters.opencode.adapter import OpenCodeAdapter
from agent_relay.adapters.vscode.adapter import VsCodeAdapter
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capabilities, Capability
from agent_relay.models.config import OpenCodeConfig, RelayConfig, Workspace, WorkspacesConfig
from agent_relay.models.events import Harness, Origin
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import save_workspaces
from tests.adapters import test_ahp
from tests.adapters.test_ahp import Rig as AhpRig
from tests.adapters.test_ahp import quiet_start, until
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


def test_the_apps_that_take_files_natively_say_so_and_the_rest_get_paths() -> None:
    for adapter in (AhpAdapter, CursorCliAdapter, OpenCodeAdapter):
        assert adapter.capabilities.has(Capability.ATTACH_FILES), adapter.__name__
    for other in (CursorCdpAdapter, VsCodeAdapter):
        assert not other.capabilities.has(Capability.ATTACH_FILES), other.__name__
        assert Capability.ATTACH_FILES not in other.capabilities.supported()


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


def phone_file(folder: Path, name: str, data: bytes, mime: str) -> Attachment:
    target = folder / name
    target.write_bytes(data)
    return Attachment(name=name, mime=mime, size=len(data), local_path=str(target), sha256="0" * 64)


# AHP

rig = test_ahp.rig


def message_of(action: dict[str, JsonValue]) -> dict[str, JsonValue]:
    message = action["message"]
    assert isinstance(message, dict)
    return message


async def test_ahp_sends_an_image_inline_and_any_other_file_by_uri(rig: AhpRig, tmp_path: Path) -> None:
    shot = phone_file(tmp_path, "shot.png", b"\x89PNG\r\n", "image/png")
    log = phone_file(tmp_path, "run.log", b"boom", "text/plain")
    await rig.core.service.start("copilot", "proj", "look", attachments=(shot, log))
    await until(lambda: rig.host.actions("chat/turnStarted"))
    (turn,) = rig.host.actions("chat/turnStarted")
    message = message_of(turn)
    assert message["text"] == "look"
    assert message["attachments"] == [
        {
            "type": "embeddedResource",
            "label": "shot.png",
            "displayKind": "image",
            "data": base64.b64encode(b"\x89PNG\r\n").decode(),
            "contentType": "image/png",
        },
        {
            "type": "resource",
            "label": "run.log",
            "displayKind": "document",
            "uri": uris.file_uri(str(tmp_path / "run.log")),
            "sizeHint": 4,
            "contentType": "text/plain",
        },
    ]


async def test_ahp_turns_without_files_carry_no_attachments_key(rig: AhpRig) -> None:
    await rig.core.service.start("copilot", "proj", "plain")
    await until(lambda: rig.host.actions("chat/turnStarted"))
    assert "attachments" not in message_of(rig.host.actions("chat/turnStarted")[0])


async def test_ahp_steering_carries_the_files_too(rig: AhpRig, tmp_path: Path) -> None:
    record, _, _ = await quiet_start(rig)
    log = phone_file(tmp_path, "run.log", b"boom", "text/plain")
    await rig.adapter.send_prompt(record, "and this", (log,))
    await until(lambda: rig.host.actions("chat/pendingMessageSet"))
    (steer,) = rig.host.actions("chat/pendingMessageSet")
    message = message_of(steer)
    assert message["text"] == "and this"
    attachments = message["attachments"]
    assert isinstance(attachments, list) and len(attachments) == 1
    first = attachments[0]
    assert isinstance(first, dict) and first["uri"] == uris.file_uri(log.local_path)


async def test_ahp_sends_an_unreadable_image_as_a_reference(rig: AhpRig, tmp_path: Path) -> None:
    gone = Attachment(
        name="gone.png", mime="image/png", size=3, local_path=str(tmp_path / "gone.png"), sha256="0" * 64
    )
    await rig.core.service.start("copilot", "proj", "look", attachments=(gone,))
    await until(lambda: rig.host.actions("chat/turnStarted"))
    attachments = message_of(rig.host.actions("chat/turnStarted")[0])["attachments"]
    assert isinstance(attachments, list)
    first = attachments[0]
    assert isinstance(first, dict) and first["type"] == "resource" and first["label"] == "gone.png"


# Cursor CLI

FAKES = Path(__file__).resolve().parents[1] / "fakes"


@dataclass
class AcpRig:
    core: Core
    registry: Registry
    fake_dir: Path
    images: bool

    def prompts(self) -> list[list[JsonValue]]:
        log = self.fake_dir / "acp.log"
        lines = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [m["params"]["prompt"] for m in lines if m.get("method") == "session/prompt"]

    async def idle(self, key: str) -> None:
        async def done() -> bool:
            found = await self.registry.get_session(key)
            return found is not None and found.status is SessionStatus.IDLE

        await until(done)


@pytest.fixture(params=[False, True], ids=["agent-without-images", "agent-with-images"])
async def acp(
    request: pytest.FixtureRequest, registry: Registry, relay_home: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AcpRig]:
    images: bool = request.param
    fake_dir = relay_home / "acp"
    fake_dir.mkdir()
    monkeypatch.setenv("FAKE_ACP_DIR", str(fake_dir))
    script = FAKES / ("acp_images.py" if images else "acp_agent.py")
    shim = relay_home / "cursor-agent"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    shim.chmod(0o755)
    workspace = relay_home / "ws"
    workspace.mkdir()
    save_workspaces(WorkspacesConfig(workspaces=[Workspace(name="proj", path=str(workspace))]))
    core = Core(RelayConfig(), registry)
    adapter = CursorCliAdapter(registry, CursorCli(shim), init_timeout=10)
    core.router.add_adapter(adapter)
    yield AcpRig(core, registry, fake_dir, images)
    await adapter.aclose()
    await core.router.stop()


async def test_cursor_cli_sends_images_as_image_blocks_only_when_the_agent_takes_them(
    acp: AcpRig, tmp_path: Path
) -> None:
    shot = phone_file(tmp_path, "shot.png", b"\x89PNG\r\n", "image/png")
    log = phone_file(tmp_path, "run.log", b"boom", "text/plain")
    record = await acp.core.service.start("cursor", "proj", "look", attachments=(shot, log))
    await acp.idle(record.session_key)
    (blocks,) = acp.prompts()
    shot_uri = (tmp_path / "shot.png").resolve().as_uri()
    image: JsonValue = (
        {
            "type": "image",
            "data": base64.b64encode(b"\x89PNG\r\n").decode(),
            "mimeType": "image/png",
            "uri": shot_uri,
        }
        if acp.images
        else {
            "type": "resource_link",
            "uri": shot_uri,
            "name": "shot.png",
            "mimeType": "image/png",
            "size": 6,
        }
    )
    assert blocks == [
        {"type": "text", "text": "look"},
        image,
        {
            "type": "resource_link",
            "uri": (tmp_path / "run.log").resolve().as_uri(),
            "name": "run.log",
            "mimeType": "text/plain",
            "size": 4,
        },
    ]


async def test_cursor_cli_follow_up_prompts_carry_files_as_blocks(acp: AcpRig, tmp_path: Path) -> None:
    record = await acp.core.service.start("cursor", "proj", "hello")
    await acp.idle(record.session_key)
    log = phone_file(tmp_path, "run.log", b"boom", "text/plain")
    assert await acp.core.service.prompt(record.session_key, "now this", (log,)) == "sent"
    await until(lambda: len(acp.prompts()) == 2)
    assert acp.prompts()[0] == [{"type": "text", "text": "hello"}]
    second = acp.prompts()[1]
    assert second[0] == {"type": "text", "text": "now this"}
    assert isinstance(second[1], dict) and second[1]["type"] == "resource_link"


async def test_an_unreadable_image_becomes_a_link_even_for_an_image_agent(tmp_path: Path) -> None:
    gone = Attachment(
        name="gone.png", mime="image/png", size=3, local_path=str(tmp_path / "gone.png"), sha256="0" * 64
    )
    (_, block) = await prompt_blocks("t", (gone,), images=True)
    assert isinstance(block, dict) and block["type"] == "resource_link"


# OpenCode


def opencode_adapter(registry: Registry) -> tuple[OpenCodeAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(200, json={"id": "s1"})
        return httpx.Response(204)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenCodeAdapter(OpenCodeConfig(), registry, None, http=http), seen


def parts_of(request: httpx.Request) -> list[JsonValue]:
    parts: list[JsonValue] = json.loads(request.content)["parts"]
    return parts


async def test_opencode_prompts_carry_files_as_file_parts(registry: Registry, tmp_path: Path) -> None:
    adapter, seen = opencode_adapter(registry)
    shot = phone_file(tmp_path, "shot.png", b"\x89PNG", "image/png")
    log = phone_file(tmp_path, "run.log", b"boom", "text/plain")
    record = await adapter.start_session(StartRequest(workspace=str(tmp_path), prompt="look"), (shot, log))
    (prompt,) = [r for r in seen if r.url.path.endswith("/prompt_async")]
    assert parts_of(prompt) == [
        {"type": "text", "text": "look"},
        {
            "type": "file",
            "mime": "image/png",
            "filename": "shot.png",
            "url": (tmp_path / "shot.png").resolve().as_uri(),
        },
        {
            "type": "file",
            "mime": "text/plain",
            "filename": "run.log",
            "url": (tmp_path / "run.log").resolve().as_uri(),
        },
    ]
    await adapter.send_prompt(record, "again", (log,))
    last = [r for r in seen if r.url.path.endswith("/prompt_async")][-1]
    kinds = [p.get("type") for p in parts_of(last) if isinstance(p, dict)]
    assert kinds == ["text", "file"]
    assert parts_of(last)[0] == {"type": "text", "text": "again"}


async def test_opencode_prompts_without_files_are_a_single_text_part(
    registry: Registry, tmp_path: Path
) -> None:
    adapter, seen = opencode_adapter(registry)
    await adapter.start_session(StartRequest(workspace=str(tmp_path), prompt="plain"))
    (prompt,) = [r for r in seen if r.url.path.endswith("/prompt_async")]
    assert parts_of(prompt) == [{"type": "text", "text": "plain"}]
