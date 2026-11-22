"""A gateway wired to a fake OAuth MCP server, shared by the gateway and front-end tests."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from agent_relay.gateway.signin import SignIn
from agent_relay.gateway.store import KEY_HEADER, CredentialStore
from agent_relay.models.config import GatewayConfig
from tests.fakes.oauth_server import UPSTREAM, FakeOAuthServer

KEY = "gateway-key-for-tests"
NAME = "atlassian"


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@dataclass
class Rig:
    server: FakeOAuthServer
    signin: SignIn
    store: CredentialStore
    clock: Clock
    posted: list[str] = field(default_factory=list)
    servers: dict[str, str] = field(default_factory=dict)

    async def sign_in(self) -> str:
        """Run a whole sign-in against the fake server; returns the state used."""
        link = await self.signin.begin(NAME)
        query = parse_qs(urlsplit(link).query)
        state = query["state"][0]
        code = self.server.grant_code(query["code_challenge"][0])
        await self.signin.complete(state, code)
        return state


def make_rig(clock: Clock) -> Rig:
    server = FakeOAuthServer()
    posted: list[str] = []
    servers = {NAME: UPSTREAM}

    async def notify(text: str) -> None:
        posted.append(text)

    config = GatewayConfig(enabled=True, signin_hold_s=0)
    store = CredentialStore()
    signin = SignIn(config, store, server.client(), lambda: servers, notify, clock)
    return Rig(server, signin, store, clock, posted, servers)


def keyed(**extra: str) -> dict[str, str]:
    return {KEY_HEADER: KEY, **extra}
