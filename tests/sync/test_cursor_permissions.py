"""`~/.cursor/cli-config.json` permissions rendered from the Cursor desktop allowlist."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_relay import paths
from agent_relay.fileops import Manifest, apply_writes
from agent_relay.sync import cursor_permissions as cp


def desktop_db(target: Path, composer: dict[str, object] | None) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        if composer is not None:
            conn.execute(
                "INSERT INTO ItemTable VALUES (?, ?)", (cp.STATE_KEY, json.dumps({"composerState": composer}))
            )
    return target


def cli_config(document: dict[str, object]) -> Path:
    target = paths.cursor_cli_config()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document), encoding="utf-8")
    target.chmod(0o640)
    return target


ALLOWLIST: dict[str, object] = {
    "yoloCommandAllowlist": ["git", "pip install", " "],
    "yoloCommandDenylist": ["rm"],
    "webFetchDomainAllowlist": ["docs.github.com"],
    "mcpAllowedTools": ["atlassian:getjiraissue", "malformed"],
}


def test_reads_the_desktop_allowlist_read_only(tmp_path: Path) -> None:
    db = desktop_db(tmp_path / "state.vscdb", ALLOWLIST)
    before = db.read_bytes()
    found = cp.read_ide_allowlist(db)
    assert found == cp.IdeAllowlist(
        commands=["git", "pip install"],
        denied=["rm"],
        domains=["docs.github.com"],
        mcp_tools=["atlassian:getjiraissue", "malformed"],
    )
    assert db.read_bytes() == before


def test_tokens_follow_the_cli_permission_syntax() -> None:
    rendered = cp.tokens(
        cp.IdeAllowlist.model_validate(
            {
                "commands": ["git", "pip install", "git"],
                "denied": ["rm -rf"],
                "domains": ["docs.github.com"],
                "mcp_tools": ["atlassian:getjiraissue", "malformed"],
            }
        )
    )
    assert rendered.allow == [
        "Shell(git)",
        "Shell(pip:install*)",
        "WebFetch(docs.github.com)",
        "Mcp(atlassian:getjiraissue)",
    ]
    assert rendered.deny == ["Shell(rm:-rf*)"]


def test_plan_keeps_hand_added_tokens_and_other_settings(tmp_path: Path) -> None:
    db = desktop_db(tmp_path / "state.vscdb", ALLOWLIST)
    config = cli_config({"approvalMode": "allowlist", "permissions": {"allow": ["Shell(ls)"], "deny": []}})
    writes, note = cp.plan(db)
    apply_writes(writes, Manifest())
    document = json.loads(config.read_text(encoding="utf-8"))
    assert document["approvalMode"] == "allowlist"
    assert document["permissions"]["allow"] == [
        "Shell(ls)",
        "Shell(git)",
        "Shell(pip:install*)",
        "WebFetch(docs.github.com)",
        "Mcp(atlassian:getjiraissue)",
    ]
    assert document["permissions"]["deny"] == ["Shell(rm)"]
    assert config.stat().st_mode & 0o777 == 0o640
    assert note.startswith("Cursor CLI permissions: 4 allow and 1 deny")


def test_a_token_removed_on_the_desktop_is_removed_from_the_cli(tmp_path: Path) -> None:
    db = desktop_db(tmp_path / "state.vscdb", ALLOWLIST)
    config = cli_config({"permissions": {"allow": ["Shell(ls)"]}})
    apply_writes(cp.plan(db)[0], Manifest())
    desktop_db(tmp_path / "state.vscdb", {**ALLOWLIST, "yoloCommandAllowlist": ["git"]})
    apply_writes(cp.plan(db)[0], Manifest())
    allow = json.loads(config.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "Shell(pip:install*)" not in allow
    assert allow[:2] == ["Shell(ls)", "Shell(git)"]


def test_second_plan_is_a_no_op(tmp_path: Path) -> None:
    db = desktop_db(tmp_path / "state.vscdb", ALLOWLIST)
    cli_config({})
    apply_writes(cp.plan(db)[0], Manifest())
    assert [w for w in cp.plan(db)[0] if w.changed()] == []


def test_skipped_without_the_cli_or_the_desktop(tmp_path: Path) -> None:
    writes, note = cp.plan(tmp_path / "missing.vscdb")
    assert writes == [] and "does not exist" in note
    cli_config({})
    writes, note = cp.plan(tmp_path / "missing.vscdb")
    assert writes == [] and "could not be read" in note


def test_desktop_without_saved_state_renders_nothing_new(tmp_path: Path) -> None:
    db = desktop_db(tmp_path / "state.vscdb", None)
    assert cp.read_ide_allowlist(db) == cp.IdeAllowlist()
    (tmp_path / "garbage.vscdb").write_text("not sqlite", encoding="utf-8")
    assert cp.read_ide_allowlist(tmp_path / "garbage.vscdb") is None
