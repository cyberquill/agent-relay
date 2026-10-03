"""The committed AHP models are generated from the committed schemas, and they read the recorded trace."""

from __future__ import annotations

import json

from pydantic import JsonValue

from agent_relay.adapters.ahp import codegen
from agent_relay.adapters.ahp import models as m
from tests.fakes.ahp import TRACE_MESSAGES


def test_models_are_up_to_date() -> None:
    expected = codegen.generate()
    assert codegen.OUTPUT.read_text() == expected, "run: uv run python -m agent_relay.adapters.ahp.codegen"


def test_schemas_for_every_protocol_file_are_committed() -> None:
    for name in codegen.SCHEMA_FILES:
        document = json.loads((codegen.SCHEMA_DIR / f"{name}.schema.json").read_text())
        assert document["$defs"], name
    assert m.PROTOCOL_VERSION == codegen.PROTOCOL_VERSION


def test_snake_case_and_aliases() -> None:
    assert codegen.snake("toolCallId") == "tool_call_id"
    assert codegen.snake("_meta") == "meta"
    assert codegen.snake("from") == "from_"
    params = m.CreateSessionParams(channel="ahp-session:/x", working_directories=["file:///w"])
    assert params.wire() == {"channel": "ahp-session:/x", "workingDirectories": ["file:///w"]}


def test_every_recorded_message_validates() -> None:
    mapped = 0
    for message in TRACE_MESSAGES:
        params = message.get("params")
        if message.get("method") == "action" and isinstance(params, dict):
            action = params["action"]
            assert isinstance(action, dict)
            model = m.ACTION_MODELS.get(str(action["type"]))
            if model is not None:
                model.model_validate(action)
                mapped += 1
        result = message.get("result")
        if isinstance(result, dict) and "snapshots" in result:
            init = m.InitializeResult.model_validate(result)
            assert isinstance(init.snapshots[0].state, m.RootState)
    assert mapped >= 5


def test_unknown_fields_and_parts_are_kept() -> None:
    raw: dict[str, JsonValue] = {
        "type": "chat/responsePart",
        "turnId": "t",
        "part": {"kind": "reasoning", "id": "r", "content": "x"},
        "future": 1,
    }
    action = m.ChatResponsePartAction.model_validate(raw)
    assert action.part == {"kind": "reasoning", "id": "r", "content": "x"}
    assert action.wire()["future"] == 1
