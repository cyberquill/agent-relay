"""Gatewayed servers are rendered as the gateway's loopback address, and an import does not undo that."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from agent_relay import paths
from agent_relay.cli import app
from agent_relay.fileops import Manifest
from agent_relay.gateway.store import KEY_HEADER, KEY_REF, CredentialStore, gateway_key
from agent_relay.sync import catalog
from agent_relay.sync.gateway import gateway_url, keep_gateway_entries, point_at_gateway
from agent_relay.sync.model import Catalog, CatalogEntry, HarnessFile, ServerDef
from agent_relay.sync.plan import plan_import, plan_sync
from tests.sync.helpers import install_fixtures, native_servers

PY = "/opt/relay/bin/python3"
REMOTE = "https://mcp.example.test/v1/mcp"
runner = CliRunner()


def remote_catalog(gateway: bool = True, upstream: str | None = None) -> Catalog:
    entry = CatalogEntry(
        server=ServerDef(transport="http", url=REMOTE, headers={"Authorization": "Bearer x"}),
        gateway=gateway,
        gateway_url=upstream,
    )
    plain = CatalogEntry(server=ServerDef(transport="http", url="https://plain.example.test/mcp"))
    return Catalog(servers={"remote": entry, "plain": plain})


def test_a_gatewayed_server_is_rendered_as_the_loopback_address_with_the_key() -> None:
    pointed = point_at_gateway(remote_catalog(), 8791)
    routed = pointed.servers["remote"].server
    assert routed.url == gateway_url(8791, "remote") == "http://127.0.0.1:8791/mcp/remote"
    assert routed.headers == {KEY_HEADER: KEY_REF}
    assert pointed.servers["plain"].server.url == "https://plain.example.test/mcp"


def test_an_upstream_can_differ_from_the_apps_entry() -> None:
    entry = CatalogEntry(
        server=ServerDef(transport="stdio", command="npx", args=["mcp-remote", REMOTE]),
        gateway=True,
        gateway_url=REMOTE,
    )
    assert entry.upstream() == REMOTE
    assert CatalogEntry(server=entry.server, gateway=True).upstream() is None
    assert remote_catalog(gateway=False).servers["remote"].upstream() is None


def test_sync_renders_every_app_through_the_gateway_and_never_the_key_itself() -> None:
    install_fixtures()
    plan_import(PY).apply(Manifest())
    source = catalog.load()
    servers = {**source.servers, "remote": remote_catalog().servers["remote"]}
    plan_sync(source.model_copy(update={"servers": servers}), PY, gateway_port=8791).apply(Manifest())
    for harness in (HarnessFile.VSCODE, HarnessFile.CURSOR, HarnessFile.COPILOT):
        rendered = native_servers(harness)["remote"]
        assert json.dumps(rendered).count("127.0.0.1:8791/mcp/remote") == 1
        assert "Bearer x" not in json.dumps(rendered)
    cursor = json.dumps(native_servers(HarnessFile.CURSOR)["remote"])
    assert KEY_HEADER in cursor
    key = gateway_key()
    assert key is not None and key in cursor


def test_a_dry_run_makes_no_key(capsys: pytest.CaptureFixture[str]) -> None:
    install_fixtures()
    plan = plan_sync(remote_catalog(), PY, gateway_port=8791)
    assert any("gateway key" in n for n in plan.notes)
    assert gateway_key() is None


def test_an_import_of_the_rendered_files_keeps_the_real_upstream() -> None:
    before = remote_catalog()
    routed = point_at_gateway(before, 8791)
    read_back = Catalog(servers={"remote": routed.servers["remote"].model_copy(update={"gateway": False})})
    kept = keep_gateway_entries(read_back, before)
    assert kept.servers["remote"] == before.servers["remote"]


def test_a_gatewayed_server_defined_another_way_keeps_its_flag() -> None:
    before = remote_catalog(upstream=REMOTE)
    other = CatalogEntry(server=ServerDef(transport="http", url="https://elsewhere.example.test/mcp"))
    kept = keep_gateway_entries(Catalog(servers={"remote": other}), before)
    assert kept.servers["remote"].gateway
    assert kept.servers["remote"].gateway_url == REMOTE
    assert kept.servers["remote"].server.url == "https://elsewhere.example.test/mcp"


def test_a_server_gone_from_the_apps_is_not_brought_back() -> None:
    assert keep_gateway_entries(Catalog(servers={}), remote_catalog()).servers == {}


def test_the_gateway_command_flags_a_server_and_names_the_next_steps() -> None:
    paths.mcp_yaml().parent.mkdir(parents=True, exist_ok=True)
    paths.mcp_yaml().write_text(catalog.dump(remote_catalog(gateway=False)), encoding="utf-8")
    added = runner.invoke(app, ["gateway", "add", "remote"])
    assert added.exit_code == 0, added.output
    assert REMOTE in added.output
    assert "relay config sync --apply" in added.output
    assert catalog.load().servers["remote"].gateway
    status = runner.invoke(app, ["gateway", "status"])
    assert "remote: signed out" in status.output
    removed = runner.invoke(app, ["gateway", "remove", "remote"])
    assert removed.exit_code == 0
    assert not catalog.load().servers["remote"].gateway


def test_the_gateway_command_refuses_an_unknown_or_non_remote_server() -> None:
    paths.mcp_yaml().parent.mkdir(parents=True, exist_ok=True)
    stdio = CatalogEntry(server=ServerDef(transport="stdio", command="npx"))
    paths.mcp_yaml().write_text(catalog.dump(Catalog(servers={"local": stdio})), encoding="utf-8")
    assert runner.invoke(app, ["gateway", "add", "nope"]).exit_code == 1
    refused = runner.invoke(app, ["gateway", "add", "local"])
    assert refused.exit_code == 1
    assert "--url" in refused.output
    ok = runner.invoke(app, ["gateway", "add", "local", "--url", REMOTE])
    assert ok.exit_code == 0, ok.output


def test_a_client_you_registered_is_stored_with_its_secret_kept_out_of_the_output() -> None:
    paths.mcp_yaml().parent.mkdir(parents=True, exist_ok=True)
    paths.mcp_yaml().write_text(catalog.dump(remote_catalog()), encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "gateway",
            "client",
            "remote",
            "--client-id",
            "cid",
            "--secret-stdin",
            "--redirect-uri",
            "http://localhost:1/cb",
        ],
        input="fake-client-secret-0000\n",
    )
    assert result.exit_code == 0, result.output
    assert "fake-client-secret-0000" not in result.output
    client = CredentialStore().load("remote").client
    assert client is not None and client.client_secret == "fake-client-secret-0000"
    assert runner.invoke(app, ["gateway", "signout", "remote"]).exit_code == 0
    assert CredentialStore().load("remote").client == client
