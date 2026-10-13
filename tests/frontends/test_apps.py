from __future__ import annotations

from agent_relay.frontends.apps import APP_EMOJI
from agent_relay.models.events import Harness


def test_every_app_has_an_emoji() -> None:
    assert set(APP_EMOJI) == set(Harness)
