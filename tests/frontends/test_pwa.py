"""AC-14: the PWA is off unless enabled, binds only to loopback, and every API route needs the token."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue
from typer.testing import CliRunner

from agent_relay.adapters.base import Decision
from agent_relay.cli import app as cli
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayService
from agent_relay.core.service.common import DeliveryError
from agent_relay.core.signals import Signals
from agent_relay.frontends import card_state
from agent_relay.frontends.pwa.app import STATIC, build_app
from agent_relay.frontends.pwa.hub import PwaHub
from agent_relay.frontends.pwa.server import LOOPBACK, Pwa, build_pwa
from agent_relay.frontends.registry import help_text
from agent_relay.frontends.webex.handlers import Handlers
from agent_relay.models.config import PwaConfig, RelayConfig
from agent_relay.models.events import ApprovalRequested, Harness, QuestionAsked, ResolvedBy, TextDelta
from agent_relay.models.sessions import PendingRequest, SessionRecord
from agent_relay.scrub import REDACTED
from tests.fakes.adapters import EmittingAdapter

TOKEN = "fake-pwa-token-0000"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        port: int = sock.getsockname()[1]
        return port


def refused(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((LOOPBACK, port)) != 0


def service(registry: Registry) -> tuple[RelayService, Router]:
    router = Router()
    return RelayService(registry, router, Signals(), RelayConfig()), router


@pytest.fixture
async def running(registry: Registry) -> AsyncIterator[tuple[Pwa, int]]:
    svc, router = service(registry)
    port = free_port()
    pwa = build_pwa(PwaConfig(enabled=True, port=port), svc, router, TOKEN)
    assert pwa is not None
    task = asyncio.create_task(pwa.server.run())
    for _ in range(100):
        if pwa.server.started:
            break
        await asyncio.sleep(0.05)
    assert pwa.server.started
    yield pwa, port
    pwa.server.stop()
    await asyncio.wait_for(task, 5)
    await router.stop()


async def test_disabled_builds_nothing_and_binds_nothing(registry: Registry) -> None:
    svc, router = service(registry)
    port = free_port()
    assert build_pwa(PwaConfig(enabled=False, port=port), svc, router, TOKEN) is None
    assert refused(port)


def test_enabled_without_a_token_fails_closed(registry: Registry) -> None:
    svc, router = service(registry)
    with pytest.raises(ValueError, match="no PWA token"):
        build_pwa(PwaConfig(enabled=True), svc, router, None)
    with pytest.raises(ValueError, match="non-empty"):
        build_app(PwaHub(), svc, Handlers(svc, PwaHub()), "")


async def test_it_binds_only_to_loopback(running: tuple[Pwa, int]) -> None:
    pwa, port = running
    assert pwa.server.bound() == [(LOOPBACK, port)]


async def test_api_routes_need_the_bearer_token(running: tuple[Pwa, int]) -> None:
    _, port = running
    async with httpx.AsyncClient(base_url=f"http://{LOOPBACK}:{port}") as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "script-src 'self'" in page.headers["content-security-policy"]
        for method, route in (("GET", "/api/sessions"), ("GET", "/api/events"), ("POST", "/api/send")):
            for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}):
                response = await client.request(method, route, headers=headers, json={"text": "/help"})
                assert response.status_code == 401, (route, headers)
                assert response.headers["www-authenticate"] == "Bearer"
        ok = await client.get("/api/sessions", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200 and ok.json() == []


async def test_commands_run_and_replies_stream_over_sse(running: tuple[Pwa, int]) -> None:
    _, port = running
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with (
        httpx.AsyncClient(base_url=f"http://{LOOPBACK}:{port}", timeout=10) as client,
        client.stream("GET", "/api/events", headers=auth) as events,
    ):
        lines = events.aiter_lines()
        assert await anext(lines) == ": connected"
        sent = await client.post("/api/send", headers=auth, json={"text": "/help"})
        assert sent.status_code == 202
        payload = None
        while payload is None:
            line = await asyncio.wait_for(anext(lines), 5)
            if line.startswith("data: "):
                payload = json.loads(line[6:])
        assert payload["kind"] == "message" and payload["session"] is None
        assert payload["markdown"] == help_text()
        missing = await client.post("/api/send", headers=auth, json={"text": "hi", "session": "nope"})
        assert missing.status_code == 404
        card = await client.post("/api/card", headers=auth, json={"inputs": {"relay": "nothing"}})
        assert card.json() == {"outcome": "ignored"}


async def test_hub_scrubs_and_drops_the_oldest_when_a_client_lags() -> None:
    hub = PwaHub(queue_size=2)
    async with hub.subscribe() as queue:
        await hub.handle(
            TextDelta(harness=Harness.OPENCODE, session_key="s1", text="Bearer fake-value-0000-abcdefgh")
        )
        hub.broadcast({"kind": "message", "n": 2})
        hub.broadcast({"kind": "message", "n": 3})
        first = queue.get_nowait()
        assert first == {"kind": "message", "n": 2}
        assert queue.get_nowait() == {"kind": "message", "n": 3}
    assert hub.clients == 0
    async with hub.subscribe() as queue:
        await hub.handle(
            TextDelta(harness=Harness.OPENCODE, session_key="s1", text="Bearer fake-value-0000-abcdefgh")
        )
        event = queue.get_nowait()["event"]
        assert isinstance(event, dict) and event["text"] == REDACTED


def test_the_page_never_uses_innerhtml() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "outerHTML" not in script and "eval(" not in script
    assert "token=" not in script


def test_expose_prints_tailscale_serve_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("relay pwa expose must not run anything")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = CliRunner().invoke(cli, ["pwa", "expose"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"tailscale serve --bg --https=443 http://{LOOPBACK}:8787"
    assert "funnel" not in result.stdout.lower()


def test_the_page_renders_monospace_text_and_sends_the_cards_own_data() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'fontType === "Monospace"' in script and "...(action.data" in script
    assert "approvalCard" not in script and "questionCard" not in script
    assert ".mono" in (STATIC / "app.css").read_text(encoding="utf-8")


H = Harness.OPENCODE
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def _request(registry: Registry, kind: str, request_id: str) -> SessionRecord:
    session = await registry.create_session(
        SessionRecord(session_key="opencode:p1", harness=H, harness_id="p1", workspace="/w/p")
    )
    await registry.add_pending(
        PendingRequest(
            request_id=request_id,
            kind="approval" if kind == "approval" else "question",
            session_key=session.session_key,
            payload={"source": "adapter"},
            deadline=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    return session


async def test_the_hub_sends_approvals_and_questions_as_cards_with_a_nonce(registry: Registry) -> None:
    session = await _request(registry, "approval", "r1")
    hub = PwaHub(registry=registry)
    async with hub.subscribe() as queue:
        await hub.handle(
            ApprovalRequested(
                harness=H, session_key=session.session_key, request_id="r1", tool="bash", detail="rm -rf x"
            )
        )
        await hub.handle(
            QuestionAsked(
                harness=H, session_key=session.session_key, request_id="q1", question="Which?", options=("a",)
            )
        )
        payloads = [queue.get_nowait() for _ in range(4)]
    assert [p["kind"] for p in payloads] == ["event", "message", "event", "message"]
    approval, question = payloads[1], payloads[3]
    assert approval["markdown"] == "🔐 Approval needed: bash · auto-deny in 10m"
    assert question["markdown"] == "❓ Question: Which?"
    for message in (approval, question):
        card = message["card"]
        assert isinstance(card, dict)
        for data in _submits(card):
            assert await registry.kv_get(f"{card_state.NONCE_PREFIX}{data['nonce']}") is not None
    assert await card_state.load_offer(registry, "q1") == ["a"]


async def test_a_hub_without_a_registry_sends_only_the_raw_event(registry: Registry) -> None:
    session = await _request(registry, "approval", "r1")
    hub = PwaHub()
    async with hub.subscribe() as queue:
        await hub.handle(
            ApprovalRequested(harness=H, session_key=session.session_key, request_id="r1", tool="b")
        )
        assert queue.get_nowait()["kind"] == "event" and queue.empty()


async def test_card_submits_need_a_live_nonce_and_relay_errors_are_shown(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, router = service(registry)
    router.add_adapter(EmittingAdapter(count=0))
    hub = PwaHub(registry=registry)
    app = build_app(hub, svc, Handlers(svc, hub), TOKEN)
    session = await _request(registry, "approval", "r1")
    async with (
        hub.subscribe() as queue,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pwa") as client,
    ):
        await hub.handle(
            ApprovalRequested(harness=H, session_key=session.session_key, request_id="r1", tool="b")
        )
        queue.get_nowait()  # the raw event
        data = _first_submit(queue.get_nowait())

        async def post(inputs: dict[str, JsonValue]) -> JsonValue:
            reply = await client.post("/api/card", headers=AUTH, json={"inputs": inputs})
            assert reply.status_code == 200
            outcome: JsonValue = reply.json()["outcome"]
            return outcome

        assert await post({k: v for k, v in data.items() if k != "nonce"}) == "expired"
        assert queue.get_nowait()["markdown"] == card_state.EXPIRED

        real = svc.answer_approval
        failures = [DeliveryError("OpenCode could not be reached.")]

        async def flaky(
            request_id: str, decision: Decision, by: ResolvedBy = "phone", reason: str | None = None
        ) -> bool:
            if failures:
                raise failures.pop()
            return await real(request_id, decision, by, reason)

        monkeypatch.setattr(svc, "answer_approval", flaky)
        assert await post(data) == "failed"
        shown = queue.get_nowait()
        assert shown["markdown"] == "⚠️ OpenCode could not be reached."
        assert shown["session"] == session.session_key
        assert await post(data) == "done"
        assert await post(data) == "expired"
    await router.stop()


def _submits(card: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    """The data of every submit button, including those inside ShowCard sub-cards."""
    found: list[dict[str, JsonValue]] = []
    actions = card.get("actions")
    assert isinstance(actions, list)
    for action in actions:
        assert isinstance(action, dict)
        if action.get("type") == "Action.ShowCard":
            inner = action["card"]
            assert isinstance(inner, dict)
            found.extend(_submits(inner))
        else:
            assert isinstance(action["data"], dict)
            found.append(action["data"])
    return found


def _first_submit(message: dict[str, JsonValue]) -> dict[str, JsonValue]:
    card = message["card"]
    assert isinstance(card, dict)
    return _submits(card)[0]
