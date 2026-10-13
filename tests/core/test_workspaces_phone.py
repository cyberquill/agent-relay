"""Workspaces from the phone: folder rules, clone URL rules, `git clone`, `.code-workspace` edits, service."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from agent_relay.core import workspaces as ws
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayError, RelayService
from agent_relay.core.signals import Signals
from agent_relay.models.config import RelayConfig, Workspace
from agent_relay.settings import load_workspaces, register_workspace
from tests.fakes.git_remote import make_remote

GITHUB = ["github.com"]


def dirs(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir(parents=True)


def fake_git(bin_dir: Path, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "git"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


# clone URLs


@pytest.mark.parametrize(
    ("url", "owner", "repo"),
    [
        ("https://github.com/acme/widgets", "acme", "widgets"),
        ("https://github.com/acme/widgets.git", "acme", "widgets"),
        ("  https://GitHub.com/acme/my.repo_1-x.git  ", "acme", "my.repo_1-x"),
        ("git@github.com:acme/widgets", "acme", "widgets"),
        ("git@github.com:acme/widgets.git", "acme", "widgets"),
    ],
)
def test_clone_urls_in_the_two_allowed_shapes_parse(url: str, owner: str, repo: str) -> None:
    spec = ws.parse_clone_url(url, GITHUB)
    assert (spec.host, spec.owner, spec.repo) == ("github.com", owner, repo)
    assert spec.url == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "github.com/acme/widgets",
        "http://github.com/acme/widgets",
        "file:///tmp/x.git",
        "ssh://git@github.com/acme/widgets",
        "ext::sh -c touch /tmp/pwned",
        "--upload-pack=touch /tmp/x",
        "https://user:pw@github.com/acme/widgets",
        "https://github.com/acme",
        "https://github.com/acme/widgets/extra",
        "https://github.com/acme/widgets/",
        "https://github.com/acme/widgets?x=1",
        "https://github.com/acme/widgets git",
        "https://github.com/acme/widgets\nhttps://github.com/a/b",
        "https://github.com/../widgets",
        "https://github.com/acme/..",
        "https://github.com/acme/a..b",
        "https://github.com/./widgets",
        "https://github.com/acme/.git",
        "git@github.com:acme/..",
        "git@github.com:acme",
        "git@github.com:/acme/widgets",
        "https://github.com:443/acme/widgets",
    ],
)
def test_other_urls_are_refused(url: str) -> None:
    with pytest.raises(ws.WorkspaceError):
        ws.parse_clone_url(url, GITHUB)


def test_a_host_outside_the_allow_list_is_refused_and_the_list_is_named() -> None:
    with pytest.raises(ws.WorkspaceError, match=r"gitlab\.com.*Allowed hosts: github\.com"):
        ws.parse_clone_url("https://gitlab.com/acme/widgets", GITHUB)
    assert ws.parse_clone_url("https://git.example.com/a/b", ["github.com", "Git.Example.com"]).host == (
        "git.example.com"
    )


def test_a_non_string_url_is_refused() -> None:
    with pytest.raises(ws.WorkspaceError):
        ws.parse_clone_url(None, GITHUB)


# names


def test_names_follow_the_pattern_and_must_be_unique() -> None:
    assert ws.validate_name("my-repo_1.x", ["other"]) == "my-repo_1.x"
    for bad in ("", "a b", "a/b", "x" * 41, "é"):
        with pytest.raises(ws.WorkspaceError, match="1 to 40"):
            ws.validate_name(bad, [])
    with pytest.raises(ws.WorkspaceError, match="exists"):
        ws.validate_name("taken", ["taken"])


# folders under the roots


def test_folders_skip_hidden_registered_files_and_symlinks_out_of_the_root(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    dirs(root, "a", "b", ".hidden", "c/.git")
    dirs(tmp_path, "outside")
    (root / "file.txt").write_text("x")
    (root / "escape").symlink_to(outside)
    (root / "within").symlink_to(root / "a")
    offer = ws.list_folders([root.resolve()], 2, exclude=[root / "b"])
    assert sorted(c.path.name for c in offer.choices) == ["a", "c"]
    assert offer.total == 2


def test_folders_go_down_to_the_depth_and_a_git_repo_is_a_leaf(tmp_path: Path) -> None:
    root = tmp_path / "root"
    dirs(root, "g/one/two", "r/.git", "r/inner")

    def names(depth: int) -> list[str]:
        offer = ws.list_folders([root.resolve()], depth)
        return sorted(str(c.path.relative_to(root.resolve())) for c in offer.choices)

    assert names(1) == ["g", "r"]
    assert names(2) == ["g", "g/one", "r"]
    assert names(3) == ["g", "g/one", "g/one/two", "r"]


def test_git_repos_come_first_then_the_most_recently_changed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    dirs(root, "old", "new", "repo/.git")
    now = time.time()
    for name, age in (("old", 5000), ("new", 10), ("repo", 9000)):
        os.utime(root / name, (now - age, now - age))
    offer = ws.list_folders([root.resolve()], 1)
    assert [c.path.name for c in offer.choices] == ["repo", "new", "old"]
    assert [c.git for c in offer.choices] == [True, False, False]
    assert offer.choices[0].label.endswith("(git)")


def test_at_most_25_folders_are_offered_and_the_total_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "root"
    dirs(root, *(f"d{i:02d}" for i in range(30)))
    offer = ws.list_folders([root.resolve()], 1)
    assert len(offer.choices) == ws.MAX_FOLDERS == 25
    assert offer.total == 30


def test_roots_that_are_missing_or_repeated_are_dropped(tmp_path: Path) -> None:
    dirs(tmp_path, "real")
    found = ws.existing_roots([str(tmp_path / "real"), str(tmp_path / "gone"), str(tmp_path / "real")])
    assert found == [(tmp_path / "real").resolve()]


def test_check_folder_accepts_only_what_the_picker_could_offer(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    dirs(root, "a/b/c", ".hidden/x", "taken")
    dirs(tmp_path, "outside")
    (root / "escape").symlink_to(outside)
    roots = [root.resolve()]
    assert ws.check_folder(str(root / "a"), roots, 2) == (root / "a").resolve()
    assert ws.check_folder(str(root / "a" / "b"), roots, 2) == (root / "a" / "b").resolve()
    refused = [
        "a",
        "",
        None,
        42,
        str(root / "a" / "b" / "c"),
        str(root / ".hidden"),
        str(root / ".hidden" / "x"),
        str(root / "escape"),
        str(root / "a" / ".." / ".." / "outside"),
        str(root),
        str(tmp_path),
        "/etc",
        str(root / "missing"),
    ]
    for raw in refused:
        with pytest.raises(ws.WorkspaceError):
            ws.check_folder(raw, roots, 2)
    with pytest.raises(ws.WorkspaceError, match="already registered"):
        ws.check_folder(str(root / "taken"), roots, 2, exclude=[root / "taken"])


def test_check_root_accepts_only_configured_roots(tmp_path: Path) -> None:
    dirs(tmp_path, "root", "other")
    roots = [(tmp_path / "root").resolve()]
    assert ws.check_root(str(tmp_path / "root"), roots) == roots[0]
    for raw in (str(tmp_path / "other"), "", None, str(tmp_path / "root" / "..")):
        with pytest.raises(ws.WorkspaceError):
            ws.check_root(raw, roots)


def test_a_clone_target_must_be_new_and_directly_under_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    dirs(root, "taken")
    assert ws.clone_destination(root.resolve(), "fresh") == root.resolve() / "fresh"
    with pytest.raises(ws.WorkspaceError, match="already exists"):
        ws.clone_destination(root.resolve(), "taken")
    (root / "dangling").symlink_to(tmp_path / "nowhere")
    with pytest.raises(ws.WorkspaceError, match="already exists"):
        ws.clone_destination(root.resolve(), "dangling")
    with pytest.raises(ws.WorkspaceError, match="not allowed"):
        ws.clone_destination(root.resolve(), "../escape")


# git clone


def test_git_sees_only_the_allow_listed_environment() -> None:
    env = ws.git_env({"HOME": "/h", "PATH": "/p", "SSH_AUTH_SOCK": "/s", "GITHUB_TOKEN": "t", "EDITOR": "vi"})
    assert env == {"HOME": "/h", "PATH": "/p", "SSH_AUTH_SOCK": "/s", "GIT_TERMINAL_PROMPT": "0"}
    assert ws.git_env({"HOME": "/h", "PATH": "/p"}) == {
        "HOME": "/h",
        "PATH": "/p",
        "GIT_TERMINAL_PROMPT": "0",
    }


async def test_clone_runs_git_without_a_shell_and_with_the_allow_listed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = tmp_path / "seen.txt"
    bin_dir = tmp_path / "bin"
    fake_git(
        bin_dir, f'for a in "$@"; do echo "arg:$a" >> {seen}; done; /usr/bin/env >> {seen}; /bin/mkdir "$4"'
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("GITHUB_TOKEN", "should-not-leak")
    monkeypatch.setenv("RELAY_OTHER", "nor-this")
    target = tmp_path / "root" / "repo"
    target.parent.mkdir()
    await ws.clone_repository("https://github.com/acme/repo", target, 30)
    lines = seen.read_text().splitlines()
    assert lines[:4] == ["arg:clone", "arg:--", "arg:https://github.com/acme/repo", f"arg:{target}"]
    env = dict(line.split("=", 1) for line in lines[4:] if "=" in line)
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    assert env["HOME"] == str(tmp_path)
    assert "GITHUB_TOKEN" not in env and "RELAY_OTHER" not in env
    assert target.is_dir()


async def test_clone_of_a_local_repository_checks_it_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    url = make_remote(tmp_path)
    target = tmp_path / "checkout"
    await ws.clone_repository(url, target, 60)
    assert (target / "README.md").read_text() == "hello\n"


async def test_a_failed_clone_leaves_nothing_and_says_what_git_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "checkout"
    with pytest.raises(ws.CloneError) as caught:
        await ws.clone_repository((tmp_path / "nothing.git").as_uri(), target, 60)
    message = str(caught.value)
    assert message.startswith("git clone failed:") and "nothing.git" in message
    assert "launchd" in message and "https://" in message
    assert not target.exists()


async def test_a_failure_removes_a_partial_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    fake_git(bin_dir, '/bin/mkdir -p "$4/.git"; echo "fatal: early EOF" >&2; exit 128')
    monkeypatch.setenv("PATH", str(bin_dir))
    target = tmp_path / "checkout"
    with pytest.raises(ws.CloneError, match="early EOF"):
        await ws.clone_repository("https://github.com/acme/repo", target, 30)
    assert not target.exists()


async def test_a_clone_that_takes_too_long_is_killed_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    fake_git(bin_dir, '/bin/mkdir -p "$4"; /bin/sleep 30')
    monkeypatch.setenv("PATH", str(bin_dir))
    target = tmp_path / "checkout"
    started = time.monotonic()
    with pytest.raises(ws.CloneError, match="longer than"):
        await ws.clone_repository("https://github.com/acme/repo", target, 0.5)
    assert time.monotonic() - started < 10
    assert not target.exists()


async def test_a_missing_git_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ws.CloneError, match="not installed"):
        await ws.clone_repository("https://github.com/acme/repo", tmp_path / "x", 30)


def test_git_output_is_scrubbed_and_cut_to_the_last_lines() -> None:
    noisy = "\n".join(f"line {i}" for i in range(20)) + "\nfatal: https://me:hunter2@example.com/x denied\n"
    tail = ws._tail(noisy.encode())
    assert "hunter2" not in tail and "<redacted>" in tail
    assert tail.splitlines()[0] == "line 16" and len(tail.splitlines()) == ws.ERROR_LINES
    assert len(ws._tail(b"x" * 5000)) <= ws.ERROR_CHARS


# attaching a folder to a workspace


def test_a_folder_joins_an_existing_code_workspace_and_other_keys_stay(tmp_path: Path) -> None:
    root = tmp_path / "root"
    dirs(root, "a", "b", "team/c")
    file = root / "team.code-workspace"
    file.write_text('{"folders": [{"path": "a"}], "settings": {"editor.tabSize": 2}, // note\n}')
    file.chmod(0o640)
    entry = Workspace(name="team", path=str((root / "a").resolve()), file=str(file.resolve()))
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    updated = ws.attach_folder(entry, root / "b", [root.resolve()])
    updated = ws.attach_folder(updated, outside, [root.resolve()])
    assert updated == entry
    data = json.loads(file.read_text())
    assert data["settings"] == {"editor.tabSize": 2}
    assert data["folders"] == [{"path": "a"}, {"path": "b"}, {"path": str(outside.resolve())}]
    assert stat.S_IMODE(file.stat().st_mode) == 0o640
    assert not list(root.glob(".*.relay-tmp"))


def test_a_plain_workspace_gets_a_code_workspace_file_with_both_folders(tmp_path: Path) -> None:
    root = tmp_path / "root"
    dirs(root, "proj", "extra")
    entry = Workspace(name="proj", path=str((root / "proj").resolve()))
    updated = ws.attach_folder(entry, root / "extra", [root.resolve()])
    file = root / "proj.code-workspace"
    assert updated == Workspace(name="proj", path=entry.path, file=str(file.resolve()))
    assert json.loads(file.read_text()) == {"folders": [{"path": "proj"}, {"path": "extra"}]}
    assert ws.workspace_entry("proj", str(file)) == updated


def test_a_workspace_outside_every_root_keeps_its_file_next_to_its_folder(tmp_path: Path) -> None:
    root, home = tmp_path / "root", tmp_path / "elsewhere"
    dirs(root, "extra")
    dirs(home, "proj")
    entry = Workspace(name="proj", path=str((home / "proj").resolve()))
    updated = ws.attach_folder(entry, root / "extra", [root.resolve()])
    assert updated.file == str((home / "proj.code-workspace").resolve())
    assert json.loads((home / "proj.code-workspace").read_text())["folders"][1] == {
        "path": str((root / "extra").resolve())
    }


def test_attaching_refuses_duplicates_a_taken_file_name_and_unsafe_names(tmp_path: Path) -> None:
    root = tmp_path / "root"
    dirs(root, "proj", "extra")
    roots = [root.resolve()]
    entry = Workspace(name="proj", path=str((root / "proj").resolve()))
    with pytest.raises(ws.WorkspaceError, match="already has"):
        ws.attach_folder(entry, root / "proj", roots)
    (root / "proj.code-workspace").write_text("{}")
    with pytest.raises(ws.WorkspaceError, match="already exists"):
        ws.attach_folder(entry, root / "extra", roots)
    with pytest.raises(ws.WorkspaceError, match="cannot name"):
        ws.attach_folder(Workspace(name="a b", path=entry.path), root / "extra", roots)
    broken = Workspace(name="b", path=entry.path, file=str(root / "nope.code-workspace"))
    with pytest.raises(ws.WorkspaceError, match="cannot read"):
        ws.attach_folder(broken, root / "extra", roots)
    (root / "list.code-workspace").write_text("[1]")
    with pytest.raises(ws.WorkspaceError, match="not a workspace file"):
        ws.attach_folder(
            Workspace(name="l", path=entry.path, file=str(root / "list.code-workspace")),
            root / "extra",
            roots,
        )


# the service


@pytest.fixture
def service(registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RelayService:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = RelayConfig()
    config.ws.roots = [str(tmp_path / "root")]
    dirs(tmp_path, "root")
    return RelayService(registry, Router(), Signals(), config)


def allow_file_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests clone from a local bare repo, so they swap the strict parser for one that takes file:// URLs."""

    def parse(raw: object, hosts: list[str]) -> ws.CloneSpec:
        url = str(raw)
        if not url.startswith("file://"):
            return real(raw, hosts)
        return ws.CloneSpec(url, "local", "tests", Path(url).name.removesuffix(".git"))

    real = ws.parse_clone_url
    monkeypatch.setattr(ws, "parse_clone_url", parse)


