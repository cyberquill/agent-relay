"""The gateway's HTTP surface: key check, token swap, retry on 401, streaming, callback page."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx

from agent_relay.gateway.store import KEY_HEADER
from tests.gateway.rig import KEY, NAME, Rig, keyed

MCP = f"/mcp/{NAME}"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}


async def test_a_request_without_the_key_is_refused(client: httpx.AsyncClient, rig: Rig) -> None:
    await rig.sign_in()
    assert (await client.post(MCP, json=INIT)).status_code == 403
    assert (await client.post(MCP, json=INIT, headers={KEY_HEADER: "wrong"})).status_code == 403
    assert rig.server.mcp_bodies == []


async def test_an_unknown_server_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/mcp/nope", json=INIT, headers=keyed())).status_code == 404


async def test_the_key_is_swapped_for_the_servers_token(client: httpx.AsyncClient, rig: Rig) -> None:
    await rig.sign_in()
    response = await client.post(MCP, json=INIT, headers=keyed(**{"Mcp-Session-Id": "abc", "Cookie": "x=1"}))
    assert response.status_code == 200
    assert response.headers["mcp-session-id"] == "sess-1"
    sent = rig.server.seen("/v1/mcp")[-1]
    assert sent.headers["authorization"] == "Bearer access-1"
    assert sent.headers["mcp-session-id"] == "abc"
    assert KEY_HEADER.lower() not in sent.headers
    assert "cookie" not in sent.headers
    assert KEY not in repr(sent.headers)
    assert b"initialize" in rig.server.mcp_bodies[-1]


async def test_a_sub_path_and_query_are_forwarded(client: httpx.AsyncClient, rig: Rig) -> None:
    await rig.sign_in()
    await client.get(f"{MCP}/stream?x=1", headers=keyed())
    assert rig.server.requests[-1].url.path == "/v1/mcp/stream"
    assert rig.server.requests[-1].url.query == b"x=1"


async def test_an_event_stream_is_relayed(client: httpx.AsyncClient, rig: Rig) -> None:
    await rig.sign_in()
    response = await client.get(MCP, headers=keyed(Accept="text/event-stream"))
    assert response.headers["content-type"] == "text/event-stream"
    assert b'data: {"ok":true}' in response.content


async def test_signed_out_answers_401_without_a_challenge_and_asks_the_phone(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    response = await client.post(MCP, json=INIT, headers=keyed())
    assert response.status_code == 401
    assert response.json()["error"] == "signin_required"
    assert "www-authenticate" not in response.headers
    assert len(rig.posted) == 1
    await client.post(MCP, json=INIT, headers=keyed())
    assert len(rig.posted) == 1


async def test_a_token_the_server_turned_down_is_refreshed_and_retried(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    await rig.sign_in()
    rig.server.revoke()
    response = await client.post(MCP, json=INIT, headers=keyed())
    assert response.status_code == 200
    assert rig.server.issued == 2


async def test_a_server_that_keeps_refusing_signs_out_and_asks_again(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    await rig.sign_in()
    rig.server.revoke()
    rig.server.refused_refresh = True
    response = await client.post(MCP, json=INIT, headers=keyed())
    assert response.status_code == 401
    assert rig.signin.status()[0].state == "waiting for you to sign in"
    assert len(rig.posted) == 2


async def test_a_server_that_cannot_be_reached_is_a_502(rig: Rig) -> None:
    from agent_relay.gateway.app import build_app

    await rig.sign_in()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    app = build_app(rig.signin, http, lambda: KEY, 0)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8791") as c:
        response = await c.post(MCP, json=INIT, headers=keyed())
    assert response.status_code == 502


async def test_a_server_that_does_not_publish_its_sign_in_is_a_502(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    rig.servers["other"] = "https://nothing.example.test/mcp"
    response = await client.post("/mcp/other", json=INIT, headers=keyed())
    assert response.status_code == 502
    assert response.json()["error"] == "signin_failed"


async def test_the_callback_in_the_mac_browser_finishes_the_sign_in(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    link = await rig.signin.begin(NAME)
    query = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    code = rig.server.grant_code(query["code_challenge"])
    response = await client.get("/oauth/callback", params={"code": code, "state": query["state"]})
    assert response.status_code == 200
    assert "atlassian is signed in" in response.text
    assert rig.signin.status()[0].state == "signed in"


async def test_the_callback_rejects_bad_input_with_a_page(client: httpx.AsyncClient, rig: Rig) -> None:
    assert (await client.get("/oauth/callback")).status_code == 400
    assert (await client.get("/oauth/callback", params={"state": "x", "code": "y"})).status_code == 400
    denied = await client.get("/oauth/callback", params={"state": "x", "error": "access_denied"})
    assert denied.status_code == 400 and "cancelled" in denied.text
