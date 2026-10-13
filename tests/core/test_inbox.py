"""The inbox keeps the phone's files inside the workspace, owner-only, and removes them when they are done."""

from __future__ import annotations

import stat
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from agent_relay import paths
from agent_relay.core.housekeeping import housekeep
from agent_relay.core.inbox import (
    DIRS_PREFIX,
    HOLD_PREFIX,
    TOP,
    Held,
    Inbox,
    Incoming,
    SkipFileError,
    TooLargeError,
    folder_name,
    human_size,
    receipt_line,
    safe_name,
    sweep_inbox,
)
from agent_relay.core.registry import Registry
from agent_relay.models.attachments import Attachment, Received
from agent_relay.models.config import AttachmentsConfig
from agent_relay.models.events import Harness, Origin, utcnow
from agent_relay.models.sessions import SessionRecord, SessionStatus


def incoming(name: str, data: bytes, mime: str | None = "text/plain", step: int = 1024) -> Incoming:
    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(data), step):
            yield data[start : start + step]

    return Incoming(name, mime, chunks())


def mode(target: Path) -> int:
    return stat.S_IMODE(target.stat().st_mode)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    target = tmp_path / "proj"
    target.mkdir()
    return target


@pytest.fixture
def inbox(registry: Registry) -> Inbox:
    return Inbox(registry, AttachmentsConfig(max_bytes=4096))


def at_time(monkeypatch: pytest.MonkeyPatch, **delta: float) -> None:
    later = utcnow() + timedelta(**delta)
    monkeypatch.setattr("agent_relay.core.inbox.utcnow", lambda: later)


# names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("photo.JPG", "photo.JPG"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\win.ini", "win.ini"),
        (".bashrc", "bashrc"),
        ("  .hidden.txt", "hidden.txt"),
        ("report final (v2).pdf", "report final _v2_.pdf"),
        ("naïve résumé.txt", "na_ve r_sum_.txt"),
        ("....", "file"),
        ("", "file"),
        ("a\x00b\nc.txt", "a_b_c.txt"),
        ("x.abcdefghijklmnopqrstuvwxyz", "x.abcdefghijklmno"),
        ("a" * 200 + ".txt", "a" * 80 + ".txt"),
    ],
)
def test_file_names_become_one_safe_component(raw: str, expected: str) -> None:
    assert safe_name(raw) == expected


def test_folder_names_cannot_climb_or_hide() -> None:
    assert folder_name("opencode:abc-1") == "opencode_abc-1"
    assert folder_name("../x") == "_x"
    assert folder_name("...") == "session"


def test_sizes_read_naturally() -> None:
    assert human_size(20 * 1024 * 1024) == "20 MB"
    assert human_size(1536 * 1024) == "1.5 MB"
    assert human_size(2048) == "2 KB"


def test_the_receipt_names_how_each_file_travelled() -> None:
    photo = Attachment(name="photo.jpg", mime="image/jpeg", size=1, local_path="/x/photo.jpg", sha256="0")
    log = Attachment(name="log.txt", mime="text/plain", size=1, local_path="/x/log.txt", sha256="0")
    skipped = ("big.zip (over 20 MB)",)
    assert receipt_line(Received(attachments=(photo, log), skipped=skipped), True) == (
        "📎 photo.jpg (attached), log.txt (attached); skipped big.zip (over 20 MB)"
    )
    assert receipt_line(Received(attachments=(log,)), False) == "📎 log.txt (as a path)"
    assert receipt_line(Received(skipped=skipped), True) == "📎 Skipped big.zip (over 20 MB)"


# saving


async def test_a_file_lands_in_the_session_folder_owner_only(inbox: Inbox, workspace: Path) -> None:
    saved = await inbox.store(
        incoming("notes.txt", b"hello"), workspace=workspace, folder="opencode:s1", session_key="opencode:s1"
    )
    target = workspace / ".agent-relay" / "inbox" / "opencode_s1" / "notes.txt"
    assert Path(saved.local_path).resolve() == target.resolve()
    assert target.read_bytes() == b"hello"
    assert (saved.name, saved.mime, saved.size) == ("notes.txt", "text/plain", 5)
    assert len(saved.sha256) == 64
    assert mode(target) == 0o600
    for folder in (target.parent, target.parent.parent, target.parent.parent.parent):
        assert mode(folder) == 0o700


