"""AC-35: import builds the union, sync round-trips without losing a server, output is valid JSON,
and secrets are never printed."""

from __future__ import annotations

import json

import keyring
import pytest
import yaml
from pydantic import JsonValue

from agent_relay import paths
from agent_relay.fileops import Manifest, restore
from agent_relay.sync import catalog
from agent_relay.sync.model import HARNESS_ORDER, Catalog, CatalogEntry, HarnessFile, ServerDef
from agent_relay.sync.plan import plan_import, plan_sync
from agent_relay.sync.secrets import MCP_KEYCHAIN_SERVICE, MissingMcpSecretError, SecretStash
from tests.sync.helpers import FAKE_SECRETS, fixture_names, install_fixtures, native_servers

PY = "/opt/relay/bin/python3"


@pytest.fixture
def imported() -> Catalog:
    install_fixtures()
    plan = plan_import(PY)
    plan.apply(Manifest())
    return catalog.load()


def every_fixture_server() -> set[str]:
    return set().union(*(fixture_names(h) for h in HARNESS_ORDER))


def test_import_is_the_union_with_conflicts_flagged(imported: Catalog) -> None:
    assert set(imported.servers) == every_fixture_server() | {"agent-relay"}
    conflicts = imported.conflicts()
    assert conflicts["GitHub"] == [HarnessFile.CURSOR]
    assert conflicts["webex-messaging"] == [HarnessFile.CURSOR]
    assert "atlassian" not in conflicts
    github = imported.servers["GitHub"]
    assert github.sources == [HarnessFile.COPILOT, HarnessFile.CURSOR]
    assert github.server.headers == {}
    assert github.variants[HarnessFile.CURSOR].headers == {
        "Authorization": "${keychain:GitHub.headers.Authorization}"
    }


def test_import_notes_name_each_conflict() -> None:
    install_fixtures()
    notes = plan_import(PY).notes
    assert any("conflict: 'GitHub' differs in cursor" in n for n in notes)
    assert any("literal secret(s) move to the Keychain" in n for n in notes)


def test_import_keeps_secrets_out_of_mcp_yaml(imported: Catalog) -> None:
    text = paths.mcp_yaml().read_text(encoding="utf-8")
    stored = " ".join(
        keyring.get_password(MCP_KEYCHAIN_SERVICE, a) or "" for a in imported_accounts(imported)
    )
    for secret in FAKE_SECRETS:
        assert secret not in text
        assert secret in stored
    sonar = imported.servers["sonarqube"].server
    assert sonar.env["SONARQUBE_TOKEN"] == "${keychain:sonarqube.env.SONARQUBE_TOKEN}"
    assert sonar.env["SONARQUBE_URL"] == "https://sonar.example.com"
    assert sonar.env["WORKDIR"] == "${env:PWD}"
    assert yaml.safe_load(text)["version"] == 1


def imported_accounts(source: Catalog) -> set[str]:
    found: set[str] = set()
    for entry in source.servers.values():
        for server in [entry.server, *entry.variants.values()]:
            found |= server.keychain_accounts()
    return found


def test_import_dry_run_stores_nothing() -> None:
    install_fixtures()
    plan = plan_import(PY)
    assert plan.changed()
    assert not paths.mcp_yaml().exists()
    assert keyring.get_password(MCP_KEYCHAIN_SERVICE, "sonarqube.env.SONARQUBE_TOKEN") is None


def test_sync_round_trip_loses_no_server(imported: Catalog) -> None:
    plan_sync(imported, PY).apply(Manifest())
    union = every_fixture_server() | {"agent-relay"}
    for harness in HARNESS_ORDER:
        names = set(native_servers(harness))
        assert fixture_names(harness) <= names, harness
        expected = union - ({"needs-input"} if harness is not HarnessFile.VSCODE else set())
        assert names == expected, harness
    again = catalog.union({h: catalog.read_servers(h, SecretStash()) for h in HARNESS_ORDER})
    for name, entry in imported.servers.items():
        if name == "agent-relay":
            continue
        for harness in entry.sources:
            assert again.servers[name].for_harness(harness).core() == entry.for_harness(harness).core(), name


def test_sync_restores_literal_values_in_native_files(imported: Catalog) -> None:
    plan_sync(imported, PY).apply(Manifest())
    cursor = native_servers(HarnessFile.CURSOR)
    github = cursor["GitHub"]
    assert isinstance(github, dict)
    assert github["headers"] == {"Authorization": "Bearer fake-github-value-0000"}
    copilot = native_servers(HarnessFile.COPILOT)
    sonar = copilot["sonarqube"]
    assert isinstance(sonar, dict)
    assert sonar["env"] == {
        "SONARQUBE_URL": "https://sonar.example.com",
        "SONARQUBE_TOKEN": "fake-sonar-value-0000",
        "SONARQUBE_IDE_PORT": "64120",
        "WORKDIR": "${PWD}",
    }
    assert sonar["source"] == "user"


