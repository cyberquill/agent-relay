"""Long or richly formatted answers as one self-contained HTML file.

Shared by Webex and the PWA; it knows nothing about either. The document has inline CSS only, no
script and no external resource (a CSP says so), and links are kept as text with no `href`
(D90). Raw HTML in the agent's text is escaped. Everything returned went through `scrub`.
"""

from __future__ import annotations

import html
import re
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererProtocol
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from markdown_it.utils import OptionsDict

from agent_relay.frontends.text import neutralise_links
from agent_relay.scrub import scrub_text

PREVIEW_MAX_BYTES = 1536
POINTER = "📎 Full answer attached (answer.html)"
ANSWER_FILENAME = "answer.html"
CONTENT_TYPE = "text/html"
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
LONG_LINES = 60
MIN_FENCES = 3
DIFF_NAME = "changes.html"

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})", re.MULTILINE)
_TABLE_DELIMITER = re.compile(r"^ {0,3}\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$", re.MULTILINE)
_TABLE_HEADER = re.compile(r"^.*\|.*\n(?=[ \t]{0,3}\|?[ \t]*:?-+:?[ \t]*\|)", re.MULTILINE)

_STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--code:#f6f8fa;
--add:#e6ffec;--del:#ffebe9;--hunk:#ddf4ff;--meta:#eaeef2}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;--line:#30363d;
--code:#161b22;--add:#12261e;--del:#2d1214;--hunk:#102a43;--meta:#21262d}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
overflow-wrap:anywhere}
main{max-width:860px;margin:0 auto}
header{border-bottom:1px solid var(--line);margin-bottom:16px;padding-bottom:10px}
header h1{font-size:1.1rem;margin:0 0 4px}
header p{margin:0;color:var(--muted);font-size:.85rem}
h1,h2,h3,h4{line-height:1.25}
pre,code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.88em}
pre{background:var(--code);border:1px solid var(--line);border-radius:6px;padding:10px 12px;
overflow-x:auto;white-space:pre;overflow-wrap:normal}
code{background:var(--code);border-radius:4px;padding:.1em .3em}
pre code{background:none;padding:0}
.scroll{overflow-x:auto;margin:0 0 16px}
table{border-collapse:collapse;min-width:100%}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
th{background:var(--code)}
blockquote{margin:0 0 16px;padding:0 12px;border-left:4px solid var(--line);color:var(--muted)}
hr{border:0;border-top:1px solid var(--line)}
.img{color:var(--muted);font-style:italic}
.diff{padding:0}
.diff span{display:block;padding:0 12px;white-space:pre}
.diff .add{background:var(--add)}
.diff .del{background:var(--del)}
.diff .hunk{background:var(--hunk)}
.diff .meta{background:var(--meta);font-weight:600}
"""


@dataclass(frozen=True)
class AnswerMeta:
    app: str
    workspace: str
    model: str | None
    when: datetime
    footer: str


@dataclass(frozen=True)
class RichAnswer:
    preview: str
    html: bytes
    filename: str
    content_type: str


# when to use HTML


def _fence_count(text: str) -> int:
    """Fenced blocks, counted by their opening fences."""
    count = 0
    closer: str | None = None
    for found in _FENCE.finditer(text):
        marker = found.group(1)
        if closer is None:
            closer = marker[0] * len(marker)
            count += 1
        elif marker[0] == closer[0] and len(marker) >= len(closer):
            closer = None
    return count


def _has_table(text: str) -> bool:
    return bool(_TABLE_DELIMITER.search(text) and _TABLE_HEADER.search(text))


def is_rich(text: str) -> bool:
    """A table, three or more fenced code blocks, or more than 60 lines."""
    return _has_table(text) or _fence_count(text) >= MIN_FENCES or len(text.splitlines()) > LONG_LINES


def wants_html(text: str, *, mode: str, max_bytes: int) -> bool:
    """`never` is never; `always` is any non-empty text; `auto` is too long for a message, or rich."""
    if mode == "never" or not text.strip():
        return False
    if mode == "always":
        return True
    return len(text.encode()) > max_bytes or is_rich(text)


# markdown to inert HTML


def _links_to_text(state: StateCore) -> None:
    """Drop every link's markup and keep what it says, then where it pointed, as plain text."""
    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        kept: list[Token] = []
        targets: list[str] = []
        texts: list[list[str]] = []
        for child in block.children:
            if child.type == "link_open":
                targets.append(str(child.attrGet("href") or ""))
                texts.append([])
                continue
            if child.type == "link_close" and targets:
                target = targets.pop()
                shown = "".join(texts.pop())
                if target and target != shown:
                    kept.append(_text(" ("))
                    code = Token("code_inline", "code", 0)
                    code.content = target
                    code.markup = "`"
                    kept.append(code)
                    kept.append(_text(")"))
                continue
            if texts and child.type in ("text", "code_inline"):
                texts[-1].append(child.content)
            kept.append(child)
        block.children = kept


def _text(content: str) -> Token:
    token = Token("text", "", 0)
    token.content = content
    return token


