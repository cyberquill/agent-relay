"""Doctor results: one `Check` per finding, rendered for a terminal, Webex markdown or JSON."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_relay.scrub import scrub_text

Status = Literal["ok", "info", "warn", "fail"]
ICONS: dict[Status, str] = {"ok": "✅", "info": "ℹ️", "warn": "⚠️", "fail": "❌"}


class Check(BaseModel):
    model_config = ConfigDict(frozen=True)

    area: str
    name: str
    status: Status
    detail: str
    fix: str | None = None


class Report(BaseModel):
    checks: list[Check] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    def counts(self) -> dict[Status, int]:
        found: dict[Status, int] = {"ok": 0, "info": 0, "warn": 0, "fail": 0}
        for check in self.checks:
            found[check.status] += 1
        return found

    def render(self) -> str:
        lines: list[str] = []
        area = ""
        for check in self.checks:
            if check.area != area:
                area = check.area
                lines.append(f"**{area}**")
            line = f"- {ICONS[check.status]} {check.name} — {check.detail}"
            if check.fix and check.status in ("warn", "fail"):
                line += f" → {check.fix}"
            lines.append(line)
        c = self.counts()
        lines.append(f"{c['fail']} failing, {c['warn']} warning(s), {c['ok']} ok")
        return scrub_text("\n".join(lines))

    def to_json(self) -> str:
        return scrub_text(json.dumps({"ok": self.ok, "counts": self.counts(), **self.model_dump()}, indent=2))
