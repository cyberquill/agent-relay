"""JSON with comments and trailing commas (VS Code `mcp.json`, `opencode.jsonc`), parsed with the stdlib.

Comments are removed outside strings only, so URLs such as `https://…` survive.
"""

from __future__ import annotations

import json
import re

from pydantic import JsonValue

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def strip_comments(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated /* comment")
            i = end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _drop_trailing_commas(text: str) -> str:
    """Trailing commas outside strings; strings are masked first so their contents never match."""
    masked: list[str] = []
    spans: list[str] = []
    for part in re.split(r'("(?:\\.|[^"\\])*")', text):
        if part.startswith('"') and part.endswith('"') and len(part) >= 2:
            spans.append(part)
            masked.append(f'"\x00{len(spans) - 1}\x00"')
        else:
            masked.append(part)
    cleaned = _TRAILING_COMMA.sub(r"\1", "".join(masked))
    return re.sub(r'"\x00(\d+)\x00"', lambda m: spans[int(m.group(1))], cleaned)


def loads(text: str) -> JsonValue:
    if not text.strip():
        return {}
    value: JsonValue = json.loads(_drop_trailing_commas(strip_comments(text)))
    return value


def dumps(value: JsonValue) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"
