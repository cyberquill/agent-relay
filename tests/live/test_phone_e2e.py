"""UR-8, the pure parts of the phone E2E driver (cards, files, report, the Webex user, the CLI gate), and
one live check gated by RELAY_LIVE=1 that runs scenario 1 (`/help`) against the real relay: no session."""

from __future__ import annotations

import io
import json
import struct
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import replace
from email.message import Message
from http.client import HTTPMessage
from pathlib import Path
from typing import Any

import pytest

from agent_relay.core.service.common import APP_ALIASES as RELAY_ALIASES
from scripts.e2e import cli, webex_cli_user
from scripts.e2e.cardkit import (
    CARD_TYPE,
    CardMismatch,
    Choice,
    InputSpec,
    build_inputs,
    card_of,
    fill_new,
    has_card,
    is_approval_card,
    pick_agent,
    pick_workspace,
)
from scripts.e2e.files import PDF_NAME, PNG_NAME, html_problems, make_pdf, make_png, write_samples
from scripts.e2e.model import Download, DriverError, Msg, Redactor, SubmitRefused
from scripts.e2e.report import Report, Result
from scripts.e2e.runner import NO_SCOPE
from scripts.e2e.webex_cli_user import WebexCliUser
from tests.conftest import live
from tests.e2e_world import BOT, ROOM, World

TOKEN = "tok-SECRET-123456"


def msg(card: dict[str, Any] | None = None, text: str = "") -> Msg:
    attachments = [{"contentType": CARD_TYPE, "content": card}] if card else []
    return Msg.model_validate({"id": "m1", "text": text, "attachments": attachments})


def new_card() -> dict[str, Any]:
    ws = [
        {"title": "other", "value": "/x/other"},
        {"title": "scratch · ~/…/.scratch", "value": "/u/repo/.scratch"},
    ]
    agents = [
        {"title": "Copilot · Default", "value": "vscode_copilot:default"},
        {"title": "Copilot · GPT", "value": "vscode_copilot:gpt-5"},
        {"title": "Cursor · Fast", "value": "cursor_cli:fast"},
    ]
    return {
        "type": "AdaptiveCard",
        "body": [
            {"type": "TextBlock", "text": "New session"},
            {"type": "Input.ChoiceSet", "id": "ws", "choices": ws, "value": "/x/other"},
            {"type": "Container", "items": [{"type": "Input.ChoiceSet", "id": "agent", "choices": agents}]},
            {"type": "Input.Text", "id": "prompt", "isRequired": True},
        ],
        "actions": [{"type": "Action.Submit", "title": "Start", "data": {"relay": "new", "nonce": "n1"}}],
    }


# cards -----------------------------------------------------------------------


def test_a_card_is_read_through_containers_and_show_cards() -> None:
    nested = {
        "type": "AdaptiveCard",
        "body": [
            {
                "type": "ActionSet",
                "actions": [{"type": "Action.Submit", "title": "Go", "data": {"relay": "x"}}],
            }
        ],
        "actions": [
            {
                "type": "Action.ShowCard",
                "title": "More",
                "card": {"body": [{"type": "Input.Text", "id": "deep"}], "actions": []},
            }
        ],
    }
    view = card_of(msg(nested))
    assert view is not None
    assert [i.id for i in view.inputs] == ["deep"]
    assert [a.title for a in view.actions] == ["Go"]
    assert card_of(msg(text="plain")) is None
    assert has_card(msg(new_card()), "new") and not has_card(msg(new_card()), "model")


def test_the_new_card_is_filled_like_a_tap_would_fill_it() -> None:
    view = card_of(msg(new_card()))
    assert view is not None
    inputs = fill_new(view, harness="vscode_copilot", prompt="Reply with exactly: OK")
    assert inputs == {
        "relay": "new",
        "nonce": "n1",
        "ws": "/u/repo/.scratch",
        "agent": "vscode_copilot:default",
        "prompt": "Reply with exactly: OK",
    }
    assert fill_new(view, harness="cursor_cli", prompt="p")["agent"] == "cursor_cli:fast"