def test_renderers_produce_strict_json_and_keep_other_keys(imported: Catalog) -> None:
    plan_sync(imported, PY).apply(Manifest())
    for harness in HARNESS_ORDER:
        json.loads(catalog.native_path(harness).read_text(encoding="utf-8"))
    opencode = json.loads(catalog.native_path(HarnessFile.OPENCODE).read_text(encoding="utf-8"))
    assert opencode["$schema"] == "https://opencode.ai/config.json"
    vscode = json.loads(catalog.native_path(HarnessFile.VSCODE).read_text(encoding="utf-8"))
    assert vscode["inputs"][0]["id"] == "example-key"


def env_value(harness: HarnessFile, server: str, section: str, key: str) -> JsonValue:
    entry = native_servers(harness)[server]
    assert isinstance(entry, dict)
    values = entry[section]
    assert isinstance(values, dict)
    return values[key]


def test_env_references_use_each_harness_syntax(imported: Catalog) -> None:
    plan_sync(imported, PY).apply(Manifest())
    assert env_value(HarnessFile.VSCODE, "sonarqube", "env", "WORKDIR") == "${env:PWD}"
    assert env_value(HarnessFile.CURSOR, "sonarqube", "env", "WORKDIR") == "${env:PWD}"
    assert env_value(HarnessFile.COPILOT, "sonarqube", "env", "WORKDIR") == "${PWD}"
    assert env_value(HarnessFile.OPENCODE, "sonarqube", "environment", "WORKDIR") == "{env:PWD}"


def test_relay_mcp_is_registered_everywhere_with_the_opencode_timeout(imported: Catalog) -> None:
    plan_sync(imported, PY).apply(Manifest())
    relay = native_servers(HarnessFile.OPENCODE)["agent-relay"]
    assert relay == {
        "type": "local",
        "command": [PY, "-m", "agent_relay.mcp.server"],
        "timeout": catalog.RELAY_MCP_TIMEOUT_MS,
    }
    assert catalog.RELAY_MCP_TIMEOUT_MS >= 900_000
    cursor = native_servers(HarnessFile.CURSOR)["agent-relay"]
    assert cursor == {"command": PY, "args": ["-m", "agent_relay.mcp.server"]}


def test_secrets_are_never_printed(imported: Catalog) -> None:
    plan = plan_sync(imported, PY)
    shown = plan.diff() + "\n".join(plan.notes)
    assert "<redacted>" in shown
    for secret in FAKE_SECRETS:
        assert secret not in shown
    importing = plan_import(PY)
    for secret in FAKE_SECRETS:
        assert secret not in importing.diff() + "\n".join(importing.notes)


def test_vscode_only_reference_is_skipped_elsewhere_with_a_note(imported: Catalog) -> None:
    notes = plan_sync(imported, PY).notes
    assert "copilot: 'needs-input' uses a vscode-only reference; not rendered" in notes


def test_sync_dry_run_writes_nothing(imported: Catalog) -> None:
    before = {h: catalog.native_path(h).read_bytes() for h in HARNESS_ORDER}
    plan = plan_sync(imported, PY)
    assert plan.changed()
    assert {h: catalog.native_path(h).read_bytes() for h in HARNESS_ORDER} == before


def test_second_sync_is_a_no_op_and_restore_is_exact(imported: Catalog) -> None:
    before = {h: catalog.native_path(h).read_bytes() for h in HARNESS_ORDER}
    manifest = Manifest()
    plan_sync(imported, PY).apply(manifest)
    assert plan_sync(imported, PY).changed() == []
    restore(manifest)
    assert {h: catalog.native_path(h).read_bytes() for h in HARNESS_ORDER} == before


def test_missing_keychain_secret_is_a_clear_error() -> None:
    source = Catalog(
        servers={
            "x": CatalogEntry(
                server=ServerDef(transport="http", url="https://x", headers={"A": "${keychain:x.A}"})
            )
        }
    )
    with pytest.raises(MissingMcpSecretError, match="x.A"):
        plan_sync(source, PY, resolver=lambda _: None)


def test_sync_without_any_harness_files_registers_relay_only() -> None:
    plan = plan_sync(Catalog(), PY)
    plan.apply(Manifest())
    for harness in HARNESS_ORDER:
        assert set(native_servers(harness)) == {"agent-relay"}
