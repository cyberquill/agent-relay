"""AHP channel URIs (spike S1, correction 1): chats are `ahp-chat://<id>/<base64url(session URI)>`."""

from __future__ import annotations

import base64
from urllib.parse import unquote, urlparse

from agent_relay import paths

ROOT = "ahp-root://"
SESSION_PREFIX = "ahp-session:/"
CHAT_PREFIX = "ahp-chat://"
DEFAULT_CHAT = "default"


def session_uri(session_id: str) -> str:
    return f"{SESSION_PREFIX}{session_id}"


def session_id(uri: str) -> str | None:
    return uri.removeprefix(SESSION_PREFIX) if uri.startswith(SESSION_PREFIX) else None


def chat_uri(chat_id: str, session: str) -> str:
    encoded = base64.urlsafe_b64encode(session.encode()).decode().rstrip("=")
    return f"{CHAT_PREFIX}{chat_id}/{encoded}"


def chat_session(uri: str) -> str | None:
    """The session URI a chat URI belongs to."""
    if not uri.startswith(CHAT_PREFIX):
        return None
    _, _, encoded = uri.removeprefix(CHAT_PREFIX).partition("/")
    if not encoded:
        return None
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def file_uri(path: str) -> str:
    return paths.expand_user(path).resolve().as_uri()


def file_path(uri: str) -> str | None:
    parsed = urlparse(uri)
    return unquote(parsed.path) if parsed.scheme == "file" else None
