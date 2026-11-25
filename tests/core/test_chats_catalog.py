"""D146: the catalog merges every store, keeps registered workspaces, and answers `/chats` filters."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_relay.core.chats import APP_WORDS, ChatCatalog, ago, copilot
from agent_relay.core.chats import catalog as catalog_module
from agent_relay.core.chats.catalog import LISTED, Reader
from agent_relay.core.chats.model import DeskChat
from agent_relay.models.config import Workspace
from agent_relay.models.events import Harness
from tests.core import chat_stores as stores


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    return folder


def workspaces(*folders: Path) -> list[Workspace]:
    return [Workspace(name=f.name, path=str(f)) for f in folders]


async def test_every_store_is_merged_newest_first(proj: Path) -> None:
    stores.cursor_chat(proj, "c1", seconds_old=7200)
    stores.copilot_chat(proj, "h1", seconds_old=60)
    stores.vscode_chat(proj, "v1", seconds_old=3600)
    stores.opencode_db(proj, "ses_1", seconds_old=10800)
    page = await ChatCatalog().page(workspaces(proj))
    assert [c.native_id for c in page.chats] == ["h1", "v1", "c1", "ses_1"]
    assert page.outside_workspaces == 0 and page.more == 0 and page.days == 7


async def test_a_chat_outside_the_registered_workspaces_is_counted_not_listed(
    proj: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    stores.copilot_chat(proj, "h1")
    stores.copilot_chat(other, "h2")
    stores.cursor_chat(other, "c2")
    page = await ChatCatalog().page(workspaces(proj))
    assert [c.native_id for c in page.chats] == ["h1"]
    assert page.outside_workspaces == 2


async def test_a_subfolder_of_a_workspace_belongs_to_it(proj: Path) -> None:
    sub = proj / "sub"
    sub.mkdir()
    stores.copilot_chat(sub, "h1")
    assert [c.native_id for c in (await ChatCatalog().page(workspaces(proj))).chats] == ["h1"]


async def test_the_window_is_a_week_unless_asked_otherwise(proj: Path) -> None:
    stores.copilot_chat(proj, "new", seconds_old=3600)
    stores.copilot_chat(proj, "old", seconds_old=10 * 86400)
    catalog = ChatCatalog()
    assert [c.native_id for c in (await catalog.page(workspaces(proj))).chats] == ["new"]
    longer = await catalog.page(workspaces(proj), days=14)
    assert {c.native_id for c in longer.chats} == {"new", "old"} and longer.days == 14


async def test_the_window_is_capped(proj: Path) -> None:
    assert (await ChatCatalog().page(workspaces(proj), days=5000)).days == 90


async def test_an_app_word_narrows_to_that_apps_chats(proj: Path) -> None:
    stores.cursor_chat(proj, "c1")
    stores.copilot_chat(proj, "h1")
    stores.copilot_chat(proj, "k1", host=False)
    page = await ChatCatalog().page(workspaces(proj), apps=APP_WORDS["copilot"])
    assert {c.native_id for c in page.chats} == {"h1", "k1"}
    only = await ChatCatalog().page(workspaces(proj), apps=APP_WORDS["cursor"])
    assert [c.native_id for c in only.chats] == ["c1"]


async def test_words_match_the_title_the_prompt_and_the_reply(proj: Path) -> None:
    stores.copilot_chat(proj, "h1", prompt="fix the parser", reply="all good")
    stores.copilot_chat(proj, "h2", prompt="deploy", reply="the parser was deployed")
    stores.copilot_chat(proj, "h3", prompt="deploy", reply="nothing")
    page = await ChatCatalog().page(workspaces(proj), words=("PARSER",))
    assert {c.native_id for c in page.chats} == {"h1", "h2"}
    both = await ChatCatalog().page(workspaces(proj), words=("parser", "deployed"))
    assert [c.native_id for c in both.chats] == ["h2"]


async def test_a_workspace_name_narrows_to_that_workspace(proj: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    stores.copilot_chat(proj, "h1")
    stores.copilot_chat(other, "h2")
    page = await ChatCatalog().page(workspaces(proj, other), workspace="other")
    assert [c.native_id for c in page.chats] == ["h2"]
    assert page.outside_workspaces == 0


async def test_the_list_is_capped_and_says_how_many_more(proj: Path) -> None:
    for i in range(LISTED + 4):
        stores.copilot_chat(proj, f"h{i:02}", seconds_old=3600 + i)
    page = await ChatCatalog().page(workspaces(proj))
    assert len(page.chats) == LISTED and page.more == 4


async def test_find_reads_the_chat_again_by_its_reference(proj: Path) -> None:
    stores.copilot_chat(proj, "h1", reply="first")
    catalog = ChatCatalog()
    chat = (await catalog.page(workspaces(proj))).chats[0]
    stores.copilot_chat(proj, "h1", reply="second")
    again = await catalog.find(chat.ref, workspaces(proj), 7)
    assert again is not None and again.last_reply == "second"
    assert await catalog.find("vscode_copilot:nope", workspaces(proj), 7) is None


async def test_find_refuses_a_chat_outside_the_workspaces(proj: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    stores.copilot_chat(other, "h2")
    assert await ChatCatalog().find("vscode_copilot:h2", workspaces(proj), 7) is None


async def test_a_store_that_fails_or_hangs_is_skipped_and_the_rest_still_list(
    proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores.copilot_chat(proj, "h1")

    def broken(since: float, roots: object, now: float) -> list[DeskChat]:
        raise OSError("disk gone")

    def slow(since: float, roots: object, now: float) -> list[DeskChat]:
        time.sleep(0.4)
        return []

    monkeypatch.setattr(catalog_module, "READ_TIMEOUT_S", 0.1)
    readers: tuple[tuple[str, Reader], ...] = (("Broken", broken), ("Slow", slow), ("Copilot", copilot.read))
    page = await ChatCatalog(readers).page(workspaces(proj))
    assert [c.native_id for c in page.chats] == ["h1"]


def test_ago_reads_naturally() -> None:
    now = datetime.now(UTC)
    assert ago(now - timedelta(seconds=30), now) == "just now"
    assert ago(now - timedelta(minutes=12), now) == "12 min ago"
    assert ago(now - timedelta(hours=5), now) == "5 h ago"
    assert ago(now - timedelta(days=3), now) == "3 d ago"


def test_every_app_word_names_known_harnesses() -> None:
    assert all(h in set(Harness) for group in APP_WORDS.values() for h in group)
