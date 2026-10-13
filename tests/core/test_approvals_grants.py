"""Workspace grants ("allow this tool in this workspace for a while") and deny reasons, below the card:
scope, expiry, adapter-held auto-allow, housekeeping, and what reaches the adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from agent_relay.core.housekeeping import housekeep
from agent_relay.core.registry import Registry
from agent_relay.core.service import DeliveryError, RelayError
from agent_relay.core.service.approvals import (
    REASON_MAX_CHARS,
    WORKSPACE_GRANT_PREFIX,
    clean_reason,
    record_workspace_grant,
    workspace_grant_active,
    workspace_grant_key,
)
from agent_relay.daemon import Core
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import ApprovalRequested, ApprovalResolved, Event, Harness
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus
from tests.fakes.adapters import EmittingAdapter

TOKEN = "ghp_" + "f" * 36
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _session(key: str, workspace: str | None) -> SessionRecord:
    return SessionRecord(session_key=key, harness=Harness.OPENCODE, harness_id=key, workspace=workspace)


@pytest.fixture
async def setup(registry: Registry) -> AsyncIterator[tuple[Core, EmittingAdapter]]:
    core = Core(RelayConfig(), registry)
    adapter = EmittingAdapter(count=0)
    core.router.add_adapter(adapter)
    await registry.create_session(_session("opencode:s1", "/w/proj"))
    yield core, adapter
    await core.router.stop()


async def _ask(core: Core, request_id: str, tool: str = "bash", session: str = "opencode:s1") -> None:
    await core.router.publish(
        ApprovalRequested(
            harness=Harness.OPENCODE, session_key=session, request_id=request_id, tool=tool, detail="ls"
        )
    )
    await core.router.join()


# scope and expiry


async def test_a_grant_covers_the_workspace_and_the_folders_under_it(registry: Registry) -> None:
    assert await record_workspace_grant(registry, "/w/proj", "bash", 3600, NOW)
    for covered in ("/w/proj", "/w/proj/src", "/w/proj/src/deep/er"):
        assert await workspace_grant_active(registry, covered, "bash", NOW), covered


@pytest.mark.parametrize("elsewhere", ["/w", "/w/other", "/w/project", "/w/pro", "/", "/x/w/proj"])
async def test_a_grant_never_applies_to_a_different_workspace(registry: Registry, elsewhere: str) -> None:
    await record_workspace_grant(registry, "/w/proj", "bash", 3600, NOW)
    assert not await workspace_grant_active(registry, elsewhere, "bash", NOW)


@pytest.mark.parametrize("other_tool", ["Bash", "bash ", "bas", "bash:x", "edit"])
async def test_tool_matching_is_exact(registry: Registry, other_tool: str) -> None:
    await record_workspace_grant(registry, "/w/proj", "bash", 3600, NOW)
    assert not await workspace_grant_active(registry, "/w/proj", other_tool, NOW)


async def test_the_key_cannot_be_confused_by_colons_in_workspace_or_tool(registry: Registry) -> None:
    await record_workspace_grant(registry, "/a", "b:c", 3600, NOW)
    assert not await workspace_grant_active(registry, "/a:b", "c", NOW)
    assert workspace_grant_key("/a", "b:c") != workspace_grant_key("/a:b", "c")


async def test_a_grant_lasts_until_its_expiry_and_no_longer(registry: Registry) -> None:
    await record_workspace_grant(registry, "/w/proj", "bash", 14_400, NOW)
    assert await workspace_grant_active(registry, "/w/proj", "bash", NOW + timedelta(hours=3, minutes=59))
    assert not await workspace_grant_active(registry, "/w/proj", "bash", NOW + timedelta(hours=4))


async def test_nothing_is_granted_without_a_workspace_or_a_tool(registry: Registry) -> None:
    assert not await record_workspace_grant(registry, None, "bash", 3600, NOW)
    assert not await record_workspace_grant(registry, "/w/proj", None, 3600, NOW)
    assert not await record_workspace_grant(registry, "/w/proj", "", 3600, NOW)
    assert await registry.kv_prefix(WORKSPACE_GRANT_PREFIX) == {}
    assert not await workspace_grant_active(registry, None, "bash", NOW)


async def test_a_garbled_grant_is_not_honoured(registry: Registry) -> None:
    await registry.kv_set(workspace_grant_key("/w/proj", "bash"), "forever")
    assert not await workspace_grant_active(registry, "/w/proj", "bash", NOW)


# housekeeping and the session lifecycle


async def test_housekeeping_prunes_expired_grants_only(registry: Registry) -> None:
    live = datetime.now(UTC)
    await record_workspace_grant(registry, "/w/old", "bash", 60, live - timedelta(hours=1))
    await record_workspace_grant(registry, "/w/new", "bash", 3600, live)
    await registry.kv_set(workspace_grant_key("/w/odd", "bash"), "forever")
    assert await housekeep(registry) == 2
    assert await registry.kv_get(workspace_grant_key("/w/old", "bash")) is None
    assert await registry.kv_get(workspace_grant_key("/w/odd", "bash")) is None
    assert await registry.kv_get(workspace_grant_key("/w/new", "bash")) is not None


async def test_a_grant_survives_the_end_of_the_session_that_made_it(
    setup: tuple[Core, EmittingAdapter],
) -> None:
    core, _ = setup
    await _ask(core, "p1")
    assert await core.service.answer_approval("p1", "allow_workspace")
    await core.service.lifecycle.transition("opencode:s1", SessionStatus.ENDED, "test")
    assert await core.broker.allowed_for_workspace("/w/proj", "bash")
    assert not await core.broker.allowed_for_session("opencode:s1", "bash")


# the service


async def test_allow_workspace_tells_the_adapter_and_records_the_grant(
    setup: tuple[Core, EmittingAdapter],
) -> None:
    core, adapter = setup
    await _ask(core, "p1")
    assert await core.service.answer_approval("p1", "allow_workspace")
    assert adapter.approvals == [("p1", "allow_workspace")]
    stored = await core.registry.kv_get(workspace_grant_key("/w/proj", "bash"))
    assert stored is not None
    expires = datetime.fromisoformat(stored)
    lasting = (expires - datetime.now(UTC)).total_seconds()
    assert core.config.approvals.workspace_grant_s - 5 < lasting <= core.config.approvals.workspace_grant_s


async def test_the_grant_length_comes_from_the_config(registry: Registry) -> None:
    config = RelayConfig()
    config = config.model_copy(
        update={"approvals": config.approvals.model_copy(update={"workspace_grant_s": 600})}
    )
    core = Core(config, registry)
    core.router.add_adapter(EmittingAdapter(count=0))
    await registry.create_session(_session("opencode:s1", "/w/proj"))
    await _ask(core, "p1")
    await core.service.answer_approval("p1", "allow_workspace")
    stored = await registry.kv_get(workspace_grant_key("/w/proj", "bash"))
    assert stored is not None
    assert 590 < (datetime.fromisoformat(stored) - datetime.now(UTC)).total_seconds() <= 600
    await core.router.stop()


async def test_the_resolved_event_says_allow_for_listeners_that_know_no_grants(
    setup: tuple[Core, EmittingAdapter],
) -> None:
    core, _ = setup
    seen: list[ApprovalResolved] = []

    class Sink:
        async def handle(self, event: object) -> None:
            if isinstance(event, ApprovalResolved):
                seen.append(event)

    core.router.subscribe(Sink())
    await _ask(core, "p1")
    await core.service.answer_approval("p1", "allow_workspace")
    await core.router.join()
    assert [(e.request_id, e.decision) for e in seen] == [("p1", "allow")]


async def test_a_request_without_a_workspace_is_allowed_once_and_grants_nothing(registry: Registry) -> None:
    core = Core(RelayConfig(), registry)
    adapter = EmittingAdapter(count=0)
    core.router.add_adapter(adapter)
    await registry.create_session(_session("opencode:s2", None))
    await _ask(core, "p9", session="opencode:s2")
    assert await core.service.answer_approval("p9", "allow_workspace")
    assert adapter.approvals == [("p9", "allow_workspace")]
    assert await registry.kv_prefix(WORKSPACE_GRANT_PREFIX) == {}
    await core.router.stop()


async def test_a_workspace_allow_that_never_reached_the_app_grants_nothing(registry: Registry) -> None:
    core = Core(RelayConfig(), registry)  # no adapter connected: delivery fails
    await registry.create_session(_session("opencode:s1", "/w/proj"))
    await _ask(core, "p1")
    with pytest.raises(DeliveryError):
        await core.service.answer_approval("p1", "allow_workspace")
    assert await registry.kv_prefix(WORKSPACE_GRANT_PREFIX) == {}
    pending = await registry.get_pending("p1")
    assert pending is not None and pending.status == "open"
    await core.router.stop()


async def test_an_unknown_decision_is_refused(setup: tuple[Core, EmittingAdapter]) -> None:
    core, adapter = setup
    await _ask(core, "p1")
    with pytest.raises(RelayError, match="not a decision"):
        await core.service.answer_approval("p1", "always")  # type: ignore[arg-type]
    assert adapter.approvals == []
    pending = await core.registry.get_pending("p1")
    assert pending is not None and pending.status == "open"


# adapter-held auto-allow


async def test_the_next_matching_adapter_request_is_allowed_before_it_reaches_the_phone(
    setup: tuple[Core, EmittingAdapter],
) -> None:
    core, adapter = setup
    await _ask(core, "p1")
    await core.service.answer_approval("p1", "allow_workspace")
    await _ask(core, "p2")
    assert adapter.approvals == [("p1", "allow_workspace"), ("p2", "allow")]
    pending = await core.registry.get_pending("p2")
    assert pending is not None and pending.status != "open"
    assert await core.registry.open_pending() == []


async def test_a_grant_does_not_answer_another_tool_or_another_workspace(
    setup: tuple[Core, EmittingAdapter],
) -> None:
    core, adapter = setup
    await core.registry.create_session(_session("opencode:s2", "/w/other"))
    await _ask(core, "p1")
    await core.service.answer_approval("p1", "allow_workspace")
    await _ask(core, "p2", tool="edit")
    await _ask(core, "p3", session="opencode:s2")
    assert adapter.approvals == [("p1", "allow_workspace")]
    assert {p.request_id for p in await core.registry.open_pending()} == {"p2", "p3"}


async def test_an_expired_grant_asks_again(setup: tuple[Core, EmittingAdapter]) -> None:
    core, adapter = setup
    await record_workspace_grant(core.registry, "/w/proj", "bash", 60, datetime.now(UTC) - timedelta(hours=2))
    await _ask(core, "p1")
    assert adapter.approvals == []
    assert [p.request_id for p in await core.registry.open_pending()] == ["p1"]


async def test_a_grant_that_cannot_be_delivered_leaves_the_request_for_the_phone(registry: Registry) -> None:
    core = Core(RelayConfig(), registry)  # no adapter connected
    await registry.create_session(_session("opencode:s1", "/w/proj"))
    await record_workspace_grant(registry, "/w/proj", "bash", 3600)
    seen: list[Event] = []

    class Sink:
        async def handle(self, event: Event) -> None:
            seen.append(event)

    core.router.subscribe(Sink())
    await _ask(core, "p1")
    assert [p.request_id for p in await registry.open_pending()] == ["p1"]
    assert [e.type for e in seen] == ["approval.requested"]
    await core.router.stop()


async def test_a_granted_request_never_produces_a_card_event(setup: tuple[Core, EmittingAdapter]) -> None:
    core, adapter = setup
    seen: list[Event] = []

    class Sink:
        async def handle(self, event: Event) -> None:
            seen.append(event)

    core.router.subscribe(Sink())
    await record_workspace_grant(core.registry, "/w/proj", "bash", 3600)
    await _ask(core, "p1")
    assert adapter.approvals == [("p1", "allow")]
    assert not [e for e in seen if isinstance(e, ApprovalRequested)]
    assert [(e.request_id, e.decision, e.by) for e in seen if isinstance(e, ApprovalResolved)] == [
        ("p1", "allow", "grant")
    ]
    pending = await core.registry.get_pending("p1")
    assert pending is not None and pending.status == "resolved"
    assert pending.answer is not None and pending.answer["by"] == "grant"
    assert pending.webex_message_id is None


async def test_an_ungranted_request_still_reaches_the_sinks(setup: tuple[Core, EmittingAdapter]) -> None:
    core, _ = setup
    seen: list[Event] = []

    class Sink:
        async def handle(self, event: Event) -> None:
            seen.append(event)

    core.router.subscribe(Sink())
    await _ask(core, "p1")
    assert [e.type for e in seen] == ["approval.requested"]


# deny reasons


async def test_a_deny_reason_reaches_the_adapter_and_is_stored(setup: tuple[Core, EmittingAdapter]) -> None:
    core, adapter = setup
    await _ask(core, "p1")
    assert await core.service.answer_approval("p1", "deny", reason="  use the\nstaging db  ")
    assert adapter.approvals == [("p1", "deny")]
    assert adapter.reasons == [("p1", "use the staging db")]
    pending = await core.registry.get_pending("p1")
    assert pending is not None and pending.answer is not None
    assert pending.answer["reason"] == "use the staging db"


@pytest.mark.parametrize("decision", ["allow", "allow_session", "allow_workspace"])
async def test_a_reason_only_goes_with_a_deny(setup: tuple[Core, EmittingAdapter], decision: str) -> None:
    core, adapter = setup
    await _ask(core, "p1")
    assert await core.service.answer_approval("p1", decision, reason="nope")  # type: ignore[arg-type]
    assert adapter.reasons == []


async def test_a_blank_reason_is_no_reason(setup: tuple[Core, EmittingAdapter]) -> None:
    core, adapter = setup
    await _ask(core, "p1")
    await core.service.answer_approval("p1", "deny", reason=" \n\t ")
    assert adapter.reasons == []


def test_reasons_are_scrubbed_collapsed_and_capped() -> None:
    cleaned = clean_reason(f"use {TOKEN} never\n\nagain")
    assert cleaned is not None and TOKEN not in cleaned and cleaned.endswith("never again")
    long = clean_reason("x" * (REASON_MAX_CHARS + 300))
    assert long == "x" * REASON_MAX_CHARS
    assert clean_reason(None) is None and clean_reason("") is None


async def test_deadline_and_other_denials_carry_no_reason(setup: tuple[Core, EmittingAdapter]) -> None:
    core, adapter = setup
    await core.registry.add_pending(
        PendingRequest(
            request_id="late",
            kind="approval",
            session_key="opencode:s1",
            payload={"source": "adapter", "native_id": "late", "tool": "bash"},
            deadline=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    assert await core.broker.sweep() == 1
    assert adapter.approvals == [("late", "deny")] and adapter.reasons == []
