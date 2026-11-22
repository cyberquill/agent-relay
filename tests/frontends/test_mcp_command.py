"""/mcp on the phone: sign-in state, starting a sign-in, and finishing one by pasting the callback address."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from agent_relay.gateway.signin import SignIn
from tests.frontends.conftest import Relay
from tests.gateway.rig import NAME, Clock, Rig, make_rig


@pytest.fixture
def rig() -> Rig:
    return make_rig(Clock())


def attach(relay: Relay, rig: Rig) -> SignIn:
    relay.frontend.handlers.mcp = rig.signin
    return rig.signin


async def test_mcp_is_off_until_the_gateway_is_on(relay: Relay) -> None:
    await relay.say("/mcp")
    assert "gateway is off" in relay.bot_texts()[-1]


async def test_mcp_lists_each_server_and_its_state(relay: Relay, rig: Rig) -> None:
    attach(relay, rig)
    await relay.say("/mcp")
    assert relay.bot_texts()[-1] == f"- **{NAME}**: signed out"


async def test_mcp_signin_posts_the_link_and_a_pasted_address_finishes_it(relay: Relay, rig: Rig) -> None:
    attach(relay, rig)
    await relay.say(f"/mcp signin {NAME}")
    assert len(rig.posted) == 1
    link = rig.posted[0].split("](")[1].split(")")[0]
    query = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    code = rig.server.grant_code(query["code_challenge"])
    await relay.say(f"http://127.0.0.1:33418/oauth/callback?code={code}&state={query['state']}")
    assert relay.bot_texts()[-1] == f"✅ {NAME} is signed in."
    await relay.say("/mcp")
    assert relay.bot_texts()[-1] == f"- **{NAME}**: signed in"


async def test_mcp_signout_and_an_unknown_name(relay: Relay, rig: Rig) -> None:
    attach(relay, rig)
    await rig.sign_in()
    await relay.say(f"/mcp signout {NAME}")
    assert relay.bot_texts()[-1] == f"🔓 {NAME} is signed out."
    await relay.say("/mcp signin nope")
    assert "Known: atlassian" in relay.bot_texts()[-1]
