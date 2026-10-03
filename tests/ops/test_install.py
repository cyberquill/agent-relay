"""`relay install|uninstall` inside a temporary RELAY_HOME: dry runs, exact reversal, idempotence (AC-39)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_relay import paths
from agent_relay.fileops import LinkBlockedError
from agent_relay.models.config import AdaptersConfig, RelayConfig
from agent_relay.ops import bundles, install
from agent_relay.ops.install import InstallPlan, apply_install, apply_uninstall, plan_install
from tests.sync.helpers import FAKE_SECRETS, install_fixtures

PYTHON = "/opt/relay/bin/python"
ORIGINAL_SKILL = b"---\nname: stepping-away\n---\n\nPost to Webex with sa.py.\n"
GIT_AI_CURSOR = {"version": 1, "hooks": {"preToolUse": [{"command": "git-ai hook pre"}]}}
GIT_AI_COPILOT = {"version": 1, "hooks": {"PreToolUse": [{"type": "command", "bash": "git-ai pre"}]}}

STAMP = re.compile(rb"\d{8}T\d{6}Z")

type Entry = tuple[str, bytes | str, int]


@dataclass
class Commands:
    seen: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.seen.append(list(argv))
        return 0, ""

    def heads(self) -> list[str]:
        return [" ".join(a[:2]) if a[0] == "launchctl" else Path(a[0]).name + " " + a[1] for a in self.seen]


@dataclass
class Seed:
    patch_dir: Path
    extension_dir: Path
    code: Path


def _write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


@pytest.fixture
def seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Seed:
    """Today's Mac in miniature: git-ai hooks, MCP files with secrets, duplicate skills, the apps."""
    install_fixtures()
    _write(paths.cursor_dir() / "hooks.json", json.dumps(GIT_AI_CURSOR).encode())
    _write(paths.copilot_dir() / "hooks" / "git-ai.json", json.dumps(GIT_AI_COPILOT).encode())
    for root in (paths.agents_skills_dir(), paths.copilot_dir() / "skills", paths.claude_dir() / "skills"):
        _write(root / "demo" / "SKILL.md", b"---\nname: demo\n---\nsame\n")
    _write(paths.cursor_dir() / "skills" / "own" / "SKILL.md", b"only here\n")
    _write(paths.agents_skills_dir() / "stepping-away" / "SKILL.md", ORIGINAL_SKILL)
    _write(paths.cursor_agent_cli(), b"#!/bin/sh\n")
    patch_dir = tmp_path / "patch"
    _write(patch_dir / "SKILL.md", b"---\nname: stepping-away\n---\n\nAgent Relay posts.\n")
    (patch_dir / "base.sha256").write_text(f"{hashlib.sha256(ORIGINAL_SKILL).hexdigest()}\n")
    extension_dir = tmp_path / "relay-bridge"
    package = {"name": "relay-bridge", "publisher": "agent-relay", "version": "0.1.0"}
    package["main"] = "./out/src/extension.js"
    _write(extension_dir / "package.json", json.dumps(package).encode())
    _write(extension_dir / "out" / "src" / "extension.js", b"exports.activate = () => {};\n")
    code = tmp_path / "apps" / "code"
    _write(code, b"#!/bin/sh\n")
    monkeypatch.setattr(paths, "VSCODE_CLI", code)
    return Seed(patch_dir, extension_dir, code)


def planned(seed: Seed, config: RelayConfig | None = None) -> InstallPlan:
    return plan_install(
        config or RelayConfig(), PYTHON, extension_dir=seed.extension_dir, patch_dir=seed.patch_dir
    )


def snapshot(skip_relay_state: bool = False) -> dict[str, Entry]:
    """Every path under RELAY_HOME: symlink targets, file bytes and modes."""
    home = paths.home()
    skipped = (paths.app_support(), paths.logs_dir()) if skip_relay_state else ()
    found: dict[str, Entry] = {}
    for item in sorted(home.rglob("*")):
        if any(item == s or item.is_relative_to(s) for s in skipped):
            continue
        key = str(item.relative_to(home))
        if item.is_symlink():
            found[key] = ("link", str(item.readlink()), 0)
        elif item.is_file():
            found[key] = ("file", item.read_bytes(), item.stat().st_mode & 0o777)
        else:
            found[key] = ("dir", "", 0)
    return found


def without_backup_stamps(tree: dict[str, Entry]) -> dict[str, Entry]:
    """Backup folders are named by time; compare what they hold, not when they were taken."""
    roots = (paths.backups_dir(), paths.skill_dup_backup_dir())
    home = paths.home()
    normalised: dict[str, Entry] = {}
    for key, entry in tree.items():
        full = home / key
        root = next((r for r in roots if full.is_relative_to(r) and full != r), None)
        if root is not None:
            rest = full.relative_to(root).parts[1:]
            key = str(root.relative_to(home).joinpath("<stamp>", *rest))
        kind, content, mode = entry
        if isinstance(content, bytes):
            content = STAMP.sub(b"<stamp>", content)
        normalised[key] = (kind, content, mode)
    return normalised


def leaked(text: str) -> list[str]:
    return [secret for secret in FAKE_SECRETS if secret in text]


