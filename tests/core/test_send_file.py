"""UR-6: `send_phone_file` only ever sends a regular file from inside the session's workspace."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_relay.core.send_file import SendableFile, SendFileError, validate_file

CAP = 4096
FAKE_TOKEN = "ghp_" + "a1B2" * 9
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "out").mkdir(parents=True)
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    (folder / "secret.txt").write_text("not yours")
    return folder


def test_a_relative_path_inside_the_workspace_is_sent(workspace: Path) -> None:
    (workspace / "out" / "report.html").write_text("<h1>done</h1>")
    sent = validate_file(str(workspace), "out/report.html", CAP)
    assert sent == SendableFile("report.html", b"<h1>done</h1>", "text/html")


def test_an_absolute_path_inside_the_workspace_is_sent(workspace: Path) -> None:
    target = workspace / "notes.md"
    target.write_text("# hi")
    sent = validate_file(str(workspace), str(target), CAP)
    assert (sent.name, sent.data) == ("notes.md", b"# hi")
    assert sent.content_type.startswith("text/")


def test_dot_segments_that_stay_inside_are_fine(workspace: Path) -> None:
    (workspace / "a.txt").write_text("x")
    assert validate_file(str(workspace), "out/../a.txt", CAP).data == b"x"


def test_a_png_passes_through_byte_for_byte(workspace: Path) -> None:
    (workspace / "shot.png").write_bytes(PNG)
    sent = validate_file(str(workspace), "shot.png", CAP)
    assert sent == SendableFile("shot.png", PNG, "image/png")


def test_binary_data_in_a_text_named_file_is_not_decoded(workspace: Path) -> None:
    (workspace / "weird.txt").write_bytes(b"\xff\xfe\x00\x01" + FAKE_TOKEN.encode())
    sent = validate_file(str(workspace), "weird.txt", CAP)
    assert sent.data.startswith(b"\xff\xfe\x00\x01")


def test_an_unknown_type_is_sent_as_octet_stream(workspace: Path) -> None:
    (workspace / "blob.zzz").write_bytes(b"\x00\x01")
    assert validate_file(str(workspace), "blob.zzz", CAP).content_type == "application/octet-stream"


def test_a_text_file_is_scrubbed(workspace: Path) -> None:
    (workspace / "env.log").write_text(f"GITHUB_TOKEN={FAKE_TOKEN}\nplain line\n")
    sent = validate_file(str(workspace), "env.log", CAP)
    assert FAKE_TOKEN.encode() not in sent.data
    assert b"plain line" in sent.data
    assert b"<redacted>" in sent.data


def test_an_html_report_is_scrubbed_too(workspace: Path) -> None:
    (workspace / "r.html").write_text(f"<p>{FAKE_TOKEN}</p>")
    assert FAKE_TOKEN.encode() not in validate_file(str(workspace), "r.html", CAP).data


def test_the_name_is_made_safe(workspace: Path) -> None:
    (workspace / "my report (final).txt").write_text("x")
    assert validate_file(str(workspace), "my report (final).txt", CAP).name == "my report _final_.txt"


# refusals


def test_dot_dot_out_of_the_workspace_is_refused(workspace: Path, outside: Path) -> None:
    with pytest.raises(SendFileError, match="outside this session's workspace"):
        validate_file(str(workspace), "../elsewhere/secret.txt", CAP)


def test_an_absolute_path_outside_the_workspace_is_refused(workspace: Path, outside: Path) -> None:
    with pytest.raises(SendFileError, match="outside this session's workspace"):
        validate_file(str(workspace), str(outside / "secret.txt"), CAP)


def test_a_sibling_folder_with_the_same_prefix_is_outside(workspace: Path) -> None:
    sibling = workspace.parent / "ws-evil"
    sibling.mkdir()
    (sibling / "x.txt").write_text("x")
    with pytest.raises(SendFileError, match="outside"):
        validate_file(str(workspace), str(sibling / "x.txt"), CAP)


def test_a_symlink_inside_pointing_outside_is_refused(workspace: Path, outside: Path) -> None:
    (workspace / "link.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(SendFileError, match="outside this session's workspace"):
        validate_file(str(workspace), "link.txt", CAP)


def test_a_symlinked_folder_pointing_outside_is_refused(workspace: Path, outside: Path) -> None:
    (workspace / "docs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SendFileError, match="outside this session's workspace"):
        validate_file(str(workspace), "docs/secret.txt", CAP)


def test_a_symlink_that_stays_inside_is_followed(workspace: Path) -> None:
    (workspace / "real.txt").write_text("real")
    (workspace / "alias.txt").symlink_to(workspace / "real.txt")
    assert validate_file(str(workspace), "alias.txt", CAP).data == b"real"


def test_a_home_relative_path_is_not_resolved_against_the_workspace(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, outside: Path
) -> None:
    monkeypatch.setenv("RELAY_HOME", str(outside))
    with pytest.raises(SendFileError, match="outside"):
        validate_file(str(workspace), "~/secret.txt", CAP)


def test_an_oversized_file_is_refused(workspace: Path) -> None:
    (workspace / "big.bin").write_bytes(b"x" * (CAP + 1))
    with pytest.raises(SendFileError, match="over the 4 KB limit"):
        validate_file(str(workspace), "big.bin", CAP)


def test_a_file_exactly_at_the_cap_is_fine(workspace: Path) -> None:
    (workspace / "edge.bin").write_bytes(b"x" * CAP)
    assert len(validate_file(str(workspace), "edge.bin", CAP).data) == CAP


def test_a_directory_is_refused(workspace: Path) -> None:
    with pytest.raises(SendFileError, match="not a regular file"):
        validate_file(str(workspace), "out", CAP)


def test_a_missing_file_is_refused(workspace: Path) -> None:
    with pytest.raises(SendFileError, match="does not exist"):
        validate_file(str(workspace), "nope.txt", CAP)


def test_an_empty_path_is_refused(workspace: Path) -> None:
    with pytest.raises(SendFileError, match="no file path"):
        validate_file(str(workspace), "  ", CAP)


def test_a_fifo_is_refused_without_blocking(workspace: Path) -> None:
    os.mkfifo(workspace / "pipe")
    with pytest.raises(SendFileError, match="not a regular file"):
        validate_file(str(workspace), "pipe", CAP)


def test_a_missing_workspace_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SendFileError, match="workspace folder is missing"):
        validate_file(str(tmp_path / "gone"), "a.txt", CAP)


def test_the_refusal_names_the_path_it_was_given_not_the_resolved_one(workspace: Path, outside: Path) -> None:
    (workspace / "link.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(SendFileError) as caught:
        validate_file(str(workspace), "link.txt", CAP)
    assert "elsewhere" not in str(caught.value)