async def test_the_folder_picker_is_off_until_roots_are_set(registry: Registry) -> None:
    service = RelayService(registry, Router(), Signals(), RelayConfig())
    assert service.ws_roots() == []
    with pytest.raises(RelayError, match=r"relay config set ws\.roots"):
        await service.ws_folders()


async def test_adding_a_folder_registers_it_under_its_name_or_a_given_one(
    service: RelayService, tmp_path: Path
) -> None:
    dirs(tmp_path / "root", "alpha", "beta")
    first = await service.ws_add_folder(str(tmp_path / "root" / "alpha"))
    second = await service.ws_add_folder(str(tmp_path / "root" / "beta"), "other")
    assert [(w.name, w.path) for w in load_workspaces().workspaces] == [
        ("alpha", str((tmp_path / "root" / "alpha").resolve())),
        ("other", str((tmp_path / "root" / "beta").resolve())),
    ]
    assert (first.name, second.name) == ("alpha", "other")
    offer = await service.ws_folders()
    assert offer.choices == ()


async def test_a_registered_folder_is_no_longer_offered_and_cannot_be_added_again(
    service: RelayService, tmp_path: Path
) -> None:
    dirs(tmp_path / "root", "alpha", "beta")
    await service.ws_add_folder(str(tmp_path / "root" / "alpha"))
    offer = await service.ws_folders()
    assert [c.path.name for c in offer.choices] == ["beta"]
    with pytest.raises(RelayError, match="already registered"):
        await service.ws_add_folder(str(tmp_path / "root" / "alpha"), "again")


