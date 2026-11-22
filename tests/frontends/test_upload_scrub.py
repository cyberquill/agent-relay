"""A file posted to the phone: text is scrubbed, images and other binary go through byte for byte."""

from __future__ import annotations

import pytest

from agent_relay.frontends.webex.api import scrub_upload
from agent_relay.scrub import REDACTED
from tests.frontends.conftest import Relay

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x00binary"
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def test_binary_types_go_through_unchanged() -> None:
    assert scrub_upload(PNG, "image/png") == PNG
    assert scrub_upload(b"%PDF-1.3 \xff\xfe", "application/pdf") == b"%PDF-1.3 \xff\xfe"
    assert scrub_upload(b"PK\x03\x04\x00", "application/zip") == b"PK\x03\x04\x00"
    assert scrub_upload(b"\x00\x01", "") == b"\x00\x01"


def test_text_types_are_scrubbed() -> None:
    cleaned = scrub_upload(f"GITHUB_TOKEN={TOKEN}\nok\n".encode(), "text/html").decode()
    assert TOKEN not in cleaned and REDACTED in cleaned and "ok" in cleaned
    assert TOKEN not in scrub_upload(f'{{"t": "{TOKEN}"}}'.encode(), "application/json").decode()


def test_text_type_that_is_not_utf8_is_not_refused() -> None:
    assert scrub_upload(b"\xff\xfe\x00 latin", "text/plain") == b"\xff\xfe\x00 latin"


@pytest.mark.parametrize("mime", ["image/png", "application/pdf"])
async def test_the_api_uploads_a_binary_file_as_is(relay: Relay, mime: str) -> None:
    room = relay.webex.direct_room
    await relay.frontend.api.create_message(room_id=room, markdown="pic", file=("pic.bin", PNG, mime))
    sent = relay.webex.bot_messages()[-1]
    assert sent.file_bytes == PNG
