"""`relay config import` and `relay config sync` as plans: diffs first, writes only when applied."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_relay import paths
from agent_relay.fileops import Manifest, PlannedWrite, apply_writes
from agent_relay.sync import catalog, cursor_permissions, jsonc
from agent_relay.sync.model import HARNESS_ORDER, Catalog, HarnessFile
from agent_relay.sync.secrets import Resolver, Revealer, SecretStash, keychain_resolver


class CatalogMissingError(RuntimeError):
    """Syncing an absent catalog would empty every harness's server list, so it is refused."""

    def __init__(self, file: Path) -> None:
        super().__init__(f"{file} does not exist: run `relay config import --apply` first")
        self.file = file


@dataclass
class Plan:
    writes: list[PlannedWrite] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    revealer: Revealer = field(default_factory=Revealer)
    stash: SecretStash = field(default_factory=SecretStash)

    def changed(self) -> list[PlannedWrite]:
        return [w for w in self.writes if w.changed()]

    def diff(self) -> str:
        """Every change as a unified diff, with secrets hidden."""
        return self.revealer.hide("".join(w.diff() for w in self.changed()))

    def apply(self, manifest: Manifest | None = None) -> list[Path]:
        self.stash.store()
        target = manifest if manifest is not None else Manifest.load()
        return apply_writes(self.changed(), target)


def _mode(file: Path, default: int = 0o600) -> int:
    return file.stat().st_mode & 0o777 if file.is_file() else default


def plan_import(python: str | None = None) -> Plan:
    plan = Plan()
    imported = catalog.import_all(plan.stash, python)
    file = paths.mcp_yaml()
    plan.writes.append(
        PlannedWrite(
            path=file,
            content=catalog.dump(imported).encode(),
            mode=_mode(file),
            reason="the union of every harness's MCP servers",
        )
    )
    sources = {s for entry in imported.servers.values() for s in entry.sources}
    found = ", ".join(sorted(sources)) or "no harness"
    plan.notes.append(f"{len(imported.servers)} server(s) from {found} plus agent-relay")
    for name, harnesses in imported.conflicts().items():
        plan.notes.append(
            f"conflict: '{name}' differs in {', '.join(harnesses)}; each keeps its own definition"
        )
    if plan.stash.found:
        plan.notes.append(
            f"{len(plan.stash.found)} literal secret(s) move to the Keychain (service agent-relay-mcp); "
            "mcp.yaml keeps references"
        )
    return plan


def render_harness(source: Catalog, harness: HarnessFile, revealer: Revealer) -> bytes:
    rendered = catalog.FORMATS[harness].render(
        source.for_harness(harness), catalog.read_document(harness), revealer
    )
    return jsonc.dumps(rendered).encode()


def plan_sync(
    source: Catalog | None = None,
    python: str | None = None,
    resolver: Resolver = keychain_resolver,
    cursor_db: Path | None = None,
) -> Plan:
    if source is None and not paths.mcp_yaml().is_file():
        raise CatalogMissingError(paths.mcp_yaml())
    plan = Plan(revealer=Revealer(resolver))
    full = catalog.with_relay(source if source is not None else catalog.load(), python)
    for harness in HARNESS_ORDER:
        file = catalog.native_path(harness)
        plan.writes.append(
            PlannedWrite(
                path=file,
                content=render_harness(full, harness, plan.revealer),
                mode=_mode(file),
                reason=f"{harness.value} MCP servers from ~/.agents/mcp.yaml",
            )
        )
        for name, owner in full.skipped_for(harness).items():
            plan.notes.append(f"{harness.value}: '{name}' uses a {owner.value}-only reference; not rendered")
    permissions, note = cursor_permissions.plan(cursor_db)
    plan.writes.extend(permissions)
    plan.notes.append(note)
    return plan
