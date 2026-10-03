"""docs/acceptance.md covers every AC in the plan, and every test it cites exists and names that AC."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "PLAN.md"
ACCEPTANCE = ROOT / "docs" / "acceptance.md"
ROW = re.compile(r"^- \[(?P<box>[ x])\] \*\*(?P<ac>AC-\d+)\*\* (?P<rest>.+)$", re.MULTILINE)
CITED = re.compile(r"`((?:tests|extensions)/[^`]+)`")


def plan_ids() -> list[str]:
    return sorted(
        set(re.findall(r"\[(AC-\d+)\]", PLAN.read_text(encoding="utf-8"))), key=lambda a: int(a[3:])
    )


def rows() -> list[re.Match[str]]:
    return list(ROW.finditer(ACCEPTANCE.read_text(encoding="utf-8")))


def test_every_plan_ac_appears_exactly_once() -> None:
    listed = [row["ac"] for row in rows()]
    assert sorted(listed, key=lambda a: int(a[3:])) == plan_ids()
    assert len(listed) == len(set(listed))
    assert plan_ids() == [f"AC-{n}" for n in range(1, 41)]


def test_every_cited_test_exists_and_names_its_ac() -> None:
    for row in rows():
        cited = CITED.findall(row["rest"])
        assert cited, f"{row['ac']} cites no test"
        for name in cited:
            file = ROOT / name
            assert file.is_file(), f"{row['ac']}: {name} does not exist"
            text = file.read_text(encoding="utf-8")
            assert re.search(rf"{row['ac']}\b", text), f"{name} never names {row['ac']}"


def test_the_security_map_cites_tests_that_exist() -> None:
    """docs/security.md cites `file::test` and `::test` (same file as the previous cite)."""
    text = (ROOT / "docs" / "security.md").read_text(encoding="utf-8")
    current: Path | None = None
    cited = 0
    for match in re.finditer(r"`(tests/[^`:]+\.py)?(?:::(\w+))?`", text):
        name, test = match.group(1), match.group(2)
        if name:
            current = ROOT / name
            assert current.is_file(), name
        if test:
            assert current is not None
            assert re.search(rf"def {test}\b", current.read_text(encoding="utf-8")), f"{current}::{test}"
            cited += 1
    assert cited >= 15


def test_an_open_box_says_what_it_waits_for() -> None:
    for row in rows():
        if row["box"] == " ":
            assert "(pending C15:" in row["rest"], f"{row['ac']} is open without a reason"
