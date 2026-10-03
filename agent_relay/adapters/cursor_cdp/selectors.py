"""The composer selectors, loaded from `selectors.json` so a Cursor update is a data change."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

SELECTORS_FILE = Path(__file__).with_name("selectors.json")
NAMES = (
    "composer",
    "send",
    "stop",
    "new_chat",
    "model_picker",
    "model_option",
    "messages",
    "assistant_message",
)


class Selector(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    css: str
    required: bool
    purpose: str = ""


class SelectorSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    selectors: dict[str, Selector]

    def css(self, name: str) -> str:
        return self.selectors[name].css

    def required(self) -> list[str]:
        return [name for name, sel in self.selectors.items() if sel.required]


def load_selectors(source: Path = SELECTORS_FILE) -> SelectorSet:
    loaded = SelectorSet.model_validate(json.loads(source.read_text(encoding="utf-8")))
    missing = [name for name in NAMES if name not in loaded.selectors]
    if missing:
        raise ValueError(f"{source} is missing selectors: {', '.join(missing)}")
    return loaded
