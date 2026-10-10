"""Generate Pydantic models for the AHP subset the adapter uses, from the published JSON Schemas.

Run `python -m agent_relay.adapters.ahp.codegen` after replacing the schema files for a new
protocol version. `tests/adapters/test_ahp_schema.py` fails when `models.py` is out of date.

Rules: integer enums are bitsets in AHP (`SessionStatus`), so they become `int`. Selected object
definitions become frozen models (camelCase fields get snake_case names with aliases; unknown fields
are kept). References to scalar definitions are inlined. References to object
definitions outside the subset become `JsonValue`, so nothing the server adds can break parsing.
"""

from __future__ import annotations

import json
import keyword
import re
import subprocess
import sys
from pathlib import Path

from pydantic import JsonValue

PROTOCOL_VERSION = "0.10.0"
HERE = Path(__file__).resolve().parent
SCHEMA_DIR = HERE / "schema" / PROTOCOL_VERSION
OUTPUT = HERE / "models.py"
SCHEMA_FILES = ("actions", "commands", "notifications", "state", "errors")

SELECTED: tuple[str, ...] = (
    # commands
    "Implementation",
    "InitializeParams",
    "InitializeResult",
    "SubscribeParams",
    "SubscribeResult",
    "UnsubscribeParams",
    "DispatchActionParams",
    "CreateSessionParams",
    "CreateChatParams",
    "DisposeSessionParams",
    "ListSessionsParams",
    "ListSessionsResult",
    # state
    "Snapshot",
    "RootState",
    "AgentInfo",
    "SessionModelInfo",
    "SessionSummary",
    "SessionChatSummary",
    # messages and parts
    "ActionEnvelope",
    "Message",
    "MessageOrigin",
    "ModelSelection",
    "AgentSelection",
    "ResponsePart",
    "MarkdownResponsePart",
    "ErrorResponsePart",
    "ErrorInfo",
    "ToolCallResult",
    "ConfirmationOption",
    # chat actions
    "ChatTurnStartedAction",
    "ChatDeltaAction",
    "ChatResponsePartAction",
    "ChatToolCallStartAction",
    "ChatToolCallReadyAction",
    "ChatToolCallApprovedAction",
    "ChatToolCallDeniedAction",
    "ChatToolCallCompleteAction",
    "ChatTurnCompleteAction",
    "ChatTurnCancelledAction",
    "ChatErrorAction",
    "ChatInputRequestedAction",
    "ChatInputCompletedAction",
    "ChatPendingMessageSetAction",
    "SessionTitleChangedAction",
    "SessionChatAddedAction",
    # input requests
    "ChatInputRequest",
    "ChatInputQuestion",
    "ChatInputTextQuestion",
    "ChatInputNumberQuestion",
    "ChatInputBooleanQuestion",
    "ChatInputSingleSelectQuestion",
    "ChatInputMultiSelectQuestion",
    "ChatInputOption",
    "ChatInputAnswer",
    "ChatInputAnswered",
    "ChatInputSkipped",
    "ChatInputAnswerValue",
    "ChatInputTextAnswerValue",
    "ChatInputNumberAnswerValue",
    "ChatInputBooleanAnswerValue",
    "ChatInputSelectedAnswerValue",
    "ChatInputSelectedManyAnswerValue",
)

type Schema = dict[str, JsonValue]

_SCALARS = {"string": "str", "number": "int | float", "integer": "int", "boolean": "bool", "null": "None"}


def load_defs(schema_dir: Path = SCHEMA_DIR) -> dict[str, Schema]:
    merged: dict[str, Schema] = {}
    for name in SCHEMA_FILES:
        document = json.loads((schema_dir / f"{name}.schema.json").read_text())
        for key, value in document.get("$defs", {}).items():
            if isinstance(value, dict):
                merged.setdefault(key, value)
    return merged


def snake(name: str) -> str:
    cleaned = name.lstrip("_")
    out = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", cleaned).replace("-", "_").lower()
    if not out.isidentifier():
        out = re.sub(r"\W", "_", out)
    if keyword.iskeyword(out) or out in ("model_config", "model_fields"):
        out = f"{out}_"
    return out


def pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]", name) if part)


def _union(members: list[str]) -> str:
    seen: list[str] = []
    for member in members:
        for piece in member.split(" | "):
            if piece not in seen:
                seen.append(piece)
    if "JsonValue" in seen:
        seen = [m for m in seen if m != "JsonValue"] + ["JsonValue"]
    return " | ".join(seen)


def _fallback_union(members: list[str]) -> str:
    """A union whose JsonValue fallback is tried last; smart mode would prefer it for any dict."""
    joined = _union(members)
    pieces = joined.split(" | ")
    if "JsonValue" in pieces and len(pieces) > 1:
        return f'Annotated[{joined}, Field(union_mode="left_to_right")]'
    return joined


