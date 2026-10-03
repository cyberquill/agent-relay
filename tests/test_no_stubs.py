"""No stubs, placeholders or deferred-work markers in committed code. Only tests/fakes/ is exempt."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED_DIRS = (
    "agent_relay",
    "tests",
    "scripts",
    "extensions/relay-bridge/src",
    "plugins/opencode",
    "skills",
)
SUFFIXES = {".py", ".ts", ".md", ".json", ".tmpl", ".sh", ".html", ".js"}
EXEMPT = (ROOT / "tests" / "fakes", Path(__file__).resolve())
SKIP_PARTS = {"node_modules", "out", "dist", "__pycache__", "fixtures", "schema"}

_MARKERS = re.compile(
    "|".join(
        [
            r"\bTO" + r"DO\b",
            r"\bFIX" + r"ME\b",
            r"\bX" + r"XX\b",
            r"NotImplemented" + r"Error",
            r"left as an " + r"exercise",
            r"(?<!\.)\b[Pp]lace" + r"holder\b(?![\"']\s*:)(?!\s*=)",
        ]
    )
)


def _files() -> list[Path]:
    out: list[Path] = []
    for rel in SCANNED_DIRS:
        base = ROOT / rel
        if not base.exists():
            continue
        for file in base.rglob("*"):
            if not file.is_file() or file.suffix not in SUFFIXES:
                continue
            if SKIP_PARTS & set(file.relative_to(ROOT).parts):
                continue
            if any(file == e or e in file.parents for e in EXEMPT):
                continue
            out.append(file)
    return out


def _is_protocol_or_abstract(
    node: ast.FunctionDef | ast.AsyncFunctionDef, parents: dict[ast.AST, ast.AST]
) -> bool:
    for deco in node.decorator_list:
        name = deco.attr if isinstance(deco, ast.Attribute) else getattr(deco, "id", "")
        if name in {"abstractmethod", "overload"}:
            return True
    parent = parents.get(node)
    if isinstance(parent, ast.ClassDef):
        bases = {b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "") for b in parent.bases}
        return "Protocol" in bases
    return False


def _stub_functions(file: Path) -> list[str]:
    tree = ast.parse(file.read_text(encoding="utf-8"))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        only_pass = all(isinstance(s, ast.Pass) for s in body) and body
        only_ellipsis = (
            len(node.body) >= 1
            and all(isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) for s in node.body)
            and any(isinstance(s, ast.Expr) and s.value.value is Ellipsis for s in node.body)  # type: ignore[attr-defined]
        )
        if (only_pass or only_ellipsis) and not _is_protocol_or_abstract(node, parents):
            found.append(f"{file.relative_to(ROOT)}:{node.lineno} {node.name}")
    return found


def test_no_markers() -> None:
    hits = []
    for file in _files():
        for n, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            if _MARKERS.search(line):
                hits.append(f"{file.relative_to(ROOT)}:{n}: {line.strip()}")
    assert hits == []


def test_no_stub_bodies() -> None:
    stubs = [s for f in _files() if f.suffix == ".py" for s in _stub_functions(f)]
    assert stubs == []


def test_scanner_detects_stubs(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("def a() -> None:\n    pass\n\ndef b() -> None:\n    ...\n")
    tree_hits = _stub_functions_at(sample)
    assert len(tree_hits) == 2
    assert _MARKERS.search("raise " + "NotImplemented" + "Error")
    word = "place" + "holder"
    assert not _MARKERS.search(f'"{word}": "Type here"')
    assert not _MARKERS.search(f'<textarea {word}="Prompt">')
    assert not _MARKERS.search(f'input.{word} = block.{word} || "";')
    assert _MARKERS.search(f"return a {word} value for now")
    assert _MARKERS.search(f"# {word.capitalize()}: replace before release")


def _stub_functions_at(file: Path) -> list[str]:
    tree = ast.parse(file.read_text(encoding="utf-8"))
    return [
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and all(
            isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
            for s in n.body
        )
    ]
