"""AC-38: an app update re-runs its adapter's self-test; a failure degrades the adapter. No phone post."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_relay import paths
from agent_relay.core import updates
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayError, RelayService
from agent_relay.core.signals import Signals
from agent_relay.core.updates import Selftest, SelftestResult, UpdateWatcher, WatchedApp
from agent_relay.models.config import RelayConfig, Workspace, WorkspacesConfig
from agent_relay.models.events import Harness
from agent_relay.settings import save_workspaces
from tests.fakes.adapters import EmittingAdapter
from tests.fakes.apps import write_plist


class Script:
    """A self-test returning queued results, recording each call."""

    def __init__(self, *results: SelftestResult) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self) -> SelftestResult:
        self.calls += 1
        return self.results.pop(0)


@pytest.fixture
def plist(tmp_path: Path) -> Path:
    return write_plist(tmp_path / "VS Code.app" / "Contents" / "Info.plist", "1.105.0")


def watcher(registry: Registry, plist: Path, test: Selftest) -> UpdateWatcher:
    return UpdateWatcher(
        registry,
        {Harness.VSCODE_COPILOT: test},
        apps=(WatchedApp("VS Code", plist, (Harness.VSCODE_COPILOT,)),),
    )


def test_app_version_reads_the_bundle_version(tmp_path: Path, plist: Path) -> None:
    assert updates.app_version(plist) == "1.105.0"
    assert updates.app_version(write_plist(tmp_path / "x" / "Info.plist", None)) is None
    assert updates.app_version(tmp_path / "missing.plist") is None
    (tmp_path / "bad.plist").write_text("not a plist", encoding="utf-8")
    assert updates.app_version(tmp_path / "bad.plist") is None


async def test_first_sighting_only_records_the_version(registry: Registry, plist: Path) -> None:
    test = Script()
    assert await watcher(registry, plist, test).check() == []
    assert test.calls == 0
    assert json.loads(paths.versions_file().read_text(encoding="utf-8")) == {"VS Code": "1.105.0"}


async def test_failed_selftest_after_update_pauses_and_records_the_notice(
    registry: Registry, plist: Path
) -> None:
    test = Script(("fail", "VS Code speaks AHP 1.0.0"))
    w = watcher(registry, plist, test)
    await w.check()
    write_plist(plist, "1.106.0")
    notices = await w.check()
    assert test.calls == 1
    assert notices[0].startswith(
        "⚠️ VS Code updated 1.105.0 → 1.106.0: the VS Code agent host self-test failed"
    )
    assert "It is paused until it passes" in notices[0]
    recorded = await updates.latest_notices(registry)
    assert recorded["vscode_copilot"][1] == notices[0], "the doctor reads it from the registry"
    reason = await updates.degraded_reason(registry, Harness.VSCODE_COPILOT)
    assert reason is not None and "AHP 1.0.0" in reason
    assert json.loads(paths.versions_file().read_text(encoding="utf-8")) == {"VS Code": "1.106.0"}


async def test_passing_selftest_after_update_is_recorded(registry: Registry, plist: Path) -> None:
    test = Script(("pass", "AHP 0.9.0"))
    w = watcher(registry, plist, test)
    await w.check()
    write_plist(plist, "1.106.0")
    notices = await w.check()
    assert notices == [
        "✅ VS Code updated 1.105.0 → 1.106.0: the VS Code agent host self-test passed (AHP 0.9.0)."
    ]
    assert (await updates.latest_notices(registry))["vscode_copilot"][1] == notices[0]
    assert await updates.degraded_reason(registry, Harness.VSCODE_COPILOT) is None


async def test_unknown_outcome_retries_on_the_next_check(registry: Registry, plist: Path) -> None:
    test = Script(("unknown", "VS Code is closed"), ("pass", "AHP 0.9.0"))
    w = watcher(registry, plist, test)
    await w.check()
    write_plist(plist, "1.106.0")
    assert await w.check() == []
    assert json.loads(paths.versions_file().read_text(encoding="utf-8")) == {"VS Code": "1.105.0"}
    notices = await w.check()
    assert test.calls == 2
    assert notices and notices[0].startswith("✅ VS Code updated 1.105.0 → 1.106.0")


async def test_degraded_adapter_recovers_when_a_retry_passes(registry: Registry, plist: Path) -> None:
    test = Script(("fail", "bad"), ("unknown", "closed"), ("pass", "AHP 0.9.0"))
    w = watcher(registry, plist, test)
    await w.check()
    write_plist(plist, "1.106.0")
    await w.check()
    assert await w.check() == []
    assert await updates.degraded_reason(registry, Harness.VSCODE_COPILOT) is not None
    notices = await w.check()
    assert notices[-1] == "✅ VS Code agent host passed its self-test again (AHP 0.9.0); it is back on."
    assert await updates.degraded_reason(registry, Harness.VSCODE_COPILOT) is None


async def test_crashing_selftest_counts_as_a_failure(registry: Registry, plist: Path) -> None:
    async def boom() -> SelftestResult:
        raise OSError("nope")

    w = watcher(registry, plist, boom)
    await w.check()
    write_plist(plist, "2.0.0")
    notices = await w.check()
    assert "self-test crashed: OSError" in notices[0]


def test_the_watcher_has_no_way_to_post_to_the_phone() -> None:
    fields = set(UpdateWatcher.__dataclass_fields__)
    assert "notify" not in fields and fields >= {"registry", "selftests", "apps"}


async def test_service_refuses_a_degraded_adapter_with_the_reason(registry: Registry, tmp_path: Path) -> None:
    save_workspaces(WorkspacesConfig(workspaces=[Workspace(name="proj", path=str(tmp_path))]))
    router = Router()
    adapter = EmittingAdapter(count=0)
    router.add_adapter(adapter)
    service = RelayService(registry, router, Signals(), RelayConfig())
    await updates.set_degraded(registry, Harness.OPENCODE, "OpenCode updated 1 → 2: the self-test failed (x)")
    with pytest.raises(RelayError, match="is disabled: OpenCode updated 1 → 2"):
        await service.start("opencode", "proj", "hello")
    with pytest.raises(RelayError, match="is disabled"):
        await service.models("opencode")
    apps = await service.apps()
    assert not apps[0].healthy and apps[0].detail.startswith("disabled: OpenCode updated")
    await updates.set_degraded(registry, Harness.OPENCODE, None)
    record = await service.start("opencode", "proj", "hello")
    await updates.set_degraded(registry, Harness.OPENCODE, "again")
    await service.set_queue_mode(record.session_key, "steer")
    assert await service.prompt(record.session_key, "more") == "queued as steering for the next tool call"
    assert adapter.prompts == []
    await router.stop()
