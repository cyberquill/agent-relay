"""Make links in agent output inert, so Webex builds no preview that could carry data out.

A bare URL becomes a code span, `[text](url)` becomes `text (`url`)`. Code spans and fenced blocks
are left alone: Webex does not link or preview inside them.
"""

from __future__ import annotations

import re

_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.DOTALL)
_LINK = re.compile(
    r"""
    !?\[(?P<label>[^\]\n]*)\]\(\s*<?(?P<target>https?://[^\s)>]+)>?(?:\s+"[^"\n]*")?\s*\)
    | <(?P<auto>https?://[^\s<>]+)>
    | (?P<bare>https?://[^\s<>`]+)
    """,
    re.VERBOSE | re.IGNORECASE,
)
_TRAILING = ".,;:!?)]}'\"*_"


def _span(url: str) -> str:
    return f"`{url}`"


def _replace(match: re.Match[str]) -> str:
    if (target := match.group("target")) is not None:
        label = match.group("label").strip()
        return f"{label} ({_span(target)})" if label else _span(target)
    if (auto := match.group("auto")) is not None:
        return _span(auto)
    bare = match.group("bare")
    url = bare.rstrip(_TRAILING)
    return f"{_span(url)}{bare[len(url) :]}"


def neutralise_links(text: str) -> str:
    out: list[str] = []
    last = 0
    for code in _CODE.finditer(text):
        out.append(_LINK.sub(_replace, text[last : code.start()]))
        out.append(code.group())
        last = code.end()
    out.append(_LINK.sub(_replace, text[last:]))
    return "".join(out)