def test_filling_a_card_fails_loudly_when_it_does_not_offer_what_is_needed() -> None:
    view = card_of(msg(new_card()))
    assert view is not None
    with pytest.raises(CardMismatch, match="opencode"):
        fill_new(view, harness="opencode", prompt="p")
    spec = view.inputs[0]
    with pytest.raises(CardMismatch, match="no input"):
        build_inputs(view, view.actions[0], {"nope": "x"})
    with pytest.raises(CardMismatch, match="not one of the choices"):
        build_inputs(view, view.actions[0], {spec.id: "/not/offered"})
    with pytest.raises(CardMismatch, match="required"):
        build_inputs(view, view.actions[0], {"agent": "vscode_copilot:default", "prompt": ""})
    only_other = replace_choices(spec, [("other", "/x/other")])
    with pytest.raises(CardMismatch, match="scratch"):
        pick_workspace(only_other)


def replace_choices(spec: InputSpec, pairs: list[tuple[str, str]]) -> InputSpec:
    return replace(spec, choices=tuple(Choice(t, v) for t, v in pairs))


def test_workspace_and_agent_choices_prefer_the_exact_entry() -> None:
    view = card_of(msg(new_card()))
    assert view is not None
    assert pick_workspace(view.inputs[0]) == "/u/repo/.scratch"
    assert pick_agent(view.inputs[1], "vscode_copilot") == "vscode_copilot:default"


def test_an_approval_card_is_recognised_by_its_actions() -> None:
    def card(*actions: dict[str, Any]) -> Msg:
        return msg({"type": "AdaptiveCard", "body": [], "actions": list(actions)})

    allow = {"type": "Action.Submit", "title": "Allow once", "data": {"relay": "x"}}
    tagged = {"type": "Action.Submit", "title": "Go", "data": {"relay": "approval"}}
    send = {"type": "Action.Submit", "title": "Send", "data": {"relay": "question"}}
    assert is_approval_card(card(allow)) and is_approval_card(card(tagged))
    assert not is_approval_card(card(send)) and not is_approval_card(msg(text="Allow"))


# files and html --------------------------------------------------------------


def test_the_sample_files_are_valid(tmp_path: Path) -> None:
    png = make_png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(
        b"IEND" + struct.pack(">I", zlib.crc32(b"IEND"))
    )
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (8, 8)
    idat = png[png.index(b"IDAT") + 4 : png.index(b"IEND") - 8]
    assert len(zlib.decompress(idat)) == height * (1 + 3 * width)
    pdf = make_pdf()
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    start = int(pdf[pdf.rindex(b"startxref") :].split()[1])
    assert pdf[start : start + 4] == b"xref"
    entries = pdf[start:].split(b"\n")[3:8]
    for number, entry in enumerate(entries, 1):
        offset = int(entry.split()[0])
        assert pdf[offset:].startswith(f"{number} 0 obj".encode())
    png_path, pdf_path = write_samples(tmp_path / "x")
    assert (png_path.name, pdf_path.name) == (PNG_NAME, PDF_NAME)
    assert png_path.read_bytes() == png and pdf_path.read_bytes() == pdf


SAFE = (
    b'<!doctype html><meta http-equiv="Content-Security-Policy" '
    b"content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\"><p>hi</p>"
)


@pytest.mark.parametrize(
    ("html", "problem"),
    [
        (SAFE, None),
        (SAFE.replace(b"'none'", b"&#x27;none&#x27;"), None),  # the relay escapes the quotes in the attribute
        (SAFE.replace(b"Content-Security-Policy", b"X-Other"), "no Content-Security-Policy"),
        (SAFE.replace(b"default-src 'none'", b"default-src *"), "default-src 'none'"),
        (SAFE + b"<SCRIPT >x</SCRIPT>", "<script"),
        (SAFE + b'<a HREF = "x">y</a>', "href="),
        (SAFE + b'<img src="https://evil.example/x.png">', "external resource"),
        (SAFE + b"<style>@import 'x.css'</style>", "external resource"),
        (SAFE + b"<style>a{background:url(http://e/x)}</style>", "external resource"),
    ],
)
def test_answer_html_checks(html: bytes, problem: str | None) -> None:
    problems = html_problems(html)
    assert (problem is None and problems == []) or (
        problem is not None and any(problem in p for p in problems)
    )


