from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from agent_relay.gateway.app import build_app
from tests.gateway.rig import KEY, Clock, Rig, make_rig


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def rig(clock: Clock) -> Rig:
    return make_rig(clock)


@pytest.fixture
def app(rig: Rig) -> FastAPI:
    return build_app(rig.signin, rig.server.client(), lambda: KEY, 0)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8791") as c:
        yield c
