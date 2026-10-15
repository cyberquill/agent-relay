"""`relay install` and `relay uninstall`: everything in the plan's touch table, as one reversible plan.

Install is a dry run until applied. Every file change goes through `fileops`, so the manifest holds
the state before the relay's first change to each path. Uninstall removes what the relay created,
strips only its entries from files it merged into, restores a backup only over a file nobody has
edited since, and leaves the rest. Commands (launchctl, `code`) run only when applied.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent_relay import paths
from agent_relay.fileops import (
    LinkBlockedError,
    Manifest,
    PlannedLink,
    PlannedWrite,
    Unmerge,
    apply_links,
    apply_moves,
    apply_writes,
    decide,
    restore,
)
from agent_relay.hook.installers import copilot, cursor, opencode
from agent_relay.log import get_logger
from agent_relay.models.config import RelayConfig
from agent_relay.ops import bundles, launchd, stepping_away
from agent_relay.ops.launchd import LaunchAgent
from agent_relay.ops.vsix import EXTENSION_DIR, VsixError, build_vsix
from agent_relay.sync import catalog, jsonc
from agent_relay.sync.model import HARNESS_ORDER, HarnessFile
from agent_relay.sync.plan import Plan, plan_mcp
from agent_relay.sync.skills import DedupePlan, plan_dedupe

log = get_logger(__name__)

type Runner = Callable[[Sequence[str]], tuple[int, str]]

RUN_TIMEOUT_S = 120


def run(argv: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, exc.__class__.__name__
    return done.returncode, (done.stdout + done.stderr).strip()


def vsix_path() -> Path:
    return paths.app_support() / "relay-bridge.vsix"


def extension_id(extension_dir: Path = EXTENSION_DIR) -> str:
    package = json.loads((extension_dir / "package.json").read_text(encoding="utf-8"))
    return f"{package['publisher']}.{package['name']}".lower()


def skill_links() -> list[PlannedLink]:
    return [
        PlannedLink(
            path=paths.agents_skills_dir() / folder.name,
            target=folder,
            reason=f"the {folder.name} skill, read by every harness from ~/.agents/skills",
        )
        for folder in bundles.skill_dirs()
    ]


def launch_agents(config: RelayConfig, python: str) -> tuple[list[LaunchAgent], list[str]]:
    chosen: list[LaunchAgent] = []
    notes: list[str] = []
    for agent in launchd.agents(python):
        if agent.name == "opencode" and not config.adapters.opencode:
            notes.append("skip launchd opencode: the opencode adapter is off")
        elif agent.name == "cursor-update" and not paths.cursor_agent_cli().is_file():
            notes.append(f"skip launchd cursor-update: {paths.cursor_agent_cli()} is not installed")
        else:
            chosen.append(agent)
    return chosen, notes


@dataclass
class InstallPlan:
    files: list[PlannedWrite] = field(default_factory=list)
    mcp: Plan = field(default_factory=Plan)
    links: list[PlannedLink] = field(default_factory=list)
    dedupe: DedupePlan = field(default_factory=DedupePlan)
    agents: list[LaunchAgent] = field(default_factory=list)
    vsix: bool = False
    notes: list[str] = field(default_factory=list)

    def writes(self) -> list[PlannedWrite]:
        return [*self.files, *self.mcp.writes]

    def report(self) -> list[str]:
        """One line per change, without content. Unchanged paths are listed as such."""
        lines: list[str] = []
        for write in self.writes():
            verb = ("create" if write.current() is None else "update") if write.changed() else "unchanged"
            lines.append(f"{verb} {write.path} — {write.reason}")
        lines += [f"link {link.path} → {link.target}" for link in self.links if link.changed()]
        lines += [f"move {m.source} → {m.destination} — {m.reason}" for m in self.dedupe.moves]
        rewritten = {w.path for w in self.files if w.changed()}
        lines += [
            f"launchctl bootstrap {agent.label}"
            + ("" if agent.path in rewritten else " (only if not loaded)")
            for agent in self.agents
        ]
        if self.vsix:
            lines.append(f"code --install-extension {vsix_path()}")
        return lines

    def diff(self) -> str:
        """Unified diffs of every changed file, with every secret hidden."""
        text = "".join(w.diff() for w in self.files if w.changed() and _is_text(w.content))
        return self.mcp.revealer.hide(text + self.mcp.diff())


def _is_text(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def plan_install(
    config: RelayConfig,
    python: str,
    cursor_db: Path | None = None,
    extension_dir: Path = EXTENSION_DIR,
    patch_dir: Path = stepping_away.PATCH_DIR,
) -> InstallPlan:
    plan = InstallPlan(mcp=plan_mcp(python, cursor_db))
    plan.notes += plan.mcp.notes
    plan.files += copilot.plan(config, python)
    plan.files += cursor.plan(config, python)
    plan.files += opencode.plan()
    patch, note = stepping_away.plan(patch_dir=patch_dir)
    plan.files += patch
    plan.notes.append(note)
    plan.agents, skipped = launch_agents(config, python)
    plan.files += [
        PlannedWrite(path=a.path, content=a.render(), mode=0o644, reason=f"launchd agent {a.label}")
        for a in plan.agents
    ]
    plan.notes += skipped
    plan.links += skill_links()
    plan.dedupe = plan_dedupe()
    plan.notes += [line for line in plan.dedupe.report() if line.startswith("keep ")]
    if config.adapters.vscode:
        _plan_vsix(plan, extension_dir)
    return plan


def _plan_vsix(plan: InstallPlan, extension_dir: Path) -> None:
    if not paths.VSCODE_CLI.is_file():
        plan.notes.append(f"skip relay-bridge: {paths.VSCODE_CLI} is missing")
        return
    with tempfile.TemporaryDirectory() as scratch:
        try:
            built = build_vsix(Path(scratch) / "relay-bridge.vsix", extension_dir)
        except VsixError as exc:
            plan.notes.append(f"skip relay-bridge: {exc}")
            return
        content = built.read_bytes()
    plan.files.append(PlannedWrite(path=vsix_path(), content=content, reason="relay-bridge VSIX for VS Code"))
    plan.vsix = True


@dataclass
class Outcome:
    changed: list[Path] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _command(argv: Sequence[str], runner: Runner, outcome: Outcome, required: bool = True) -> None:
    code, output = runner(argv)
    outcome.commands.append(" ".join(argv))
    if code != 0 and required:
        outcome.problems.append(f"`{' '.join(argv)}` exited {code}: {output[:300]}")


def apply_install(
    plan: InstallPlan, runner: Runner = run, sleep: Callable[[float], None] = time.sleep
) -> Outcome:
    blocked = next((link.path for link in plan.links if link.blocked()), None)
    if blocked is not None:
        raise LinkBlockedError(blocked)
    outcome = Outcome()
    manifest = Manifest.load()
    plan.mcp.stash.store()
    outcome.changed += apply_writes([w for w in plan.writes() if w.changed()], manifest)
    outcome.changed += apply_links(plan.links, manifest)
    outcome.changed += apply_moves(plan.dedupe.moves, manifest)
    rewritten = set(outcome.changed)
    for agent in plan.agents:
        _load(agent, agent.path in rewritten, runner, outcome, sleep)
    if plan.vsix:
        _command([str(paths.VSCODE_CLI), "--install-extension", str(vsix_path()), "--force"], runner, outcome)
    log.info(f"install applied: {len(outcome.changed)} change(s), {len(outcome.problems)} problem(s)")
    return outcome


def _load(
    agent: LaunchAgent, rewritten: bool, runner: Runner, outcome: Outcome, sleep: Callable[[float], None]
) -> None:
    """Bootstrap an agent that is not loaded; reload one only when its plist changed."""
    loaded = launchd.is_loaded(agent, runner)
    if loaded and not rewritten:
        return
    if loaded:
        _command(launchd.bootout_command(agent), runner, outcome, required=False)
        launchd.wait_unloaded(agent, runner, sleep)
    _command(launchd.enable_command(agent), runner, outcome, required=False)
    _command(launchd.bootstrap_command(agent), runner, outcome)


def _drop_relay_server(harness: HarnessFile) -> Unmerge:
    key: str = catalog.FORMATS[harness].KEY

    def unmerge(content: bytes) -> bytes:
        document = jsonc.loads(content.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("MCP config is not a JSON object")
        servers = document.get(key)
        if isinstance(servers, dict):
            document[key] = {k: v for k, v in servers.items() if k != catalog.RELAY_SERVER}
        return jsonc.dumps(document).encode()

    return unmerge


def _mcp_files(harness: HarnessFile) -> list[Path]:
    if harness is HarnessFile.OPENCODE:
        folder = paths.opencode_config_dir()
        return [folder / "opencode.json", folder / "opencode.jsonc"]
    return [catalog.native_path(harness)]


def unmergers() -> dict[str, Unmerge]:
    """Files the relay merges into rather than owns: uninstall strips its entries from the current bytes."""
    found: dict[str, Unmerge] = {str(cursor.hooks_file()): cursor.unmerge_bytes}
    for harness in HARNESS_ORDER:
        drop = _drop_relay_server(harness)
        found.update({str(file): drop for file in _mcp_files(harness)})
    return found


def uninstall_report(manifest: Manifest | None = None) -> list[str]:
    current = manifest if manifest is not None else Manifest.load()
    merges = unmergers()
    lines: list[str] = []
    for key, entry in sorted(current.entries.items()):
        fate = decide(key, entry, merges)[0]
        lines.append(f"{fate} {key}" + (" (changed since install)" if fate == "leave" else ""))
    lines += [f"launchctl bootout {agent.label}" for agent in _installed_agents(current)]
    if _extension_installed(current):
        lines.append(f"code --uninstall-extension {extension_id()}")
    return lines


def _extension_installed(manifest: Manifest) -> bool:
    return str(vsix_path()) in manifest.entries and paths.VSCODE_CLI.is_file()


def _installed_agents(manifest: Manifest) -> list[LaunchAgent]:
    return [a for a in launchd.agents("python") if str(a.path) in manifest.entries]


def apply_uninstall(runner: Runner = run) -> Outcome:
    outcome = Outcome()
    manifest = Manifest.load()
    for agent in _installed_agents(manifest):
        _command(launchd.bootout_command(agent), runner, outcome, required=False)
    if _extension_installed(manifest):
        _command([str(paths.VSCODE_CLI), "--uninstall-extension", extension_id()], runner, outcome, False)
    backups = [Path(e.backup) for e in manifest.entries.values() if e.backup]
    moved = [Path(e.moved_to) for e in manifest.entries.values() if e.moved_to]
    report = restore(manifest, unmergers=unmergers())
    outcome.changed += report.changed
    outcome.notes += report.notes
    discard_backups([b for b in backups if b not in report.kept_backups], moved)
    paths.install_manifest().unlink(missing_ok=True)
    log.info(f"uninstall applied: {len(outcome.changed)} path(s) restored, {len(report.left)} left in place")
    return outcome


def discard_backups(backups: list[Path], moved: list[Path]) -> None:
    """Delete restored backups, then the directories they leave empty, up to each backup root."""
    roots = (paths.backups_dir(), paths.skill_dup_backup_dir())
    for backup in backups:
        if backup.is_file() and any(backup.is_relative_to(r) for r in roots):
            backup.unlink()
    for leaf in [*backups, *moved]:
        root = next((r for r in roots if leaf.is_relative_to(r)), None)
        if root is not None:
            _prune_empty(leaf.parent if not leaf.is_dir() else leaf, root)


def _prune_empty(folder: Path, root: Path) -> None:
    while folder.is_relative_to(root):
        if folder.is_dir():
            if any(folder.iterdir()):
                return
            folder.rmdir()
        if folder == root:
            return
        folder = folder.parent
