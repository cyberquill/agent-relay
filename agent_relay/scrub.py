"""Secret redaction for everything logged or posted outward."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

REDACTED = "<redacted>"

_SECRET_KEY = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|tkn)", re.I)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\b[sp]k_(?:live|test)_[0-9A-Za-z]{16,}"),
    re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"[A-Za-z0-9]{40,}_[A-Z0-9]{4}_[0-9a-f-]{36}"),
)

_KEY_VALUE = re.compile(
    r"""(?ix)
    (?P<key>["']?[\w.-]*(?:token|secret|password|passwd|api[_-]?key|tkn)[\w.-]*["']?)
    (?P<sep>\s*[:=]\s*)
    (?P<quote>["']?)
    (?P<value>[^"'\s,&}]{4,})
    """
)
_URL_CREDS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/\s:@]+):(?P<pw>[^/\s@]+)@", re.I)
_HIGH_ENTROPY = re.compile(r"(?<![A-Za-z0-9+_-])[A-Za-z0-9+_-]{32,}={0,2}(?![A-Za-z0-9+_-])")


def _looks_random(candidate: str) -> bool:
    return (
        any(c.isupper() for c in candidate)
        and any(c.islower() for c in candidate)
        and sum(c.isdigit() for c in candidate) >= 2
    )


def scrub_text(text: str) -> str:
    out = text
    for pattern in _PATTERNS:
        out = pattern.sub(REDACTED, out)
    out = _URL_CREDS.sub(lambda m: f"{m['scheme']}{m['user']}:{REDACTED}@", out)
    out = _KEY_VALUE.sub(lambda m: f"{m['key']}{m['sep']}{m['quote']}{REDACTED}", out)
    out = _HIGH_ENTROPY.sub(lambda m: REDACTED if _looks_random(m.group(0)) else m.group(0), out)
    return out


type Scrubbable = str | int | float | bool | None | Mapping[str, Scrubbable] | Sequence[Scrubbable]


def scrub_obj(value: Scrubbable) -> Scrubbable:
    """Redact secret-named keys and secret-shaped strings anywhere in a JSON-like value."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if _SECRET_KEY.search(k) and isinstance(v, str) else scrub_obj(v))
            for k, v in value.items()
        }
    if isinstance(value, Sequence):
        return [scrub_obj(v) for v in value]
    return value