class Generator:
    def __init__(self, defs: dict[str, Schema], selected: tuple[str, ...] = SELECTED) -> None:
        missing = [name for name in selected if name not in defs]
        if missing:
            raise KeyError(f"schema has no definitions {missing}")
        self.defs = defs
        self.selected = selected
        self.classes: list[str] = []
        self.aliases: list[str] = []
        self.action_types: dict[str, list[str]] = {}
        self._emitted: set[str] = set()

    def _ref_name(self, schema: Schema) -> str | None:
        ref = schema.get("$ref")
        return ref.rsplit("/", 1)[-1] if isinstance(ref, str) else None

    def scalar(self, schema: Schema) -> str | None:
        """A Python annotation when the schema is scalar (after following refs), else None."""
        ref = self._ref_name(schema)
        if ref is not None:
            target = self.defs.get(ref)
            return self.scalar(target) if target is not None else None
        if "const" in schema:
            return f"Literal[{json.dumps(schema['const'])}]"
        enum = schema.get("enum")
        if isinstance(enum, list) and enum and all(type(v) is int for v in enum):
            return "int"
        if isinstance(enum, list):
            return f"Literal[{', '.join(json.dumps(v) for v in enum)}]"
        kind = schema.get("type")
        if isinstance(kind, str) and kind in _SCALARS:
            return _SCALARS[kind]
        if isinstance(kind, list) and all(isinstance(k, str) and k in _SCALARS for k in kind):
            return _union([_SCALARS[str(k)] for k in kind])
        for key in ("oneOf", "anyOf"):
            options = schema.get(key)
            if isinstance(options, list) and options:
                parts = [self.scalar(o) if isinstance(o, dict) else None for o in options]
                if all(p is not None for p in parts):
                    literals = [p for p in parts if p is not None and p.startswith("Literal[")]
                    if len(literals) == len(parts):
                        return f"Literal[{', '.join(p[8:-1] for p in literals)}]"
                    return _union([p for p in parts if p is not None])
        return None

    def annotation(self, schema: JsonValue, owner: str, field: str) -> str:
        if not isinstance(schema, dict) or not schema:
            return "JsonValue"
        ref = self._ref_name(schema)
        if ref is not None:
            if ref in self.selected:
                return ref
            return self.scalar(schema) or "JsonValue"
        found = self.scalar(schema)
        if found is not None:
            return found
        for key in ("oneOf", "anyOf"):
            options = schema.get(key)
            if isinstance(options, list):
                return _fallback_union([self.annotation(o, owner, field) for o in options])
        kind = schema.get("type")
        if kind == "array":
            return f"list[{self.annotation(schema.get('items'), owner, field)}]"
        if kind == "object" or "properties" in schema:
            properties = schema.get("properties")
            if isinstance(properties, dict) and properties:
                nested = f"{owner}{pascal(field)}"
                self.emit_class(nested, schema)
                return nested
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict) and extra:
                return f"dict[str, {self.annotation(extra, owner, field)}]"
            return "dict[str, JsonValue]"
        return "JsonValue"

    def emit_class(self, name: str, schema: Schema) -> None:
        if name in self._emitted:
            return
        self._emitted.add(name)
        properties = schema.get("properties")
        props = properties if isinstance(properties, dict) else {}
        required_raw = schema.get("required")
        required = (
            {r for r in required_raw if isinstance(r, str)} if isinstance(required_raw, list) else set()
        )
        lines = [f"class {name}(AhpModel):"]
        for raw_name, prop in props.items():
            field = snake(raw_name)
            ann = self.annotation(prop, name, raw_name)
            alias = f'alias="{raw_name}"' if field != raw_name else ""
            const = prop.get("const") if isinstance(prop, dict) else None
            if raw_name == "type" and isinstance(const, str):
                self.action_types.setdefault(const, []).append(name)
                lines.append(f"    {field}: {ann} = {json.dumps(const)}")
            elif raw_name in required:
                lines.append(f"    {field}: {ann} = Field({alias})" if alias else f"    {field}: {ann}")
            else:
                optional = ann if ann.endswith("None") or ann == "JsonValue" else f"{ann} | None"
                extra = f", {alias}" if alias else ""
                lines.append(f"    {field}: {optional} = Field(default=None{extra})")
        if len(lines) == 1:
            lines.append('    """Declares no fields; every field the server sends is kept."""')
        self.classes.append("\n".join(lines))

    def emit(self, name: str) -> None:
        schema = self.defs[name]
        props = schema.get("properties")
        if isinstance(props, dict) or schema.get("type") == "object":
            self.emit_class(name, schema)
            return
        ann = self.annotation({k: v for k, v in schema.items() if k != "$ref"}, name, "")
        self.aliases.append(f"type {name} = {ann}")

    def render(self) -> str:
        for name in self.selected:
            self.emit(name)
        unique = {t: names[0] for t, names in sorted(self.action_types.items()) if len(names) == 1}
        class_names = [block.split("(", 1)[0].removeprefix("class ") for block in self.classes]
        parts = [
            f'"""AHP {PROTOCOL_VERSION} models generated by `codegen.py` from the schemas. Do not edit."""',
            "",
            "from __future__ import annotations",
            "",
            "from typing import Annotated, Literal",
            "",
            "from pydantic import BaseModel, ConfigDict, Field, JsonValue",
            "",
            f'PROTOCOL_VERSION = "{PROTOCOL_VERSION}"',
            "",
            "",
            "class AhpModel(BaseModel):",
            '    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)',
            "",
            "    def wire(self) -> dict[str, JsonValue]:",
            '        return self.model_dump(mode="json", by_alias=True, exclude_none=True)',
            "",
            "",
            *[f"{alias}\n" for alias in self.aliases],
            "",
            "\n\n\n".join(self.classes),
            "",
            "",
            *[f"{cls}.model_rebuild()" for cls in class_names],
            "",
            "ACTION_MODELS: dict[str, type[AhpModel]] = {",
            *[f'    "{t}": {cls},' for t, cls in unique.items()],
            "}",
            "",
        ]
        return "\n".join(parts)


def ruff_format(source: str) -> str:
    """The committed file is ruff-formatted, so the freshness check compares formatted text."""
    done = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--stdin-filename", str(OUTPUT), "-"],
        input=source,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def generate(schema_dir: Path = SCHEMA_DIR) -> str:
    return ruff_format(Generator(load_defs(schema_dir)).render())


def main() -> int:
    OUTPUT.write_text(generate())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