def _image(
    self: RendererProtocol,
    tokens: Sequence[Token],
    idx: int,
    options: OptionsDict,
    env: MutableMapping[str, Any],
) -> str:
    alt = tokens[idx].content or "image"
    return f'<span class="img">[image: {html.escape(alt)}]</span>'


def _table_open(
    self: RendererProtocol,
    tokens: Sequence[Token],
    idx: int,
    options: OptionsDict,
    env: MutableMapping[str, Any],
) -> str:
    return '<div class="scroll"><table>\n'


def _table_close(
    self: RendererProtocol,
    tokens: Sequence[Token],
    idx: int,
    options: OptionsDict,
    env: MutableMapping[str, Any],
) -> str:
    return "</table></div>\n"


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    md.core.ruler.push("links_to_text", _links_to_text)
    md.add_render_rule("image", _image)
    md.add_render_rule("table_open", _table_open)
    md.add_render_rule("table_close", _table_close)
    return md


def _document(title: str, meta: AnswerMeta, body: str) -> bytes:
    when = meta.when.strftime("%Y-%m-%d %H:%M %Z").strip()
    facts = [meta.workspace, meta.model or "default model", when, meta.footer]
    line = " · ".join(html.escape(f) for f in facts if f)
    page = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(CSP)}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{html.escape(title)}</title>\n<style>{_STYLE}</style></head>\n"
        f"<body><main><header><h1>{html.escape(title)}</h1><p>{line}</p></header>\n"
        f"{body}</main></body></html>\n"
    )
    return scrub_text(page).encode()


def _title(meta: AnswerMeta, what: str) -> str:
    return f"{what} · {meta.app}"


# the preview


def _blocks(text: str) -> list[str]:
    """Paragraph-sized pieces of `text`, with fenced code and tables left out."""
    kept: list[str] = []
    fence: str | None = None
    current: list[str] = []
    for line in [*text.splitlines(), ""]:
        opening = _FENCE.match(line)
        if fence is not None:
            if opening and opening.group(1)[0] == fence[0] and len(opening.group(1)) >= len(fence):
                fence = None
            continue
        if opening:
            fence = opening.group(1)
            continue
        if line.strip():
            current.append(line)
            continue
        if current:
            kept.append("\n".join(current))
            current = []
    return [b for b in kept if not _is_table_block(b)]


def _is_table_block(block: str) -> bool:
    lines = block.splitlines()
    return len(lines) >= 2 and "|" in lines[0] and bool(_TABLE_DELIMITER.match(lines[1]))


def _summary(text: str) -> str:
    """The first paragraph; a heading brings the paragraph after it along."""
    blocks = _blocks(text)
    if not blocks:
        return text.strip()
    first = blocks[0]
    if first.lstrip().startswith("#") and len(blocks) > 1:
        return f"{first}\n\n{blocks[1]}"
    return first


def _clip(text: str, limit: int) -> str:
    if len(text.encode()) <= limit:
        return text
    cut = text.encode()[: max(limit - 3, 0)].decode(errors="ignore").rstrip()
    clipped = f"{cut}…"
    return f"{clipped}`" if clipped.count("`") % 2 else clipped


def _preview(text: str) -> str:
    tail = f"\n\n{POINTER}"
    budget = PREVIEW_MAX_BYTES - len(tail.encode())
    head = _clip(neutralise_links(scrub_text(_summary(text))), budget)
    return f"{head}{tail}"


# public API


def render_answer(text: str, meta: AnswerMeta) -> RichAnswer:
    body = _markdown().render(scrub_text(text))
    return RichAnswer(
        preview=scrub_text(_preview(text)),
        html=_document(_title(meta, "Answer"), meta, body),
        filename=ANSWER_FILENAME,
        content_type=CONTENT_TYPE,
    )


_DIFF_HEADERS = (
    "+++",
    "---",
    "diff ",
    "index ",
    "new file",
    "deleted file",
    "similarity",
    "rename ",
    "old mode",
    "new mode",
    "Binary",
)


def _diff_classes(lines: Sequence[str]) -> list[str]:
    """One class per line. Inside a hunk `---` and `+++` are content, not file headers."""
    classes: list[str] = []
    in_hunk = False
    for line in lines:
        if line.startswith("diff "):
            in_hunk = False
        if line.startswith("@@"):
            in_hunk = True
            classes.append("hunk")
        elif in_hunk and line.startswith("+"):
            classes.append("add")
        elif in_hunk and line.startswith("-"):
            classes.append("del")
        elif not in_hunk and line.startswith(_DIFF_HEADERS):
            classes.append("meta")
        else:
            classes.append("ctx")
    return classes


def render_diff(diff: str, meta: AnswerMeta) -> bytes:
    """A unified diff as the same page: added, removed, hunk and header lines in colour."""
    lines = diff.splitlines()
    rows = [
        f'<span class="{kind}">{html.escape(line) or " "}</span>'
        for kind, line in zip(_diff_classes(lines), lines, strict=True)
    ]
    body = f'<pre class="diff"><code>{"".join(rows)}</code></pre>\n'
    return _document(_title(meta, "Changes"), meta, body)
