"""Signing in to the gateway's MCP servers, and keeping them signed in.

A sign-in is asked for the first time a server needs one, or when its tokens stop working. The link goes
to the phone through `notify`. The browser then lands on the gateway's own callback (when it is the
Mac's browser) or on a loopback address the phone cannot open; in that case the owner pastes the address
back and `complete_from_url` finishes the same flow.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx

from agent_relay.gateway import oauth
from agent_relay.gateway.oauth import ClientInfo, Discovery, OAuthError, Pkce, Tokens
from agent_relay.gateway.store import Credentials, CredentialStore
from agent_relay.log import get_logger
from agent_relay.models.config import GatewayConfig

log = get_logger(__name__)

Notify = Callable[[str], Awaitable[None]]
Servers = Callable[[], Mapping[str, str]]
CALLBACK_PATH = "/oauth/callback"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


@dataclass
class _Pending:
    name: str
    discovery: Discovery
    client: ClientInfo
    pkce: Pkce
    created: float
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None


@dataclass(frozen=True)
class ServerStatus:
    name: str
    upstream: str
    state: str


class SignIn:
    def __init__(
        self,
        config: GatewayConfig,
        store: CredentialStore,
        http: httpx.AsyncClient,
        servers: Servers,
        notify: Notify | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store
        self.http = http
        self.servers = servers
        self.notify = notify
        self.clock = clock
        self._pending: dict[str, _Pending] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.config.port}{CALLBACK_PATH}"

    def upstream(self, name: str) -> str | None:
        return self.servers().get(name)

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    def _live(self, name: str) -> _Pending | None:
        pending = next((p for p in self._pending.values() if p.name == name), None)
        if pending is not None and self.clock() - pending.created > self.config.signin_wait_s:
            self._drop(pending)
            return None
        return pending

    def _drop(self, pending: _Pending) -> None:
        for state in [s for s, p in self._pending.items() if p is pending]:
            del self._pending[state]

    async def tokens(self, name: str) -> Tokens | None:
        """An access token that is good for at least the refresh margin, refreshed when needed."""
        creds = self.store.load(name)
        if creds.tokens is None:
            return None
        if not creds.tokens.expires_within(self.config.refresh_margin_s, self.clock()):
            return creds.tokens
        return await self.refresh(name)

    async def refresh(self, name: str) -> Tokens | None:
        async with self._lock(name):
            creds = self.store.load(name)
            if creds.tokens is None or creds.discovery is None or creds.client is None:
                return None
            if not creds.tokens.expires_within(self.config.refresh_margin_s, self.clock()):
                return creds.tokens
            try:
                fresh = await oauth.refresh(
                    self.http, creds.discovery, creds.client, creds.tokens, self.clock()
                )
            except OAuthError as exc:
                log.warning(f"refreshing the sign-in for MCP server {name} failed: {exc}")
                if creds.tokens.expires_within(0, self.clock()) or not creds.tokens.refresh_token:
                    self.store.save(name, creds.model_copy(update={"tokens": None}))
                    return None
                return creds.tokens
            self.store.save(name, creds.model_copy(update={"tokens": fresh}))
            return fresh

    async def invalidate(self, name: str) -> None:
        """The server turned the access token down: forget it, keep the refresh token for one retry."""
        async with self._lock(name):
            creds = self.store.load(name)
            if creds.tokens is None:
                return
            stale = creds.tokens.model_copy(update={"expires_at": 0.0})
            self.store.save(name, creds.model_copy(update={"tokens": stale}))

    async def begin(self, name: str, why: str = "needs you to sign in") -> str:
        """Start (or reuse) a sign-in for `name`, tell the phone, and return the link."""
        upstream = self.upstream(name)
        if upstream is None:
            raise OAuthError(f"`{name}` is not an MCP server of the gateway")
        existing = self._live(name)
        if existing is not None:
            return oauth.authorize_url(
                existing.discovery, existing.client, self._state_of(existing), existing.pkce
            )
        creds = self.store.load(name)
        found = creds.discovery or await oauth.discover(self.http, upstream)
        client = creds.client or await oauth.register(self.http, found, self.redirect_uri)
        self.store.save(name, Credentials(discovery=found, client=client, tokens=creds.tokens))
        pkce, state = oauth.new_pkce(), oauth.new_state()
        self._pending[state] = _Pending(name, found, client, pkce, self.clock())
        link = oauth.authorize_url(found, client, state, pkce)
        if self.notify is not None:
            await self.notify(signin_message(name, why, link))
        return link

    def _state_of(self, pending: _Pending) -> str:
        return next(s for s, p in self._pending.items() if p is pending)

    async def complete(self, state: str, code: str) -> str:
        """Finish a sign-in from its callback. Returns the server's name."""
        pending = self._pending.get(state)
        if pending is None or self.clock() - pending.created > self.config.signin_wait_s:
            raise OAuthError("that sign-in is not waiting any more: ask for a new link")
        try:
            tokens = await oauth.exchange(
                self.http, pending.discovery, pending.client, code, pending.pkce, self.clock()
            )
        except OAuthError as exc:
            pending.error = str(exc)
            pending.done.set()
            self._drop(pending)
            raise
        creds = self.store.load(pending.name)
        self.store.save(pending.name, creds.model_copy(update={"tokens": tokens}))
        pending.done.set()
        self._drop(pending)
        log.info(f"MCP server {pending.name} is signed in")
        return pending.name

    def cancel(self, state: str, reason: str) -> None:
        pending = self._pending.get(state)
        if pending is not None:
            pending.error = reason
            pending.done.set()
            self._drop(pending)

    async def complete_from_url(self, text: str) -> str | None:
        """A pasted callback address finishes the sign-in it belongs to; anything else returns None."""
        parsed = callback_params(text)
        if parsed is None:
            return None
        state, code, error = parsed
        pending = self._pending.get(state)
        if pending is None:
            return None
        if error is not None or code is None:
            self.cancel(state, error or "no code in that address")
            return f"⚠️ {pending.name}: the sign-in was not allowed ({error or 'no code in that address'})."
        try:
            name = await self.complete(state, code)
        except OAuthError as exc:
            return f"⚠️ {pending.name}: {exc}."
        return f"✅ {name} is signed in."

    async def ensure(self, name: str, hold_s: float) -> Tokens | None:
        """Tokens now, or ask for a sign-in and wait up to `hold_s` seconds for it."""
        tokens = await self.tokens(name)
        if tokens is not None:
            return tokens
        await self.begin(name)
        pending = self._live(name)
        if pending is not None and hold_s > 0:
            try:
                await asyncio.wait_for(pending.done.wait(), hold_s)
            except TimeoutError:
                return None
        return await self.tokens(name)

    def sign_out(self, name: str) -> None:
        self.store.sign_out(name)

    def status(self) -> list[ServerStatus]:
        out: list[ServerStatus] = []
        for name, upstream in sorted(self.servers().items()):
            creds = self.store.load(name)
            if self._live(name) is not None:
                state = "waiting for you to sign in"
            elif creds.tokens is None:
                state = "signed out"
            elif creds.tokens.expires_within(0, self.clock()):
                state = (
                    "signed in (access token expired, refreshes on use)"
                    if creds.tokens.refresh_token
                    else "expired"
                )
            else:
                state = "signed in"
            out.append(ServerStatus(name, upstream, state))
        return out


def signin_message(name: str, why: str, link: str) -> str:
    return (
        f"🔐 **{name}** {why}.\n\n"
        f"1. Open [the sign-in page]({link}) on this phone and allow access.\n"
        "2. The page then cannot load (it points back to the Mac). Copy its address and paste it here.\n\n"
        "If you are at the Mac, opening the link there finishes by itself."
    )


def callback_params(text: str) -> tuple[str, str | None, str | None] | None:
    """(state, code, error) of a loopback OAuth callback address inside `text`, else None."""
    for word in text.split():
        candidate = word.strip("<>()[]\"'`,.;")
        parts = urlsplit(candidate)
        if parts.scheme not in ("http", "https") or (parts.hostname or "") not in LOOPBACK_HOSTS:
            continue
        query = parse_qs(parts.query)
        state = (query.get("state") or [""])[0]
        if not state:
            continue
        code = (query.get("code") or [""])[0] or None
        error = (query.get("error") or [""])[0] or None
        return state, code, error
    return None