async def test_adding_refuses_typed_paths_bad_names_and_taken_names(
    service: RelayService, tmp_path: Path
) -> None:
    dirs(tmp_path / "root", "alpha", "beta", ".hidden")
    outside = tmp_path / "outside"
    outside.mkdir()
    for raw in (str(outside), "/etc", "alpha", str(tmp_path / "root" / ".hidden"), None):
        with pytest.raises(RelayError):
            await service.ws_add_folder(raw)
    with pytest.raises(RelayError, match="1 to 40"):
        await service.ws_add_folder(str(tmp_path / "root" / "alpha"), "bad name")
    await service.ws_add_folder(str(tmp_path / "root" / "alpha"), "taken")
    with pytest.raises(RelayError, match="exists"):
        await service.ws_add_folder(str(tmp_path / "root" / "beta"), "taken")
    assert [w.name for w in load_workspaces().workspaces] == ["taken"]


async def test_a_folder_with_a_name_that_cannot_be_a_workspace_name_needs_one_typed(
    service: RelayService, tmp_path: Path
) -> None:
    dirs(tmp_path / "root", "my repo")
    with pytest.raises(RelayError, match="1 to 40"):
        await service.ws_add_folder(str(tmp_path / "root" / "my repo"))
    assert (await service.ws_add_folder(str(tmp_path / "root" / "my repo"), "my-repo")).name == "my-repo"


