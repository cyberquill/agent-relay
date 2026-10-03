"""AC-38: an app update re-runs its adapter's self-test; a failure degrades the adapter and is posted."""

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


class Posts:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, text: str) -> None:
        self.texts.append(text)


@pytest.fixture
def plist(tmp_path: Path) -> Path:
    return write_plist(tmp_path / "VS Code.app" / "Contents" / "Info.plist", "1.105.0")


def watcher(registry: Registry, plist: Path, test: Selftest, posts: Posts) -> UpdateWatcher:
    return UpdateWatcher(
        registry,
        {Harness.VSCODE_COPILOT: test},
        posts,
        apps=(WatchedApp("VS Code", plist, (Harness.VSCODE_COPILOT,)),),
    )


def test_app_version_reads_the_bundle_version(tmp_path: Path, plist: Path) -> None:
    assert updates.app_version(plist) == "1.105.0"
    assert updates.app_version(write_plist(tmp_path / "x" / "Info.plist", None)) is None
    assert updates.app_version(tmp_path / "missing.plist") is None
    (tmp_path / "bad.plist").write_text("not a plist", encoding="utf-8")
    assert updates.app_version(tmp_path / "bad.plist") is None


async def test_first_sighting_only_records_the_version(registry: Registry, plist: Path) -> None:
    test, posts = Script(), Posts()
    assert await watcher(registry, plist, test, posts).check() == []
    assert test.calls == 0
    assert json.loads(paths.versions_file().read_text(encoding="utf-8")) == {"VS Code": "1.105.0"}


async def test_failed_selftest_after_update_pauses_and_posts(registry: Registry, plist: Path) -> None:
    test, posts = Script(("fail", "VS Code speaks AHP 1.0.0")), Posts()
    w = watcher(registry, plist, test, posts)
    await w.check()
    write_plist(plist, "1.106.0")
    notices = await w.check()
    assert test.calls == 1
    assert notices == posts.texts
    assert posts.texts[0].startswith(
        "⚠️ VS Code updated 1.105.0 → 1.106.0: the VS Code agent host self-test failed"
    )
    assert "It is paused until it passes" in posts.texts[0]
    reason = await updates.degraded_reason(registry, Harness.VSCODE_COPILOT)
    assert reason is not None and "AHP 1.0.0" in reason
    assert json.loads(paths.versions_file().read_text(encoding="utf-8")) == {"VS Code": "1.106.0"}


async def test_passing_selftest_after_update_is_posted(registry: Registry, plist: Path) -> None:
    test, posts = Script(("pass", "AHP 0.9.0")), Posts()
    w = watcher(registry, plist, test, posts)
    await w.check()
    write_plist(plist, "1.106.0")
    await w.check()
    assert posts.texts == [
        "✅ VS Code updated 1.105.0 → 1.106.0: the VS Code agent host self-test passed (AHP 0.9.0)."
    ]
    assert await updates.degraded_reason(registry, Harness.VSCODE_COPILOT) is None


async def test_unknown_outcome_retries_on_the_next_check(registry: Registry, plist: Path) -> None:
    test, posts = Script(("unknown", "VS Code is closed"), ("pass", "AHP 0.9.0")), Posts()
    w = watcher(registry, plist, test, posts)
    await w.check()
    write_plist(plist, "1.106.0")
    assert await w.check() == []
    assert json.loads(paths.versions_file().read_text(encoding="utf-8")) == {"VS Code": "1.105.0"}
    await w.check()
    assert test.calls == 2
    assert posts.texts and posts.texts[0].startswith("✅ VS Code updated 1.105.0 → 1.106.0")


async def test_degraded_adapter_recovers_when_a_retry_passes(registry: Registry, plist: Path) -> None:
    test, posts = Script(("fail", "bad"), ("unknown", "closed"), ("pass", "AHP 0.9.0")), Posts()
    w = watcher(registry, plist, test, posts)
    await w.check()
    write_plist(plist, "1.106.0")
    await w.check()
    assert await w.check() == []
    assert await updates.degraded_reason(registry, Harness.VSCODE_COPILOT) is not None
    await w.check()
    assert posts.texts[-1] == "✅ VS Code agent host passed its self-test again (AHP 0.9.0); it is back on."
    assert await updates.degraded_reason(registry, Harness.VSCODE_COPILOT) is None


async def test_crashing_selftest_counts_as_a_failure(registry: Registry, plist: Path) -> None:
    async def boom() -> SelftestResult:
        raise OSError("nope")

    posts = Posts()
    w = watcher(registry, plist, boom, posts)
    await w.check()
    write_plist(plist, "2.0.0")
    await w.check()
    assert "self-test crashed: OSError" in posts.texts[0]


async def test_a_failing_post_does_not_stop_the_check(registry: Registry, plist: Path) -> None:
    async def broken(_: str) -> None:
        raise RuntimeError("webex down")

    w = UpdateWatcher(
        registry,
        {Harness.VSCODE_COPILOT: Script(("fail", "x"))},
        broken,
        apps=(WatchedApp("VS Code", plist, (Harness.VSCODE_COPILOT,)),),
    )
    await w.check()
    write_plist(plist, "2.0.0")
    assert len(await w.check()) == 1


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
    assert await service.prompt(record.session_key, "more") == "queued as steering for the next tool call"
    assert adapter.prompts == []
    await router.stop()
