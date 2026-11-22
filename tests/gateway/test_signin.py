"""The sign-in flow: link to the phone, callback or pasted address, refresh, expiry, status."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_relay.gateway.oauth import OAuthError
from agent_relay.gateway.signin import callback_params
from tests.fakes.oauth_server import UPSTREAM
from tests.gateway.rig import NAME, Rig


def _query(link: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}


async def test_begin_posts_the_link_once_and_reuses_the_waiting_sign_in(rig: Rig) -> None:
    first = await rig.signin.begin(NAME)
    second = await rig.signin.begin(NAME)
    assert first == second
    assert len(rig.posted) == 1
    assert first in rig.posted[0]
    assert "paste it here" in rig.posted[0]
    assert rig.signin.status()[0].state == "waiting for you to sign in"


async def test_an_unknown_server_cannot_sign_in(rig: Rig) -> None:
    with pytest.raises(OAuthError, match="not an MCP server of the gateway"):
        await rig.signin.begin("nope")


async def test_a_callback_stores_tokens_and_the_client_is_registered_once(rig: Rig) -> None:
    await rig.sign_in()
    tokens = await rig.signin.tokens(NAME)
    assert tokens is not None and tokens.access_token == "access-1"
    await rig.signin.begin(NAME)
    assert len(rig.server.registered) == 1
    assert rig.signin.status()[0].state == "waiting for you to sign in"


async def test_a_pasted_address_finishes_the_sign_in(rig: Rig) -> None:
    link = await rig.signin.begin(NAME)
    query = _query(link)
    code = rig.server.grant_code(query["code_challenge"])
    pasted = f"here http://127.0.0.1:33418/oauth/callback?code={code}&state={query['state']} thanks"
    assert await rig.signin.complete_from_url(pasted) == f"✅ {NAME} is signed in."
    assert (await rig.signin.tokens(NAME)) is not None


async def test_an_address_for_no_waiting_sign_in_is_not_taken(rig: Rig) -> None:
    assert await rig.signin.complete_from_url("http://127.0.0.1:1/cb?code=c&state=unknown") is None
    assert await rig.signin.complete_from_url("see https://example.com/?code=a&state=b") is None
    assert await rig.signin.complete_from_url("hello") is None


async def test_a_denied_sign_in_is_reported_and_closed(rig: Rig) -> None:
    link = await rig.signin.begin(NAME)
    state = _query(link)["state"]
    reply = await rig.signin.complete_from_url(f"http://localhost:1/cb?error=access_denied&state={state}")
    assert reply is not None and "access_denied" in reply
    with pytest.raises(OAuthError, match="not waiting"):
        await rig.signin.complete(state, "code")


async def test_a_state_is_used_once(rig: Rig) -> None:
    state = await rig.sign_in()
    with pytest.raises(OAuthError, match="not waiting"):
        await rig.signin.complete(state, "code-again")


async def test_a_wrong_code_fails_the_sign_in_cleanly(rig: Rig) -> None:
    link = await rig.signin.begin(NAME)
    with pytest.raises(OAuthError, match="refused the token request"):
        await rig.signin.complete(_query(link)["state"], "forged")
    assert await rig.signin.tokens(NAME) is None


async def test_a_sign_in_expires_after_the_wait(rig: Rig) -> None:
    link = await rig.signin.begin(NAME)
    rig.clock.now += rig.signin.config.signin_wait_s + 1
    with pytest.raises(OAuthError, match="not waiting"):
        await rig.signin.complete(_query(link)["state"], "x")
    await rig.signin.begin(NAME)
    assert len(rig.posted) == 2


async def test_tokens_near_expiry_are_refreshed(rig: Rig) -> None:
    await rig.sign_in()
    rig.clock.now += 3600 - 60
    tokens = await rig.signin.tokens(NAME)
    assert tokens is not None and tokens.access_token == "access-2"
    assert rig.server.issued == 2


async def test_concurrent_refreshes_make_one_request(rig: Rig) -> None:
    await rig.sign_in()
    rig.clock.now += 3600
    results = await asyncio.gather(*(rig.signin.tokens(NAME) for _ in range(5)))
    assert {t.access_token for t in results if t} == {"access-2"}
    assert len(rig.server.seen("/token")) == 2


async def test_a_refused_refresh_of_an_expired_token_signs_out(rig: Rig) -> None:
    await rig.sign_in()
    rig.server.refused_refresh = True
    rig.clock.now += 3600
    assert await rig.signin.tokens(NAME) is None
    assert rig.signin.status()[0].state == "signed out"


async def test_a_refused_refresh_keeps_a_token_that_still_works(rig: Rig) -> None:
    await rig.sign_in()
    rig.server.refused_refresh = True
    rig.clock.now += 3600 - 60
    tokens = await rig.signin.tokens(NAME)
    assert tokens is not None and tokens.access_token == "access-1"


async def test_ensure_returns_none_and_asks_when_signed_out(rig: Rig) -> None:
    assert await rig.signin.ensure(NAME, 0) is None
    assert len(rig.posted) == 1


async def test_ensure_waits_for_a_sign_in_that_arrives(rig: Rig) -> None:
    waiting = asyncio.create_task(rig.signin.ensure(NAME, 5))
    for _ in range(50):
        await asyncio.sleep(0)
        if rig.posted:
            break
    link = _query(await rig.signin.begin(NAME))
    code = rig.server.grant_code(link["code_challenge"])
    await rig.signin.complete(link["state"], code)
    tokens = await waiting
    assert tokens is not None and tokens.access_token == "access-1"


async def test_sign_out_forgets_tokens_but_keeps_the_client(rig: Rig) -> None:
    await rig.sign_in()
    rig.signin.sign_out(NAME)
    creds = rig.store.load(NAME)
    assert creds.tokens is None and creds.client is not None
    assert [(s.name, s.upstream, s.state) for s in rig.signin.status()] == [(NAME, UPSTREAM, "signed out")]


async def test_status_tells_an_expired_access_token_from_a_signed_out_server(rig: Rig) -> None:
    await rig.sign_in()
    assert rig.signin.status()[0].state == "signed in"
    rig.clock.now += 4000
    assert "refreshes on use" in rig.signin.status()[0].state


def test_only_loopback_addresses_count_as_callbacks() -> None:
    assert callback_params("http://127.0.0.1:9/cb?code=a&state=b") == ("b", "a", None)
    assert callback_params("<http://localhost/cb?state=b&error=denied>.") == ("b", None, "denied")
    assert callback_params("https://evil.example/cb?code=a&state=b") is None
    assert callback_params("http://127.0.0.1/cb?code=a") is None