# report ----------------------------------------------------------------------


def test_the_report_renders_a_markdown_table_and_an_exit_code() -> None:
    report = Report(started="2026-11-21 16:00 UTC")
    report.add(Result("1", "/help", "-", "PASS", "one reply", 3.2))
    report.add(Result("2", "card | new", "copilot", "SKIP", NO_SCOPE, 0.0))
    markdown = report.markdown()
    assert "| 1 | /help | - | PASS | one reply | 3 |" in markdown
    assert "card \\| new" in markdown
    assert "1 passed, 0 failed, 1 skipped." in markdown
    assert report.exit_code() == 0 and "PASS" in report.text()
    report.add(Result("3", "x", "copilot", "FAIL", "y" * 400, 1.0))
    assert report.exit_code() == 1 and "…" in report.markdown()
    assert Report(started="t").exit_code() == 1  # nothing ran is not a pass


# the Webex user --------------------------------------------------------------


class Response:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status, self.body, self.headers = status, body, headers

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


type Reply = tuple[int, bytes] | tuple[int, bytes, dict[str, str]]


class Opener:
    """Stands in for urllib's opener: canned replies, and a record of what was asked."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float = 0) -> Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        status, body = reply[0], reply[1]
        headers = reply[2] if len(reply) == 3 else {}
        if status >= 400:
            raw = Message()
            for key, value in headers.items():
                raw[key] = value
            raise urllib.error.HTTPError(request.full_url, status, "error", raw, io.BytesIO(body))
        return Response(status, body, headers)

    def json_body(self, index: int = 0) -> dict[str, object]:
        data = self.requests[index].data
        assert isinstance(data, bytes)
        body: dict[str, object] = json.loads(data)
        return body


def webex_user(monkeypatch: pytest.MonkeyPatch, *replies: Reply) -> tuple[WebexCliUser, Opener, Redactor]:
    opener = Opener(*replies)
    monkeypatch.setattr(webex_cli_user, "_OPENER", opener)
    redact = Redactor()
    return WebexCliUser(redact, token_source=lambda: TOKEN), opener, redact


async def test_an_error_from_webex_never_carries_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    user, _, _ = webex_user(monkeypatch, (403, f'{{"message":"bad {TOKEN}"}}'.encode()))
    with pytest.raises(DriverError) as caught:
        await user.delete("m1")
    assert "403" in str(caught.value) and TOKEN not in str(caught.value)


async def test_a_refused_card_submit_is_reported_as_missing_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    user, _, _ = webex_user(monkeypatch, (403, b'{"message":"insufficient scope"}'))
    with pytest.raises(SubmitRefused) as caught:
        await user.submit_card("card-1", {"relay": "new"})
    assert caught.value.status == 403 and TOKEN not in str(caught.value)


async def test_a_card_submit_posts_the_documented_body(monkeypatch: pytest.MonkeyPatch) -> None:
    user, opener, _ = webex_user(monkeypatch, (200, b'{"id":"a1"}'))
    await user.submit_card("card-1", {"relay": "new", "ws": "w"})
    request = opener.requests[0]
    assert (
        request.full_url == "https://webexapis.com/v1/attachment/actions" and request.get_method() == "POST"
    )
    assert opener.json_body() == {
        "type": "submit",
        "messageId": "card-1",
        "inputs": {"relay": "new", "ws": "w"},
    }
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"


async def test_the_scope_probe_reads_the_status_once(monkeypatch: pytest.MonkeyPatch) -> None:
    user, opener, _ = webex_user(monkeypatch, (403, b"{}"))
    assert await user.can_submit_cards() is False
    assert await user.can_submit_cards() is False
    assert len(opener.requests) == 1
    user, opener, _ = webex_user(monkeypatch, (400, b'{"message":"Invalid messageId"}'))
    assert await user.can_submit_cards() is True


def test_the_token_is_only_ever_sent_to_webexapis(monkeypatch: pytest.MonkeyPatch) -> None:
    user, opener, _ = webex_user(monkeypatch)
    with pytest.raises(DriverError, match="host other than webexapis.com"):
        user._http("GET", "https://evil.example/contents/x")
    assert opener.requests == []
    refused = webex_cli_user._NoRedirect().redirect_request(
        urllib.request.Request("https://webexapis.com/v1/contents/x"),
        io.BytesIO(),
        302,
        "Found",
        HTTPMessage(),
        "https://evil.example/x",
    )
    assert refused is None


def write_cli(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "webex-cli"  # no .py suffix, like the real one
    script.write_text(body, encoding="utf-8")
    return script


async def test_the_token_comes_from_webex_cli_without_running_its_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = write_cli(
        tmp_path,
        "def get_access_token():\n    return 'file-token-123456'\n\n"
        "def main():\n    raise SystemExit('main must not run')\n\n"
        "if __name__ == '__main__':\n    main()\n",
    )
    opener = Opener((200, b'{"id":"me","emails":["o@example.com"]}'))
    monkeypatch.setattr(webex_cli_user, "_OPENER", opener)
    redact = Redactor()
    me = await WebexCliUser(redact, script).whoami()
    assert (me.id, me.email) == ("me", "o@example.com")
    assert opener.requests[0].get_header("Authorization") == "Bearer file-token-123456"
    assert "file-token-123456" not in redact("x file-token-123456 y")


async def test_a_failed_refresh_in_webex_cli_is_a_safe_error(tmp_path: Path) -> None:
    script = write_cli(
        tmp_path,
        "import sys\n\ndef get_access_token():\n    sys.exit('refresh failed Bearer abcdefghijklmnop')\n",
    )
    with pytest.raises(DriverError) as caught:
        await WebexCliUser(Redactor(), script).whoami()
    assert "could not provide a token" in str(caught.value) and "abcdefghijklmnop" not in str(caught.value)
    with pytest.raises(DriverError, match="not found"):
        await WebexCliUser(Redactor(), tmp_path / "missing").whoami()


async def test_a_file_is_uploaded_as_multipart_with_its_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = write_samples(tmp_path)[0]
    user, opener, _ = webex_user(monkeypatch, (200, json.dumps({"id": "m2", "roomId": ROOM}).encode()))
    sent = await user.send(ROOM, "", parent_id="root-1", file=png)
    assert sent.id == "m2"
    request = opener.requests[0]
    assert request.get_header("Content-type", "").startswith("multipart/form-data; boundary=")
    body = request.data
    assert isinstance(body, bytes)
    assert f'filename="{PNG_NAME}"'.encode() in body and b'name="parentId"' in body
    assert b'name="text"' not in body and b"\x89PNG" in body


async def test_a_locked_download_is_retried_and_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    disposition = {"Content-Disposition": 'attachment; filename="answer.html"', "Content-Type": "text/html"}
    user, opener, _ = webex_user(monkeypatch, (423, b"", {"Retry-After": "1"}), (200, b"<html>", disposition))
    got = await user.download("https://webexapis.com/v1/contents/abc")
    assert got == Download("answer.html", "text/html", b"<html>") and len(opener.requests) == 2


async def test_the_room_must_be_a_one_to_one_with_a_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    me = (200, b'{"id":"me","emails":["o@example.com"]}')
    members = {
        "items": [{"personId": "me", "personEmail": "o@example.com"}, {"personId": "b", "personEmail": BOT}]
    }
    user, _, _ = webex_user(monkeypatch, me, (200, json.dumps(members).encode()))
    assert (await user.counterpart(ROOM)).email == BOT
    human = {
        "items": [
            {"personId": "me", "personEmail": "o@example.com"},
            {"personId": "h", "personEmail": "h@example.com"},
        ]
    }
    user, _, _ = webex_user(monkeypatch, me, (200, json.dumps(human).encode()))
    with pytest.raises(DriverError, match="not a bot"):
        await user.counterpart(ROOM)
    two = {"items": [{"id": "r1", "title": "Brij"}, {"id": "r2", "title": "Brij"}]}
    user, _, _ = webex_user(monkeypatch, (200, json.dumps(two).encode()))
    with pytest.raises(DriverError, match="--room-id"):
        await user.find_room("Brij")


async def test_messages_parse_from_the_webex_wire_format(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {
        "id": "m1",
        "roomId": ROOM,
        "personEmail": BOT,
        "text": "hi",
        "parentId": "p",
        "files": ["u"],
        "extra": 1,
    }
    user, opener, _ = webex_user(monkeypatch, (200, json.dumps({"items": [item]}).encode()))
    (message,) = await user.list_messages(ROOM, parent_id="p", limit=5)
    assert (message.person_email, message.parent_id, message.files) == (BOT, "p", ["u"])
    assert "parentId=p" in opener.requests[0].full_url and "max=5" in opener.requests[0].full_url


# the CLI ---------------------------------------------------------------------


def boom_factory(redact: Redactor, path: Path) -> World:
    raise AssertionError("the Webex user must not be built")


def run_cli(
    argv: list[str],
    environ: dict[str, str],
    factory: Callable[[Redactor, Path], World],
    world: World | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        argv,
        environ=environ,
        user_factory=factory,
        clock=world.clock if world else None,
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("environ", [{}, {"RELAY_LIVE": "0"}, {"RELAY_LIVE": "true"}])
def test_the_driver_refuses_to_run_without_relay_live_one(environ: dict[str, str]) -> None:
    code, out, err = run_cli(["--apps", "copilot"], environ, boom_factory)
    assert code == 2 and out == "" and "RELAY_LIVE=1" in err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "--apps"),
        (["--only", "3"], "--apps"),
        (["--only", "12", "--apps", "copilot"], "unknown scenario"),
        (["--apps", "copilot-cli"], "cannot be started from the phone"),
        (["--apps", "nope"], "unknown app"),
    ],
)
def test_bad_arguments_stop_the_run_before_anything_is_sent(argv: list[str], message: str) -> None:
    code, _, err = run_cli(argv, {"RELAY_LIVE": "1"}, boom_factory)
    assert code == 2 and message in err


def test_app_names_resolve_like_the_relay_does() -> None:
    assert cli.APP_ALIASES == RELAY_ALIASES
    assert cli.parse_apps("copilot, cursor ,cursor,oc,vscode") == [
        "vscode_copilot",
        "cursor_cli",
        "opencode",
        "vscode",
    ]
    assert cli.parse_apps("cursor_cdp") == ["cursor_cdp"]
    assert cli.parse_numbers("1, 9") == {1, 9} and cli.parse_numbers("") == set()


def test_a_live_run_is_wired_end_to_end_and_writes_the_report(tmp_path: Path) -> None:
    world = World()
    report_path = tmp_path / "out.md"
    code, out, err = run_cli(
        ["--only", "1", "--report", str(report_path)], {"RELAY_LIVE": "1"}, lambda r, p: world, world
    )
    assert code == 0 and err == ""
    assert "PASS" in out and "1 passed, 0 failed, 0 skipped" in out and "cleanup: deleted 1 of 1" in out
    assert "| 1 | /help replies, nothing else | - | PASS |" in report_path.read_text(encoding="utf-8")


def test_the_scope_is_probed_only_when_a_card_scenario_runs(tmp_path: Path) -> None:
    probes: list[bool] = []

    class Spy(World):
        async def can_submit_cards(self) -> bool:
            probes.append(True)
            return await super().can_submit_cards()

    world = Spy()
    run_cli(["--only", "1"], {"RELAY_LIVE": "1"}, lambda r, p: world, world)
    assert probes == []
    world = Spy()
    code, _, _ = run_cli(["--only", "9"], {"RELAY_LIVE": "1"}, lambda r, p: world, world)
    assert code == 0 and probes == [True]


def test_a_setup_error_is_scrubbed_and_exits_two() -> None:
    def factory(redact: Redactor, path: Path) -> World:
        raise DriverError("could not start: Bearer abcdefghijklmnop1234")

    code, out, err = run_cli(["--only", "1"], {"RELAY_LIVE": "1"}, factory)
    assert code == 2 and out == "" and "could not start" in err and "abcdefghijklmnop1234" not in err


@live
def test_live_help_round_trip_creates_no_session() -> None:
    assert cli.main(["--only", "1"]) == 0