def test_dry_run_plans_the_whole_touch_table_and_writes_nothing(seed: Seed) -> None:
    before = snapshot()
    plan = planned(seed)
    report = "\n".join(plan.report())
    diff = plan.diff()
    assert snapshot() == before
    for touched in (
        paths.copilot_dir() / "hooks" / "agent-relay.json",
        paths.cursor_dir() / "hooks.json",
        paths.opencode_config_dir() / "plugins" / "agent-relay.ts",
        paths.mcp_yaml(),
        paths.agents_skills_dir() / "stepping-away" / "SKILL.md",
        paths.launch_agents_dir() / "io.github.cyberquill.agent-relay.relayd.plist",
        install.vsix_path(),
    ):
        assert str(touched) in report, touched
    assert f"link {paths.agents_skills_dir() / bundles.SKILL_NAME}" in report
    assert "move " in report and "keep " in "\n".join(plan.notes)
    assert "launchctl bootstrap io.github.cyberquill.agent-relay.cursor-update" in report
    assert leaked(report + diff) == []
    assert "Binary" not in diff and "PK\x03\x04" not in diff


def test_install_uninstall_install_leaves_no_stray_files(seed: Seed) -> None:
    """AC-39."""
    before = snapshot()
    outside_relay_state = snapshot(skip_relay_state=True)
    first = apply_install(planned(seed), Commands())
    assert first.ok, first.problems
    installed = snapshot()
    assert installed != before
    git_ai = json.loads((paths.cursor_dir() / "hooks.json").read_text())
    assert git_ai["hooks"]["preToolUse"][0] == {"command": "git-ai hook pre"}
    link = paths.agents_skills_dir() / bundles.SKILL_NAME
    assert link.is_symlink() and link.readlink() == bundles.SKILL_DIR

    assert apply_uninstall(Commands()).ok
    assert snapshot(skip_relay_state=True) == outside_relay_state
    assert not paths.backups_dir().exists()
    assert not paths.skill_dup_backup_dir().exists()
    assert not paths.install_manifest().exists()

    assert apply_install(planned(seed), Commands()).ok
    assert without_backup_stamps(snapshot()) == without_backup_stamps(installed)


def test_a_second_install_changes_nothing_and_keeps_the_first_backups(seed: Seed) -> None:
    apply_install(planned(seed), Commands())
    tree = snapshot()
    again = planned(seed)
    assert [line for line in again.report() if not line.startswith(("unchanged", "launchctl", "code "))] == []
    outcome = apply_install(again, Commands())
    assert outcome.changed == []
    assert snapshot() == tree


def test_apply_runs_launchctl_and_installs_the_extension(seed: Seed) -> None:
    commands = Commands()
    apply_install(planned(seed), commands)
    labels = [a[-1] for a in commands.seen if a[:2] == ["launchctl", "bootstrap"]]
    assert [Path(label).stem for label in labels] == [
        "io.github.cyberquill.agent-relay.relayd",
        "io.github.cyberquill.agent-relay.opencode",
        "io.github.cyberquill.agent-relay.cursor-update",
    ]
    assert [str(seed.code), "--install-extension", str(install.vsix_path()), "--force"] in commands.seen
    for agent in (paths.launch_agents_dir() / f"{label}" for label in labels):
        assert agent.is_file()


def test_uninstall_boots_agents_out_and_removes_the_extension(seed: Seed) -> None:
    apply_install(planned(seed), Commands())
    report = install.uninstall_report()
    assert "launchctl bootout io.github.cyberquill.agent-relay.relayd" in report
    assert "code --uninstall-extension agent-relay.relay-bridge" in report
    commands = Commands()
    apply_uninstall(commands)
    assert commands.heads()[:3] == ["launchctl bootout", "launchctl bootout", "launchctl bootout"]
    assert [str(seed.code), "--uninstall-extension", "agent-relay.relay-bridge"] in commands.seen
    assert install.uninstall_report() == []


def test_a_failed_command_is_reported_not_hidden(seed: Seed) -> None:
    def refuse(argv: Sequence[str]) -> tuple[int, str]:
        return (5, "Bootstrap failed: 5: Input/output error") if "bootstrap" in argv else (0, "")

    outcome = apply_install(planned(seed), refuse)
    assert not outcome.ok
    assert all("exited 5" in problem for problem in outcome.problems)
    assert len(outcome.problems) == 3


def test_launch_agents_follow_the_adapters_and_installed_tools(seed: Seed) -> None:
    paths.cursor_agent_cli().unlink()
    config = RelayConfig(adapters=AdaptersConfig(opencode=False))
    chosen, notes = install.launch_agents(config, PYTHON)
    assert [a.name for a in chosen] == ["relayd"]
    assert any("opencode adapter is off" in n for n in notes)
    assert any("cursor-agent" in n for n in notes)


def test_a_real_folder_where_the_skill_link_goes_stops_the_install_before_any_write(seed: Seed) -> None:
    (paths.agents_skills_dir() / bundles.SKILL_NAME).mkdir(parents=True)
    before = snapshot()
    with pytest.raises(LinkBlockedError):
        apply_install(planned(seed), Commands())
    assert snapshot() == before


def test_an_unbuilt_extension_is_skipped_with_the_build_command(seed: Seed) -> None:
    (seed.extension_dir / "out" / "src" / "extension.js").unlink()
    plan = planned(seed)
    assert not plan.vsix
    assert any("npm ci && npm run build" in n for n in plan.notes)


def test_no_vscode_means_no_extension_step(seed: Seed) -> None:
    seed.code.unlink()
    plan = planned(seed)
    assert not plan.vsix and not any(line.startswith("code ") for line in plan.report())
