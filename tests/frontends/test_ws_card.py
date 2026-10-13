"""The `/ws` card: menu, add a folder, clone a repo, add a repo to a workspace, hand-off to `/new`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay.core import workspaces as ws
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.cards import ws as ws_cards
from agent_relay.models.config import Workspace
from agent_relay.settings import load_workspaces, replace_workspace
from tests.fakes.git_remote import make_remote
from tests.frontends.conftest import Relay, submit_data


def make_root(relay: Relay, tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "code"
    for name in names:
        (root / name).mkdir(parents=True)
    root.mkdir(exist_ok=True)
    relay.config.ws.roots = [str(root)]
    return root


def titles(card: dict[str, JsonValue]) -> list[str]:
    actions = card["actions"]
    assert isinstance(actions, list)
    return [str(a["title"]) for a in actions if isinstance(a, dict)]


def body_blocks(card: dict[str, JsonValue], kind: str) -> list[dict[str, JsonValue]]:
    body = card["body"]
    assert isinstance(body, list)
    return [b for b in body if isinstance(b, dict) and b["type"] == kind]


def choice_values(card: dict[str, JsonValue]) -> list[str]:
    [chooser] = [b for b in body_blocks(card, "Input.ChoiceSet") if b["id"] == "folder"]
    choices = chooser["choices"]
    assert isinstance(choices, list)
    return [str(c["value"]) for c in choices if isinstance(c, dict)]


def allow_file_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    real = ws.parse_clone_url

    def parse(raw: object, hosts: list[str]) -> ws.CloneSpec:
        url = str(raw)
        if not url.startswith("file://"):
            return real(raw, hosts)
        return ws.CloneSpec(url, "local", "tests", Path(url).name.removesuffix(".git"))

    monkeypatch.setattr(ws, "parse_clone_url", parse)


async def test_without_roots_ws_lists_and_says_how_to_enable_it(relay: Relay) -> None:
    await relay.say("/ws")
    text = relay.bot_texts()[-1]
    assert "**proj**" in text
    assert "relay config set ws.roots '[\"<folder holding your repos>\"]'" in text
    assert not relay.webex.bot_messages()[-1].attachments


async def test_with_roots_ws_sends_a_card_with_three_actions_each_with_its_own_nonce(
    relay: Relay, tmp_path: Path
) -> None:
    make_root(relay, tmp_path, "a")
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    assert "**proj**" in msg.markdown
    assert titles(card) == ["Add folder", "Clone repo", "Add repo to workspace"]
    nonces = {submit_data(card, t)["nonce"] for t in titles(card)}
    assert len(nonces) == 3
    assert {submit_data(card, t)["relay"] for t in titles(card)} == {"ws"}


async def test_ws_in_a_thread_only_lists(relay: Relay, tmp_path: Path) -> None:
    make_root(relay, tmp_path, "a")
    await relay.say('/new opencode proj m1 "start"')
    session = (await relay.service.sessions())[0]
    before = len(relay.webex.bot_messages())
    await relay.say("/ws", parent_id=session.webex_thread_id)
    last = relay.webex.bot_messages()[-1]
    assert len(relay.webex.bot_messages()) == before + 1
    assert "**proj**" in last.markdown and not last.attachments


async def test_add_folder_from_the_card_registers_it_and_offers_to_start_a_session(
    relay: Relay, tmp_path: Path
) -> None:
    root = make_root(relay, tmp_path, "alpha", "beta", ".hidden")
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Add folder")
    msg, card = relay.webex.last_card()
    assert sorted(Path(v).name for v in choice_values(card)) == ["alpha", "beta"]
    assert [b["id"] for b in body_blocks(card, "Input.Text")] == ["name"]
    await relay.press(msg.id, card, "Add", folder=str((root / "alpha").resolve()), name="")
    assert load_workspaces().workspaces[-1] == Workspace(name="alpha", path=str((root / "alpha").resolve()))
    msg, done = relay.webex.last_card()
    assert 'Or type `/new <app> alpha "…"`.' in msg.markdown
    assert "alpha" in msg.markdown
    start = submit_data(done, "Start a session here")
    assert {k: v for k, v in start.items() if k != "nonce"} == {
        "relay": "new",
        "step": "ws",
        "value": "alpha",
    }


async def test_the_start_button_goes_straight_into_the_new_wizard_with_the_workspace_chosen(
    relay: Relay, tmp_path: Path
) -> None:
    root = make_root(relay, tmp_path, "alpha")
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Add folder")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Add", folder=str((root / "alpha").resolve()), name="mine")
    msg, done = relay.webex.last_card()
    await relay.press(msg.id, done, "Start a session here")
    msg, card = relay.webex.last_card()
    assert cards.card_fallback(card) == "New session"
    [chooser] = [b for b in body_blocks(card, "Input.ChoiceSet") if b["id"] == "ws"]
    assert chooser["value"] == "mine"


async def test_add_folder_refuses_a_typed_path_and_the_card_still_works_afterwards(
    relay: Relay, tmp_path: Path
) -> None:
    root = make_root(relay, tmp_path, "alpha")
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Add folder")
    msg, card = relay.webex.last_card()
    for bad in ("/etc", str(tmp_path), "alpha", str(root / ".git")):
        await relay.press(msg.id, card, "Add", folder=bad)
        assert relay.bot_texts()[-1].startswith("⚠️"), bad
    assert [w.name for w in load_workspaces().workspaces] == ["proj"]
    await relay.press(msg.id, card, "Add", folder=str((root / "alpha").resolve()))
    assert [w.name for w in load_workspaces().workspaces] == ["proj", "alpha"]


async def test_add_folder_validates_the_name(relay: Relay, tmp_path: Path) -> None:
    root = make_root(relay, tmp_path, "alpha")
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Add folder")
    msg, card = relay.webex.last_card()
    folder = str((root / "alpha").resolve())
    for bad in ("two words", "x" * 41, "../up", "proj"):
        await relay.press(msg.id, card, "Add", folder=folder, name=bad)
        assert relay.bot_texts()[-1].startswith("⚠️"), bad
    assert [w.name for w in load_workspaces().workspaces] == ["proj"]


async def test_a_card_cannot_be_submitted_twice(relay: Relay, tmp_path: Path) -> None:
    root = make_root(relay, tmp_path, "alpha")
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Add folder")
    msg, card = relay.webex.last_card()
    folder = str((root / "alpha").resolve())
    await relay.press(msg.id, card, "Add", folder=folder)
    await relay.press(msg.id, card, "Add", folder=folder)
    assert relay.bot_texts()[-1] == "This card has expired or was already answered."
    assert [w.name for w in load_workspaces().workspaces] == ["proj", "alpha"]


async def test_tapping_one_menu_button_does_not_spend_the_others(relay: Relay, tmp_path: Path) -> None:
    make_root(relay, tmp_path, "alpha")
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Add folder")
    await relay.press(msg.id, card, "Clone repo")
    assert cards.card_fallback(relay.webex.last_card()[1]) == "Clone a repo"


async def test_add_folder_with_nothing_to_offer_says_so(relay: Relay, tmp_path: Path) -> None:
    make_root(relay, tmp_path)
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Add folder")
    assert "No folders to offer" in relay.bot_texts()[-1]


async def test_unknown_ops_are_refused(relay: Relay, tmp_path: Path) -> None:
    make_root(relay, tmp_path, "alpha")
    nonce = await relay.nonce()
    await relay.say("hi")
    msg = relay.webex.bot_messages()[-1]
    await relay.click(msg.id, {"relay": "ws", "op": "shell", "nonce": nonce})
    assert "not an action this card offers" in relay.bot_texts()[-1]


async def test_actions_refuse_when_ws_roots_is_removed_meanwhile(relay: Relay, tmp_path: Path) -> None:
    make_root(relay, tmp_path, "alpha")
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    relay.config.ws.roots = []
    await relay.press(msg.id, card, "Clone repo")
    assert "relay config set ws.roots" in relay.bot_texts()[-1]
    await relay.press(msg.id, card, "Add folder")
    assert "relay config set ws.roots" in relay.bot_texts()[-1]


# clone


async def _clone_form(relay: Relay) -> tuple[str, dict[str, JsonValue]]:
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Clone repo")
    msg, card = relay.webex.last_card()
    return msg.id, card


async def test_the_clone_form_asks_for_url_name_and_root(relay: Relay, tmp_path: Path) -> None:
    root = make_root(relay, tmp_path)
    _, card = await _clone_form(relay)
    assert [b["id"] for b in body_blocks(card, "Input.Text")] == ["url", "name"]
    [roots] = body_blocks(card, "Input.ChoiceSet")
    assert roots["id"] == "root"
    choices = roots["choices"]
    assert isinstance(choices, list) and len(choices) == 1
    [only] = choices
    assert isinstance(only, dict) and only["value"] == str(root.resolve())
    assert str(root.resolve()).startswith(str(only["title"]))


async def test_clone_from_the_card_checks_out_registers_and_offers_to_start(
    relay: Relay, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_file_urls(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = make_root(relay, tmp_path)
    url = make_remote(tmp_path, "widgets")
    msg_id, card = await _clone_form(relay)
    await relay.press(msg_id, card, "Clone", url=url, name="", root=str(root.resolve()))
    texts = relay.bot_texts()
    assert any("Cloning `tests/widgets`" in t for t in texts)
    assert (root / "widgets" / "README.md").exists()
    assert load_workspaces().workspaces[-1] == Workspace(
        name="widgets", path=str((root / "widgets").resolve())
    )
    msg, done = relay.webex.last_card()
    assert "Cloned and added workspace **widgets**" in msg.markdown
    assert submit_data(done, "Start a session here")["value"] == "widgets"


async def test_clone_refuses_a_bad_url_host_root_and_name_without_running_git(
    relay: Relay, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_root(relay, tmp_path)

    async def never(*args: object) -> None:
        raise AssertionError("git must not run")

    monkeypatch.setattr(ws, "clone_repository", never)
    msg_id, card = await _clone_form(relay)
    good = str(root.resolve())
    attempts: list[dict[str, JsonValue]] = [
        {"url": "https://evil.example/acme/x", "root": good},
        {"url": "https://github.com/acme/x/../y", "root": good},
        {"url": "ext::sh -c id", "root": good},
        {"url": "https://github.com/acme/x", "root": str(tmp_path)},
        {"url": "https://github.com/acme/x", "root": ""},
        {"url": "https://github.com/acme/x", "root": good, "name": "bad name"},
        {"url": "x" * 400, "root": good},
    ]
    for inputs in attempts:
        await relay.press(msg_id, card, "Clone", **inputs)
        assert relay.bot_texts()[-1].startswith("⚠️"), inputs
    assert [w.name for w in load_workspaces().workspaces] == ["proj"]
    assert not (root / "x").exists()


async def test_a_failed_clone_reports_git_with_the_launchd_hint_and_can_be_retried(
    relay: Relay, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_file_urls(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = make_root(relay, tmp_path)
    msg_id, card = await _clone_form(relay)
    await relay.press(msg_id, card, "Clone", url=(tmp_path / "gone.git").as_uri(), root=str(root.resolve()))
    failure = relay.bot_texts()[-1]
    assert failure.startswith("⚠️ git clone failed:") and "launchd" in failure
    assert not (root / "gone").exists() and [w.name for w in load_workspaces().workspaces] == ["proj"]
    url = make_remote(tmp_path, "gone")
    await relay.press(msg_id, card, "Clone", url=url, root=str(root.resolve()))
    assert [w.name for w in load_workspaces().workspaces] == ["proj", "gone"]


async def test_clone_into_an_existing_folder_is_refused(relay: Relay, tmp_path: Path) -> None:
    root = make_root(relay, tmp_path, "widgets")
    msg_id, card = await _clone_form(relay)
    await relay.press(msg_id, card, "Clone", url="https://github.com/acme/widgets", root=str(root.resolve()))
    assert "already exists" in relay.bot_texts()[-1]


# add a repo to a workspace


async def _repo_picker(relay: Relay) -> tuple[str, dict[str, JsonValue]]:
    await relay.say("/ws")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Add repo to workspace")
    msg, card = relay.webex.last_card()
    return msg.id, card


async def test_add_a_folder_to_a_plain_workspace_creates_the_code_workspace_file(
    relay: Relay, tmp_path: Path
) -> None:
    root = make_root(relay, tmp_path, "extra")
    plain = root / "app"
    plain.mkdir()
    await relay.service.ws_add_folder(str(plain), "app")
    msg_id, card = await _repo_picker(relay)
    [chooser] = body_blocks(card, "Input.ChoiceSet")
    assert chooser["id"] == "workspace"
    assert titles(card) == ["Pick a folder", "Clone a repo"]
    await relay.press(msg_id, card, "Pick a folder", workspace="app")
    msg, folder_card = relay.webex.last_card()
    assert sorted(Path(v).name for v in choice_values(folder_card)) == ["extra"]
    assert submit_data(folder_card, "Add to workspace")["workspace"] == "app"
    await relay.press(msg.id, folder_card, "Add to workspace", folder=str((root / "extra").resolve()))
    file = root / "app.code-workspace"
    assert json.loads(file.read_text())["folders"] == [{"path": "app"}, {"path": "extra"}]
    [entry] = [w for w in load_workspaces().workspaces if w.name == "app"]
    assert entry.file == str(file.resolve()) and entry.path == str(plain.resolve())
    done_msg, done = relay.webex.last_card()
    assert submit_data(done, "Start a session here")["value"] == "app"
    assert "app" in done_msg.markdown


async def test_add_a_folder_to_a_code_workspace_keeps_its_other_keys(relay: Relay, tmp_path: Path) -> None:
    root = make_root(relay, tmp_path, "one", "two")
    file = root / "team.code-workspace"
    file.write_text('{"folders": [{"path": "one"}], "settings": {"a": 1}}')
    await relay.service.ws_add_folder(str(root / "one"), "team")
    entry = load_workspaces().workspaces[-1]
    replace_workspace(Workspace(name="team", path=entry.path, file=str(file.resolve())))
    msg_id, card = await _repo_picker(relay)
    await relay.press(msg_id, card, "Pick a folder", workspace="team")
    msg, folder_card = relay.webex.last_card()
    await relay.press(msg.id, folder_card, "Add to workspace", folder=str((root / "two").resolve()))
    data = json.loads(file.read_text())
    assert data["settings"] == {"a": 1}
    assert data["folders"] == [{"path": "one"}, {"path": "two"}]


async def test_add_repo_refuses_an_unknown_workspace_and_a_typed_folder(relay: Relay, tmp_path: Path) -> None:
    root = make_root(relay, tmp_path, "extra")
    msg_id, card = await _repo_picker(relay)
    await relay.press(msg_id, card, "Pick a folder", workspace="nope")
    assert "Unknown workspace `nope`" in relay.bot_texts()[-1]
    await relay.press(msg_id, card, "Pick a folder", workspace="proj")
    msg, folder_card = relay.webex.last_card()
    for bad in ("/etc", str(tmp_path), "extra"):
        await relay.press(msg.id, folder_card, "Add to workspace", folder=bad)
        assert relay.bot_texts()[-1].startswith("⚠️"), bad
    assert not list(root.glob("*.code-workspace"))


async def test_add_a_cloned_repo_to_a_workspace(
    relay: Relay, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_file_urls(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = make_root(relay, tmp_path)
    plain = root / "app"
    plain.mkdir()
    await relay.service.ws_add_folder(str(plain), "app")
    msg_id, card = await _repo_picker(relay)
    await relay.press(msg_id, card, "Clone a repo", workspace="app")
    msg, form = relay.webex.last_card()
    assert submit_data(form, "Clone")["workspace"] == "app"
    await relay.press(msg.id, form, "Clone", url=make_remote(tmp_path, "lib"), root=str(root.resolve()))
    assert (root / "lib" / "README.md").exists()
    assert json.loads((root / "app.code-workspace").read_text())["folders"] == [
        {"path": "app"},
        {"path": "lib"},
    ]
    assert [w.name for w in load_workspaces().workspaces] == ["proj", "app"]
    msg, done = relay.webex.last_card()
    assert "Cloned the repo into workspace **app**" in msg.markdown
    assert submit_data(done, "Start a session here")["value"] == "app"


# card builders


def test_a_folder_card_says_when_it_shows_only_some_of_the_folders(tmp_path: Path) -> None:
    choices = tuple(ws.FolderChoice(tmp_path / f"d{i}", f"d{i}", False) for i in range(25))
    card = ws_cards.pick_folder(
        ws.FolderOffer(choices, 40), title="t", op=ws_cards.OP_ADD, button="Add", nonce="n"
    )
    notes = [str(b["text"]) for b in body_blocks(card, "TextBlock") if "Showing" in str(b["text"])]
    assert notes == ["Showing 25 of 40, git repos first."]
    full = ws_cards.pick_folder(
        ws.FolderOffer(choices, 25), title="t", op=ws_cards.OP_ADD, button="Add", nonce="n"
    )
    assert not [b for b in body_blocks(full, "TextBlock") if "Showing" in str(b["text"])]


def test_the_cards_fit_in_a_webex_message(tmp_path: Path) -> None:
    choices = tuple(ws.FolderChoice(tmp_path / ("x" * 120 + str(i)), "y" * 300, True) for i in range(25))
    card = ws_cards.pick_folder(
        ws.FolderOffer(choices, 80), title="t", op=ws_cards.OP_ADD, button="Add", nonce="n", ask_name=True
    )
    assert cards.fit_card("Pick a folder", card) is not None
