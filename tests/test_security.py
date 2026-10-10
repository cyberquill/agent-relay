"""AC-40: the §5.10 controls not already proven elsewhere. docs/security.md maps every control to tests."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_relay import log as relay_log
from agent_relay.adapters.factory import build_adapters
from agent_relay.adapters.vscode.hub import BridgeHub
from agent_relay.core.approvals import ALLOW_SESSION_KEY, ApprovalBroker, is_read_only
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayService
from agent_relay.core.signals import Signals
from agent_relay.frontends.pwa import server as pwa_server
from agent_relay.frontends.webex.api import scrub_attachment
from agent_relay.models.config import READ_ONLY_TOOLS, ApprovalConfig, CdpConfig, RelayConfig
from agent_relay.models.events import Harness, Origin
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.scrub import REDACTED

PACKAGE = Path(__file__).resolve().parents[1] / "agent_relay"
LISTENERS = re.compile(
    r"\b(create_server|start_server|HTTPServer|serve_forever|uvicorn\.Config)\(|\.bind\(|\.listen\("
)
FAKE_TOKEN = "ghp_" + "f" * 36


def test_only_the_pwa_opens_a_tcp_listener_and_only_on_loopback() -> None:
    found = {
        str(file.relative_to(PACKAGE))
        for file in PACKAGE.rglob("*.py")
        if LISTENERS.search(file.read_text(encoding="utf-8"))
    }
    assert found == {"frontends/pwa/server.py"}
    source = (PACKAGE / "frontends" / "pwa" / "server.py").read_text(encoding="utf-8")
    assert re.findall(r"host=(\w+)", source) == ["LOOPBACK"]
    assert pwa_server.LOOPBACK == "127.0.0.1"
    assert "start_unix_server" in (PACKAGE / "ipc.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("pattern", ["*", "**", "?*", "[a]*", " * ", "!*"])
def test_no_global_auto_approve(pattern: str) -> None:
    with pytest.raises(ValueError, match="global auto-approve"):
        ApprovalConfig(auto_allow=["read", pattern])


@pytest.mark.parametrize("tool", ["bash", "shell", "run_in_terminal", "edit", "write", "apply_patch", "task"])
def test_approvals_are_manual_by_default_for_anything_that_acts(tool: str) -> None:
    assert ApprovalConfig().auto_allow == list(READ_ONLY_TOOLS)
    assert not is_read_only(tool, ApprovalConfig().auto_allow)


async def test_allow_for_session_ends_with_the_session(registry: Registry) -> None:
    config = RelayConfig()
    router, signals = Router(), Signals()
    service = RelayService(registry, router, signals, config)
    broker = ApprovalBroker(registry, router, signals, service, config.approvals)
    session = SessionRecord(
        session_key="copilot:S1",
        harness=Harness.COPILOT_CLI,
        harness_id="S1",
        workspace="/w",
        origin=Origin.DESK,
    )
    await registry.upsert_session(session)
    key = ALLOW_SESSION_KEY.format(session_key=session.session_key, tool="bash")
    await registry.kv_set(key, "1")
    assert await broker.allowed_for_session(session.session_key, "bash")
    await registry.set_session_fields(session.session_key, status=SessionStatus.ENDED.value)
    assert await registry.kv_get(key) is None
    await registry.kv_set(key, "1")
    assert not await broker.allowed_for_session(session.session_key, "bash")


def test_attachments_are_scrubbed_as_text_and_binary_is_refused() -> None:
    diff = f"+GITHUB_TOKEN={FAKE_TOKEN}\n+print('ok')\n".encode()
    cleaned = scrub_attachment(diff).decode()
    assert FAKE_TOKEN not in cleaned and REDACTED in cleaned and "print('ok')" in cleaned
    with pytest.raises(ValueError, match="UTF-8 text"):
        scrub_attachment(b"\xff\xfe\x00binary")


@pytest.fixture
def bodies() -> Iterator[logging.Logger]:
    logger = relay_log.get_logger("tests.bodies")
    yield logger
    relay_log.set_debug_bodies(False)


def test_message_bodies_are_off_by_default_never_at_info_and_scrubbed(
    bodies: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    assert RelayConfig().debug_bodies is False
    text = f"deploy with token {FAKE_TOKEN}"
    with caplog.at_level(logging.DEBUG, logger="tests.bodies"):
        relay_log.log_body(bodies, "webex in", text)
    assert caplog.records == []
    relay_log.set_debug_bodies(True)
    with caplog.at_level(logging.INFO, logger="tests.bodies"):
        relay_log.log_body(bodies, "webex in", text)
    assert caplog.records == []
    with caplog.at_level(logging.DEBUG, logger="tests.bodies"):
        relay_log.log_body(bodies, "webex in", text + "x" * 3_000)
    (record,) = caplog.records
    assert record.levelno == logging.DEBUG
    assert FAKE_TOKEN not in record.getMessage() and REDACTED in record.getMessage()
    assert record.getMessage().endswith("chars)")


async def test_the_cdp_adapter_exists_only_when_enabled(registry: Registry) -> None:
    off = build_adapters(RelayConfig(), registry, BridgeHub())
    assert Harness.CURSOR_CDP not in {a.harness for a in off}
    on = build_adapters(RelayConfig(cdp=CdpConfig(enabled=True)), registry, BridgeHub())
    assert Harness.CURSOR_CDP in {a.harness for a in on}