async def test_the_type_falls_back_to_the_name_when_the_download_has_none(
    inbox: Inbox, workspace: Path
) -> None:
    saved = await inbox.store(
        incoming("shot.png", b"x", "application/octet-stream"), workspace=workspace, folder="s"
    )
    assert saved.mime == "image/png"
    other = await inbox.store(incoming("blob.zzz", b"x", None), workspace=workspace, folder="s")
    assert other.mime == "application/octet-stream"


async def test_two_files_with_one_name_are_numbered(inbox: Inbox, workspace: Path) -> None:
    first = await inbox.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")
    second = await inbox.store(incoming("a.txt", b"2"), workspace=workspace, folder="s")
    assert (first.name, second.name) == ("a.txt", "a-2.txt")
    assert Path(second.local_path).read_bytes() == b"2"


async def test_a_planted_link_with_the_same_name_is_never_written_through(
    inbox: Inbox, workspace: Path, tmp_path: Path
) -> None:
    first = await inbox.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    Path(first.local_path).unlink()
    Path(first.local_path).symlink_to(outside)
    again = await inbox.store(incoming("a.txt", b"2"), workspace=workspace, folder="s")
    assert again.name == "a-2.txt"
    assert outside.read_text() == "keep"


async def test_a_file_over_the_cap_is_refused_and_nothing_is_left(inbox: Inbox, workspace: Path) -> None:
    with pytest.raises(TooLargeError) as raised:
        await inbox.store(incoming("big.bin", b"x" * 5000, step=700), workspace=workspace, folder="s")
    assert raised.value.describe() == "big.bin (over 4 KB)"
    assert list((workspace / ".agent-relay" / "inbox" / "s").iterdir()) == []


async def test_a_file_exactly_at_the_cap_is_kept(inbox: Inbox, workspace: Path) -> None:
    saved = await inbox.store(incoming("ok.bin", b"x" * 4096), workspace=workspace, folder="s")
    assert saved.size == 4096


