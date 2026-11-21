"""The pass/fail/skip table: printed to stdout and written as markdown for `docs/operations.md`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

type Status = Literal["PASS", "FAIL", "SKIP"]

DETAIL_CHARS = 240


@dataclass(frozen=True)
class Result:
    label: str  # "2", "2b", ...
    title: str
    app: str  # "-" for scenarios that are not per app
    status: Status
    detail: str
    seconds: float


def _cell(text: str) -> str:
    one_line = " ".join(text.split()).replace("|", "\\|")
    return one_line if len(one_line) <= DETAIL_CHARS else f"{one_line[: DETAIL_CHARS - 1]}…"


@dataclass
class Report:
    started: str
    results: list[Result] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, result: Result) -> None:
        self.results.append(result)

    def count(self, status: Status) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def failed(self) -> bool:
        return self.count("FAIL") > 0

    def exit_code(self) -> int:
        return 1 if self.failed or not self.results else 0

    def markdown(self) -> str:
        lines = [
            f"### Phone E2E, {self.started}",
            "",
            "| # | Scenario | App | Result | Detail | Seconds |",
            "|---|---|---|---|---|---|",
        ]
        for r in self.results:
            cells = [r.label, _cell(r.title), r.app, r.status, _cell(r.detail), f"{r.seconds:.0f}"]
            lines.append(f"| {' | '.join(cells)} |")
        lines += [
            "",
            f"{self.count('PASS')} passed, {self.count('FAIL')} failed, {self.count('SKIP')} skipped.",
        ]
        for note in self.notes:
            lines.append(f"- {_cell(note)}")
        return "\n".join(lines) + "\n"

    def text(self) -> str:
        """The console form: one line per result, then the totals."""
        width = max((len(r.title) for r in self.results), default=0)
        lines = [
            f"{r.status:<4}  {r.label:<3} {r.title:<{width}}  {r.app:<14} {_cell(r.detail)}"
            for r in self.results
        ]
        lines.append(
            f"{self.count('PASS')} passed, {self.count('FAIL')} failed, {self.count('SKIP')} skipped"
        )
        lines += self.notes
        return "\n".join(lines) + "\n"
