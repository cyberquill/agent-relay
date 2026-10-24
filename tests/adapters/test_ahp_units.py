"""AHP building blocks: endpoint discovery, URIs, the action mapper and input answers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay.adapters.ahp import mapping as mp
from agent_relay.adapters.ahp import models as m
from agent_relay.adapters.ahp import uris
from agent_relay.adapters.ahp.endpoints import AhpUnavailableError, discover, parse_endpoints
from agent_relay.adapters.ahp.rpc import upgrade_uri

SOCKET = "/var/folders/x/T/vscode-ah-1/abc.sock"


def endpoints_doc(version: str = "0.10.0", kind: str = "socket") -> str:
    return json.dumps(
        {
            "userDataPath": "/Users/dev/Library/Application Support/Code",
            "endpoints": [
                {
                    "schemaVersion": 2,
                    "type": "editor",
                    "pid": 42,
                    "protocolVersion": version,
                    "connectionToken": "tok/with+chars",
                    "endpoint": {"type": kind, "path": SOCKET},
                }
            ],
        }
    )


def test_parse_endpoints_picks_the_socket() -> None:
    endpoint = parse_endpoints(endpoints_doc())
    assert (endpoint.socket_path, endpoint.protocol_version, endpoint.pid) == (SOCKET, "0.10.0", 42)
    assert endpoint.token == "tok/with+chars"
    assert "tok" not in repr(endpoint)


def test_parse_endpoints_leaves_the_version_to_initialize() -> None:
    assert parse_endpoints(endpoints_doc("1.1.0")).protocol_version == "1.1.0"


def test_parse_endpoints_rejects_bad_output() -> None:
    with pytest.raises(AhpUnavailableError, match="not running an Agent Host"):
        parse_endpoints(endpoints_doc(kind="pipe"))
    with pytest.raises(AhpUnavailableError, match="did not print JSON"):
        parse_endpoints("Error: no editor")
    with pytest.raises(AhpUnavailableError, match="not running"):
        parse_endpoints(json.dumps({"endpoints": []}))


async def test_discover_runs_the_cli(tmp_path: Path) -> None:
    cli = tmp_path / "code"
    cli.write_text("")
    calls: list[Sequence[str]] = []

    async def ok(argv: Sequence[str]) -> tuple[int, str, str]:
        calls.append(argv)
        return 0, endpoints_doc(), ""

    assert (await discover(cli, ok)).socket_path == SOCKET
    assert list(calls[0]) == [str(cli), "agent", "endpoints"]

    async def failing(argv: Sequence[str]) -> tuple[int, str, str]:
        return 1, "", "connectionToken=abcdefabcdefabcdefabcdefabcdefabcdef broke"

    with pytest.raises(AhpUnavailableError) as caught:
        await discover(cli, failing)
    assert "abcdefabcdef" not in str(caught.value)
    with pytest.raises(AhpUnavailableError, match="CLI not found"):
        await discover(tmp_path / "missing", ok)


def test_uris_round_trip() -> None:
    session = uris.session_uri("f7f70a60-951f-47ae-8e51-1d1f09eeb712")
    chat = uris.chat_uri("a8aaf650-cb18-477c-960e-d1424bde9dd9", session)
    assert chat == (
        "ahp-chat://a8aaf650-cb18-477c-960e-d1424bde9dd9/"
        "YWhwLXNlc3Npb246L2Y3ZjcwYTYwLTk1MWYtNDdhZS04ZTUxLTFkMWYwOWVlYjcxMg"
    )
    assert uris.chat_session(chat) == session
    assert uris.session_id(session) == "f7f70a60-951f-47ae-8e51-1d1f09eeb712"
    assert uris.session_id(chat) is None and uris.chat_session(session) is None
    assert uris.file_path(uris.file_uri("/tmp/a b")) == str(Path("/tmp/a b").resolve())
    assert upgrade_uri("a/b+c=") == "ws://localhost/?tkn=a%2Fb%2Bc%3D"


def test_mapper_forwards_markdown_only_and_joins_the_final_text() -> None:
    mapper = mp.ActionMapper()
    chat = "ahp-chat://c/x"
    actions: list[dict[str, JsonValue]] = [
        {
            "type": "chat/responsePart",
            "turnId": "t",
            "part": {"kind": "reasoning", "id": "r", "content": "hmm"},
        },
        {"type": "chat/delta", "turnId": "t", "partId": "r", "content": " more"},
        {
            "type": "chat/responsePart",
            "turnId": "t",
            "part": {"kind": "markdown", "id": "p", "content": "Hel"},
        },
        {"type": "chat/delta", "turnId": "t", "partId": "p", "content": "lo"},
        {"type": "chat/usage", "turnId": "t", "usage": {}},
        {"type": "chat/turnComplete", "turnId": "t", "duration": 1},
    ]
    records = [r for a in actions for r in mapper.map(chat, a)]
    assert records == [mp.Delta(chat, "Hel"), mp.Delta(chat, "lo"), mp.TurnDone(chat, "t", "Hello")]


def test_mapper_skips_malformed_actions_and_auto_confirmed_tools() -> None:
    mapper = mp.ActionMapper()
    assert mapper.map("c", {"type": "chat/delta", "turnId": "t"}) == []
    ready: dict[str, JsonValue] = {
        "type": "chat/toolCallReady",
        "turnId": "t",
        "toolCallId": "x",
        "invocationMessage": "Reading a file",
        "confirmed": "not-needed",
    }
    assert mapper.map("c", ready) == []


def question(kind: str, qid: str, **extra: JsonValue) -> m.ChatInputQuestion:
    raw: dict[str, JsonValue] = {"id": qid, "message": f"{qid}?", "kind": kind, **extra}
    request = m.ChatInputRequest.model_validate({"id": "r", "questions": [raw]})
    assert request.questions
    return request.questions[0]


def test_build_answers_covers_each_question_kind() -> None:
    options: JsonValue = [{"id": "a", "label": "Alpha"}, {"id": "b", "label": "Beta"}]
    questions = (
        question("text", "name"),
        question("integer", "count"),
        question("boolean", "sure"),
        question("single-select", "pick", options=options, allowFreeformInput=True),
        question("multi-select", "many", options=options),
    )
    response, answers = mp.build_answers(questions, "Ada\n3\nyes\nGamma\nalpha, 2")
    assert response == "accept" and answers is not None
    wire = {k: v.model_dump(mode="json", by_alias=True, exclude_none=True) for k, v in answers.items()}
    assert wire == {
        "name": {"state": "submitted", "value": {"kind": "text", "value": "Ada"}},
        "count": {"state": "submitted", "value": {"kind": "number", "value": 3}},
        "sure": {"state": "submitted", "value": {"kind": "boolean", "value": True}},
        "pick": {
            "state": "submitted",
            "value": {"kind": "selected", "value": "", "freeformValues": ["Gamma"]},
        },
        "many": {"state": "submitted", "value": {"kind": "selected-many", "value": ["a", "b"]}},
    }


def test_build_answers_skips_and_declines() -> None:
    assert mp.build_answers((question("text", "a"),), None) == ("decline", None)
    assert mp.build_answers((), "no") == ("decline", None)
    assert mp.build_answers((), "go ahead") == ("accept", None)
    _, answers = mp.build_answers((question("integer", "n"), question("text", "t")), "many")
    assert answers is not None
    assert answers["n"].model_dump(mode="json", exclude_none=True, by_alias=True) == {
        "state": "skipped",
        "freeformValues": ["many"],
    }
    assert answers["t"].model_dump(mode="json", exclude_none=True) == {"state": "skipped"}


def test_describe_request_numbers_multiple_questions() -> None:
    request = m.ChatInputRequest.model_validate(
        {
            "id": "r",
            "message": "Two things",
            "questions": [
                {"id": "a", "message": "Proceed?", "kind": "boolean"},
                {"id": "b", "message": "Name?", "kind": "text"},
            ],
        }
    )
    text, options = mp.describe_request(request)
    assert text == f"Two things\n1. Proceed? (yes / no)\n2. Name?\n{mp.MULTI_ANSWER_HINT}"
    assert options == ()
    url_only = m.ChatInputRequest.model_validate({"id": "u", "message": "Sign in", "url": "https://x"})
    assert mp.describe_request(url_only) == ("Sign in\nhttps://x", ("accept", "decline"))
