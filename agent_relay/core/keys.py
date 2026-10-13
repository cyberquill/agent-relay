"""Signal names and registry flag keys shared by the core parts. No imports, so nothing cycles."""

from __future__ import annotations

RELEASE_KEY = "release:{session_key}"


def pending_key(request_id: str) -> str:
    return f"pending:{request_id}"


def session_signal(session_key: str) -> str:
    return f"session:{session_key}"