async def test_an_inbox_folder_that_is_a_link_out_of_the_workspace_is_refused(
    inbox: Inbox, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (workspace / ".agent-relay").symlink_to(outside)
    with pytest.raises(SkipFileError, match="link"):
        await inbox.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")
    assert list(outside.iterdir()) == []


async def test_a_session_folder_that_is_a_link_is_refused(
    inbox: Inbox, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    base = workspace / ".agent-relay" / "inbox"
    base.mkdir(parents=True)
    (base / "s").symlink_to(outside)
    with pytest.raises(SkipFileError, match="link"):
        await inbox.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("inbox_dir", ["../up", "/tmp/abs", "", "a/../../b"])
async def test_an_inbox_dir_must_stay_inside_the_workspace(
    registry: Registry, workspace: Path, inbox_dir: str
) -> None:
    bad = Inbox(registry, AttachmentsConfig(inbox_dir=inbox_dir))
    with pytest.raises(SkipFileError, match="inside the workspace"):
        await bad.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")


async def test_a_missing_workspace_is_refused(inbox: Inbox, tmp_path: Path) -> None:
    with pytest.raises(SkipFileError, match="missing"):
        await inbox.store(incoming("a.txt", b"1"), workspace=tmp_path / "gone", folder="s")


async def test_a_custom_inbox_dir_is_used(registry: Registry, workspace: Path) -> None:
    custom = Inbox(registry, AttachmentsConfig(inbox_dir="tmp/phone"))
    saved = await custom.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")
    assert Path(saved.local_path).resolve() == (workspace / "tmp" / "phone" / "s" / "a.txt").resolve()


# git


async def test_a_git_workspace_ignores_the_inbox_once(inbox: Inbox, workspace: Path) -> None:
    (workspace / ".git" / "info").mkdir(parents=True)
    (workspace / ".git" / "info" / "exclude").write_text("# existing\nbuild/")
    await inbox.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")
    await inbox.store(incoming("b.txt", b"2"), workspace=workspace, folder="t")
    lines = (workspace / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines == ["# existing", "build/", ".agent-relay/"]


async def test_a_workspace_that_is_not_a_repo_gets_no_git_folder(inbox: Inbox, workspace: Path) -> None:
    await inbox.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")
    assert not (workspace / ".git").exists()


async def test_a_linked_worktree_uses_the_shared_exclude_file(
    inbox: Inbox, workspace: Path, tmp_path: Path
) -> None:
    common = tmp_path / "main" / ".git"
    gitdir = common / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..")
    (workspace / ".git").write_text(f"gitdir: {gitdir}\n")
    await inbox.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")
    assert (common / "info" / "exclude").read_text() == ".agent-relay/\n"


# staging and holding


async def stage(inbox: Inbox, name: str, data: bytes) -> Attachment:
    return await inbox.store(incoming(name, data), workspace=None, folder="top-1")


async def test_files_without_a_workspace_wait_in_private_staging(inbox: Inbox) -> None:
    saved = await stage(inbox, "a.txt", b"1")
    staging = paths.app_support() / "inbox-staging"
    assert Path(saved.local_path).resolve().is_relative_to(staging.resolve())
    assert mode(staging) == 0o700
    assert mode(Path(saved.local_path).parent) == 0o700
    assert mode(Path(saved.local_path)) == 0o600


async def test_held_files_are_released_once_and_merge_with_later_ones(inbox: Inbox, workspace: Path) -> None:
    one = await inbox.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")
    two = await inbox.store(incoming("b.txt", b"2"), workspace=workspace, folder="s")
    await inbox.hold("thread-1", Received(attachments=(one,), skipped=("x (big)",)))
    await inbox.hold("thread-1", Received(attachments=(two,)))
    assert await inbox.take("thread-2") is None
    got = await inbox.take("thread-1")
    assert got == Received(attachments=(one, two), skipped=("x (big)",))
    assert await inbox.take("thread-1") is None


async def test_held_files_past_their_time_are_deleted_not_released(
    inbox: Inbox, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one = await inbox.store(incoming("a.txt", b"1"), workspace=workspace, folder="s")
    await inbox.hold("thread-1", Received(attachments=(one,)))
    at_time(monkeypatch, seconds=AttachmentsConfig().hold_s + 1)
    assert await inbox.take("thread-1") is None
    assert not Path(one.local_path).exists()


async def test_files_sent_with_new_move_into_the_new_sessions_folder(inbox: Inbox, workspace: Path) -> None:
    staged = await stage(inbox, "a.txt", b"hello")
    await inbox.hold(TOP, Received(attachments=(staged,), skipped=("big.zip (over 4 KB)",)), staged=True)
    got = await inbox.take_for_new(workspace)
    assert got is not None
    assert got.skipped == ("big.zip (over 4 KB)",)
    (moved,) = got.attachments
    target = Path(moved.local_path)
    assert target.parent.name.startswith("new-")
    assert target.parent.parent == (workspace / ".agent-relay" / "inbox").resolve()
    assert target.read_bytes() == b"hello"
    assert not Path(staged.local_path).exists()
    assert await inbox.take_for_new(workspace) is None


async def test_new_with_nothing_held_has_no_files(inbox: Inbox, workspace: Path) -> None:
    assert await inbox.take_for_new(workspace) is None


async def test_a_staged_file_that_cannot_move_is_reported_skipped(
    registry: Registry, workspace: Path
) -> None:
    staged = await stage(Inbox(registry, AttachmentsConfig()), "a.txt", b"hello")
    await Inbox(registry, AttachmentsConfig()).hold(TOP, Received(attachments=(staged,)), staged=True)
    got = await Inbox(registry, AttachmentsConfig(inbox_dir="../out")).take_for_new(workspace)
    assert got is not None
    assert got.attachments == ()
    assert got.skipped and got.skipped[0].startswith("attachments (")


async def test_discarding_removes_the_files_and_an_emptied_new_folder(inbox: Inbox, workspace: Path) -> None:
    staged = await stage(inbox, "a.txt", b"1")
    await inbox.hold(TOP, Received(attachments=(staged,)), staged=True)
    got = await inbox.take_for_new(workspace)
    assert got is not None
    folder = Path(got.attachments[0].local_path).parent
    await inbox.discard(got)
    assert not folder.exists()


# cleanup


async def session(registry: Registry, key: str, status: SessionStatus) -> None:
    await registry.create_session(
        SessionRecord(
            session_key=key, harness=Harness.OPENCODE, harness_id=key, origin=Origin.PHONE, status=status
        )
    )


async def test_a_session_folder_goes_a_day_after_the_session_ended(
    inbox: Inbox, registry: Registry, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await session(registry, "opencode:old", SessionStatus.ENDED)
    saved = await inbox.store(
        incoming("a.txt", b"1"), workspace=workspace, folder="opencode:old", session_key="opencode:old"
    )
    folder = Path(saved.local_path).parent
    assert await sweep_inbox(registry) == 0
    assert folder.exists()
    at_time(monkeypatch, hours=23)
    assert await sweep_inbox(registry) == 0
    at_time(monkeypatch, hours=25)
    assert await sweep_inbox(registry) == 1
    assert not folder.exists()
    assert await registry.kv_get(f"{DIRS_PREFIX}opencode:old") is None


async def test_a_running_sessions_files_stay(
    inbox: Inbox, registry: Registry, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await session(registry, "opencode:live", SessionStatus.RUNNING)
    saved = await inbox.store(
        incoming("a.txt", b"1"), workspace=workspace, folder="opencode:live", session_key="opencode:live"
    )
    at_time(monkeypatch, days=30)
    assert await sweep_inbox(registry) == 0
    assert Path(saved.local_path).exists()


async def test_the_folder_of_a_session_that_no_longer_exists_goes(
    inbox: Inbox, registry: Registry, workspace: Path
) -> None:
    saved = await inbox.store(
        incoming("a.txt", b"1"), workspace=workspace, folder="opencode:gone", session_key="opencode:gone"
    )
    assert await sweep_inbox(registry) == 1
    assert not Path(saved.local_path).parent.exists()


async def test_a_new_session_folder_is_cleaned_once_the_session_claims_it(
    inbox: Inbox, registry: Registry, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = await stage(inbox, "a.txt", b"1")
    await inbox.hold(TOP, Received(attachments=(staged,)), staged=True)
    got = await inbox.take_for_new(workspace)
    assert got is not None
    await session(registry, "opencode:n1", SessionStatus.ENDED)
    await inbox.claim("opencode:n1", got)
    folder = Path(got.attachments[0].local_path).parent
    at_time(monkeypatch, days=2)
    assert await sweep_inbox(registry) == 1
    assert not folder.exists()


async def test_expired_held_files_are_swept_and_fresh_ones_kept(
    inbox: Inbox, registry: Registry, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await session(registry, "opencode:h", SessionStatus.RUNNING)
    saved = await inbox.store(
        incoming("a.txt", b"1"), workspace=workspace, folder="opencode:h", session_key="opencode:h"
    )
    await inbox.hold("thread-h", Received(attachments=(saved,)))
    assert await sweep_inbox(registry) == 0
    assert await registry.kv_get(f"{HOLD_PREFIX}thread-h") is not None
    at_time(monkeypatch, seconds=AttachmentsConfig().hold_s + 1)
    assert await sweep_inbox(registry) == 1
    assert not Path(saved.local_path).exists()
    assert await registry.kv_get(f"{HOLD_PREFIX}thread-h") is None


async def test_a_garbled_hold_is_dropped(registry: Registry) -> None:
    await registry.kv_set(f"{HOLD_PREFIX}x", "not json")
    assert await sweep_inbox(registry) == 1
    assert await registry.kv_get(f"{HOLD_PREFIX}x") is None


async def test_sweeping_never_follows_a_link_or_deletes_a_foreign_folder(
    registry: Registry, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    link = workspace / "opencode_k"
    link.symlink_to(outside)
    foreign = workspace / "not-an-inbox-folder"
    foreign.mkdir()
    await registry.kv_set(f"{DIRS_PREFIX}opencode:k", f'["{link}", "{foreign}"]')
    assert await sweep_inbox(registry) == 1
    assert (outside / "keep.txt").exists()
    assert foreign.exists()


async def test_housekeeping_runs_the_sweep(registry: Registry, workspace: Path, inbox: Inbox) -> None:
    saved = await inbox.store(
        incoming("a.txt", b"1"), workspace=workspace, folder="opencode:z", session_key="opencode:z"
    )
    assert await housekeep(registry) == 1
    assert not Path(saved.local_path).exists()


def test_held_records_round_trip() -> None:
    held = Held(expires=utcnow(), received=Received(skipped=("a",)), staged=True)
    assert Held.model_validate_json(held.model_dump_json()) == held