async def test_clone_checks_out_and_registers_the_repo(
    service: RelayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_file_urls(monkeypatch)
    url = make_remote(tmp_path, "widgets")
    told: list[str] = []

    async def started(repo: str) -> None:
        told.append(repo)

    entry = await service.ws_clone(url, None, str(tmp_path / "root"), started)
    target = (tmp_path / "root" / "widgets").resolve()
    assert (target / "README.md").exists()
    assert entry == Workspace(name="widgets", path=str(target))
    assert load_workspaces().workspaces == [entry]
    assert told == ["tests/widgets"]


async def test_clone_takes_a_name_and_refuses_a_taken_one_before_cloning(
    service: RelayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_file_urls(monkeypatch)
    url = make_remote(tmp_path, "widgets")
    register_workspace(Workspace(name="taken", path=str(tmp_path)))
    with pytest.raises(RelayError, match="exists"):
        await service.ws_clone(url, "taken", str(tmp_path / "root"))
    assert not (tmp_path / "root" / "widgets").exists()
    assert (await service.ws_clone(url, "mine", str(tmp_path / "root"))).name == "mine"


async def test_clone_refuses_an_existing_target_a_stray_root_and_a_strict_url(
    service: RelayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirs(tmp_path / "root", "widgets")
    root = str(tmp_path / "root")
    with pytest.raises(RelayError, match="already exists"):
        await service.ws_clone("https://github.com/acme/widgets", None, root)
    with pytest.raises(RelayError, match="not one of the configured roots"):
        await service.ws_clone("https://github.com/acme/other", None, str(tmp_path))
    with pytest.raises(RelayError, match="not allowed"):
        await service.ws_clone("https://evil.example/acme/other", None, root)

    async def never(*args: object) -> None:
        raise AssertionError("git must not run")

    monkeypatch.setattr(ws, "clone_repository", never)
    with pytest.raises(RelayError, match="Use `https://"):
        await service.ws_clone("file:///tmp/x.git", None, root)
    assert load_workspaces().workspaces == []


async def test_a_failed_clone_registers_nothing(
    service: RelayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_file_urls(monkeypatch)
    with pytest.raises(RelayError, match="git clone failed"):
        await service.ws_clone((tmp_path / "gone.git").as_uri(), None, str(tmp_path / "root"))
    assert not (tmp_path / "root" / "gone").exists()
    assert load_workspaces().workspaces == []


async def test_a_repo_folder_joins_a_workspace_and_a_plain_one_gets_a_file(
    service: RelayService, tmp_path: Path
) -> None:
    dirs(tmp_path / "root", "proj", "extra", "more")
    await service.ws_add_folder(str(tmp_path / "root" / "proj"))
    first = await service.ws_add_repo_folder("proj", str(tmp_path / "root" / "extra"))
    file = tmp_path / "root" / "proj.code-workspace"
    assert first.file == str(file.resolve()) and first.path == str((tmp_path / "root" / "proj").resolve())
    assert load_workspaces().workspaces == [first]
    second = await service.ws_add_repo_folder("proj", str(tmp_path / "root" / "more"))
    assert second == first
    assert [f["path"] for f in json.loads(file.read_text())["folders"]] == ["proj", "extra", "more"]
    assert (await service.ws_folders("proj")).choices == ()


async def test_a_repo_folder_must_be_new_to_the_workspace_and_the_workspace_must_exist(
    service: RelayService, tmp_path: Path
) -> None:
    dirs(tmp_path / "root", "proj", "extra")
    await service.ws_add_folder(str(tmp_path / "root" / "proj"))
    with pytest.raises(RelayError, match="already"):
        await service.ws_add_repo_folder("proj", str(tmp_path / "root" / "proj"))
    with pytest.raises(RelayError, match="Unknown workspace"):
        await service.ws_add_repo_folder("nope", str(tmp_path / "root" / "extra"))
    with pytest.raises(RelayError, match="not one the list offered"):
        await service.ws_add_repo_folder("proj", "/etc")
    assert not (tmp_path / "root" / "proj.code-workspace").exists()


async def test_the_folder_picker_for_a_workspace_still_offers_other_registered_folders(
    service: RelayService, tmp_path: Path
) -> None:
    dirs(tmp_path / "root", "proj", "other")
    await service.ws_add_folder(str(tmp_path / "root" / "proj"))
    await service.ws_add_folder(str(tmp_path / "root" / "other"))
    assert [c.path.name for c in (await service.ws_folders("proj")).choices] == ["other"]
    assert (await service.ws_folders()).choices == ()


async def test_a_cloned_repo_is_attached_to_the_workspace(
    service: RelayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_file_urls(monkeypatch)
    dirs(tmp_path / "root", "proj")
    await service.ws_add_folder(str(tmp_path / "root" / "proj"))
    entry = await service.ws_add_repo_clone("proj", make_remote(tmp_path, "lib"), str(tmp_path / "root"))
    assert (tmp_path / "root" / "lib" / "README.md").exists()
    folders = json.loads((tmp_path / "root" / "proj.code-workspace").read_text())["folders"]
    assert [f["path"] for f in folders] == ["proj", "lib"]
    assert [w.name for w in load_workspaces().workspaces] == ["proj"]
    assert entry.file == str((tmp_path / "root" / "proj.code-workspace").resolve())


async def test_a_workspace_that_cannot_take_the_repo_is_found_out_before_cloning(
    service: RelayService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_file_urls(monkeypatch)
    dirs(tmp_path / "root", "proj")
    await service.ws_add_folder(str(tmp_path / "root" / "proj"))
    (tmp_path / "root" / "proj.code-workspace").write_text("{}")
    with pytest.raises(RelayError, match="already exists"):
        await service.ws_add_repo_clone("proj", make_remote(tmp_path, "lib"), str(tmp_path / "root"))
    assert not (tmp_path / "root" / "lib").exists()
