"""`relay config import` and `relay config sync` as plans: diffs first, writes only when applied."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path

from agent_relay import paths, settings
from agent_relay.fileops import Manifest, PlannedWrite, apply_writes
from agent_relay.gateway.store import KEY_ACCOUNT
from agent_relay.sync import catalog, cursor_permissions, jsonc
from agent_relay.sync.gateway import keep_gateway_entries, point_at_gateway
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
    return _import(python)[0]


def _import(python: str | None) -> tuple[Plan, Catalog]:
    plan = Plan()
    imported = keep_gateway_entries(catalog.import_all(plan.stash, python), catalog.load())
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
    return plan, imported


def render_harness(source: Catalog, harness: HarnessFile, revealer: Revealer) -> bytes:
    rendered = catalog.FORMATS[harness].render(
        source.for_harness(harness), catalog.read_document(harness), revealer
    )
    return jsonc.dumps(rendered).encode()


def _configured_gateway_port() -> int | None:
    config = settings.load_config()
    return config.gateway.port if config.gateway.enabled else None


def _provide_gateway_key(plan: Plan, resolver: Resolver) -> None:
    """The rendered files carry the gateway key; make one (stored only on apply) when none exists yet."""
    if resolver(KEY_ACCOUNT) is not None:
        return
    plan.stash.found[KEY_ACCOUNT] = secrets.token_urlsafe(32)
    stash = plan.stash
    plan.revealer.resolver = lambda name: stash.found.get(name) or resolver(name)
    plan.notes.append("a gateway key is created in the Keychain (service agent-relay-mcp)")


def plan_sync(
    source: Catalog | None = None,
    python: str | None = None,
    resolver: Resolver = keychain_resolver,
    cursor_db: Path | None = None,
    gateway_port: int | None = None,
) -> Plan:
    """The native files for the catalog. With `gateway_port`, gatewayed servers go through the gateway."""
    if source is None and not paths.mcp_yaml().is_file():
        raise CatalogMissingError(paths.mcp_yaml())
    plan = Plan(revealer=Revealer(resolver))
    full = catalog.with_relay(source if source is not None else catalog.load(), python)
    port = gateway_port if gateway_port is not None else _configured_gateway_port()
    if port is not None:
        full = point_at_gateway(full, port)
        if any(entry.upstream() for entry in full.servers.values()):
            _provide_gateway_key(plan, resolver)
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


def plan_mcp(python: str | None = None, cursor_db: Path | None = None) -> Plan:
    """What `relay install` runs: sync from the catalog, importing it first when there is none.

    On a first install the imported secrets are not in the Keychain yet, so the native files are
    rendered from the pending stash; applying the plan stores them before anything is written.
    """
    if paths.mcp_yaml().is_file():
        return plan_sync(python=python, cursor_db=cursor_db)
    imported, source = _import(python)
    stash = imported.stash

    def pending_first(name: str) -> str | None:
        return stash.found.get(name) or keychain_resolver(name)

    synced = plan_sync(source, python, pending_first, cursor_db)
    return Plan(
        writes=imported.writes + synced.writes,
        notes=imported.notes + synced.notes,
        revealer=synced.revealer,
        stash=stash,
    )
