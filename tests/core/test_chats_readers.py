"""D146: each app's own store is read into `DeskChat`s: title, last prompt, last reply, running or not."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_relay import paths
from agent_relay.core.chats import copilot, cursor, opencode, vscode
from agent_relay.core.chats import text as t
from agent_relay.core.chats.model import DeskChat
from agent_relay.models.events import Harness
from tests.core import chat_stores as stores


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    return folder


def read(reader: object, ws: Path) -> list[DeskChat]:
    assert callable(reader)
    found = reader(time.time() - 7 * 86400, [ws.resolve()], time.time())
    assert isinstance(found, list)
    return found


# text shaping


def test_ends_keeps_the_first_and_last_hundred_characters() -> None:
    text = "a" * 150 + "MIDDLE" + "z" * 150
    shown = t.ends(text)
    assert shown == "a" * 100 + " … " + "z" * 100
    assert "MIDDLE" not in shown


def test_ends_leaves_a_short_message_whole_and_squashes_whitespace() -> None:
    assert t.ends("one\n\n  two   three") == "one two three"


def test_code_fences_are_dropped_from_a_snippet() -> None:
    assert t.squash("see:\n```python\nprint(1)\n```\ndone") == "see: print(1) done"


def test_a_cursor_prompt_loses_its_wrappers() -> None:
    raw = "<timestamp>Mon</timestamp>\n<user_query>\nFix it\n</user_query>"
    assert t.user_text(raw) == "Fix it"
    assert t.user_text("<system_reminder>x</system_reminder>plain") == "plain"


def test_short_cuts_with_an_ellipsis() -> None:
    assert t.short("x" * 100, 10) == "x" * 9 + "…"


# Cursor


def test_a_cursor_chat_has_its_prompt_as_title_and_the_text_after_the_last_tool_call_as_reply(
    ws: Path,
) -> None:
    stores.cursor_chat(ws, "c1")
    [chat] = read(cursor.read, ws)
    assert chat.app is Harness.CURSOR
    assert chat.native_id == "c1" and chat.workspace == str(ws.resolve())
    assert chat.title == "Add a parser test" and chat.last_prompt == "Add a parser test"
    assert chat.last_reply == "Done: the parser has a test."
    assert not chat.running and chat.transcript.endswith("c1.jsonl")


def test_a_cursor_chat_in_the_cli_store_is_a_cursor_cli_chat(ws: Path) -> None:
    stores.cursor_chat(ws, "c1", in_cli=True)
    assert [c.app for c in read(cursor.read, ws)] == [Harness.CURSOR_CLI]


def test_a_cursor_chat_still_working_is_running(ws: Path) -> None:
    stores.cursor_chat(ws, "c1", finished=False, seconds_old=5)
    [chat] = read(cursor.read, ws)
    assert chat.running and chat.last_reply is None


def test_a_quiet_cursor_chat_is_not_running_even_without_a_final_message(ws: Path) -> None:
    stores.cursor_chat(ws, "c1", finished=False, seconds_old=3600)
    assert not read(cursor.read, ws)[0].running


def test_a_cursor_chat_older_than_the_window_is_left_out(ws: Path) -> None:
    stores.cursor_chat(ws, "old", seconds_old=9 * 86400)
    assert read(cursor.read, ws) == []


def test_a_cursor_chat_in_an_unregistered_folder_has_no_workspace(ws: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    stores.cursor_chat(other, "c9")
    [chat] = read(cursor.read, ws)
    assert chat.workspace == ""


def test_a_secret_in_a_chat_never_reaches_the_list(ws: Path) -> None:
    stores.cursor_chat(ws, "c1", reply="the token is ghp_" + "a" * 36)
    [chat] = read(cursor.read, ws)
    assert chat.last_reply is not None and "ghp_" not in chat.last_reply


def test_a_missing_cursor_store_is_an_empty_list(ws: Path) -> None:
    assert read(cursor.read, ws) == []


# Copilot


def test_an_agent_host_chat_is_a_vscode_copilot_chat_and_the_cli_one_is_not(ws: Path) -> None:
    stores.copilot_chat(ws, "h1", host=True)
    stores.copilot_chat(ws, "k1", host=False)
    apps = {c.native_id: c.app for c in read(copilot.read, ws)}
    assert apps == {"h1": Harness.VSCODE_COPILOT, "k1": Harness.COPILOT_CLI}


def test_a_copilot_chat_reply_is_the_last_message_not_the_narration(ws: Path) -> None:
    stores.copilot_chat(ws, "h1")
    [chat] = read(copilot.read, ws)
    assert chat.last_reply == "Triage is done." and chat.last_prompt == "Run the triage skill"
    assert chat.title == "Run the triage skill" and not chat.running


def test_a_yaml_block_name_is_no_title(ws: Path) -> None:
    stores.copilot_chat(ws, "h1", name="|-")
    assert read(copilot.read, ws)[0].title == "Run the triage skill"


def test_a_named_copilot_chat_uses_its_name(ws: Path) -> None:
    stores.copilot_chat(ws, "h1", name="Triage the Linux failures")
    assert read(copilot.read, ws)[0].title == "Triage the Linux failures"


def test_a_copilot_chat_between_tool_calls_is_running(ws: Path) -> None:
    stores.copilot_chat(ws, "h1", finished=False, seconds_old=3)
    [chat] = read(copilot.read, ws)
    assert chat.running and chat.last_reply is None


def test_a_session_state_folder_without_a_cwd_is_skipped(ws: Path) -> None:
    folder = paths.copilot_session_state() / "bare"
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text("{}\n", encoding="utf-8")
    assert read(copilot.read, ws) == []


# VS Code's own agent


def test_a_vscode_chat_is_replayed_from_its_patches(ws: Path) -> None:
    stores.vscode_chat(ws, "v1", title="Router tour")
    [chat] = read(vscode.read, ws)
    assert chat.app is Harness.VSCODE and chat.workspace == str(ws.resolve())
    assert chat.title == "Router tour" and chat.last_prompt == "Explain the router"
    assert chat.last_reply == "The router fans events out."
    assert not chat.running


def test_a_vscode_chat_without_a_title_uses_its_prompt(ws: Path) -> None:
    stores.vscode_chat(ws, "v1")
    assert read(vscode.read, ws)[0].title == "Explain the router"


def test_a_vscode_chat_with_no_completion_mark_and_fresh_writes_is_running(ws: Path) -> None:
    stores.vscode_chat(ws, "v1", finished=False, seconds_old=4)
    assert read(vscode.read, ws)[0].running


def test_a_vscode_window_without_a_folder_has_no_workspace(ws: Path) -> None:
    stores.vscode_chat(ws, "v1")
    (paths.vscode_workspace_storage() / "abc123" / "workspace.json").write_text(
        '{"workspace": "file:///x/y.code-workspace"}', encoding="utf-8"
    )
    assert read(vscode.read, ws)[0].workspace == ""


# OpenCode


def test_an_opencode_chat_comes_from_its_database(ws: Path) -> None:
    stores.opencode_db(ws, "ses_1")
    [chat] = read(opencode.read, ws)
    assert chat.app is Harness.OPENCODE and chat.native_id == "ses_1"
    assert chat.last_prompt == "Send the report" and chat.last_reply == "Sent."
    assert not chat.running


def test_an_opencode_turn_with_no_completion_time_is_running(ws: Path) -> None:
    stores.opencode_db(ws, "ses_1", finished=False, seconds_old=3)
    assert read(opencode.read, ws)[0].running


def test_opencode_sub_agent_sessions_are_not_chats(ws: Path) -> None:
    stores.opencode_db(ws, "ses_1")
    stores.opencode_db(ws, "ses_2", parent="ses_1")
    assert [c.native_id for c in read(opencode.read, ws)] == ["ses_1"]


def test_a_broken_opencode_database_is_an_empty_list(ws: Path) -> None:
    file = paths.opencode_db()
    file.parent.mkdir(parents=True)
    file.write_text("not a database", encoding="utf-8")
    assert read(opencode.read, ws) == []
