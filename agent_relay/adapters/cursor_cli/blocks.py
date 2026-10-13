"""ACP prompt content blocks for files sent from the phone."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Sequence
from pathlib import Path

from pydantic import JsonValue

from agent_relay.log import get_logger
from agent_relay.models.attachments import Attachment

log = get_logger(__name__)


def _link(item: Attachment) -> dict[str, JsonValue]:
    return {
        "type": "resource_link",
        "uri": Path(item.local_path).resolve().as_uri(),
        "name": item.name,
        "mimeType": item.mime,
        "size": item.size,
    }


async def _image(item: Attachment) -> dict[str, JsonValue]:
    try:
        data = await asyncio.to_thread(Path(item.local_path).read_bytes)
    except OSError:
        log.warning(f"cannot read {item.name}; sending it as a file reference")
        return _link(item)
    return {
        "type": "image",
        "data": base64.b64encode(data).decode(),
        "mimeType": item.mime,
        "uri": Path(item.local_path).resolve().as_uri(),
    }


async def prompt_blocks(text: str, attachments: Sequence[Attachment], images: bool) -> list[JsonValue]:
    """The text, then one block per file: an image block where the agent takes images, else a link."""
    blocks: list[JsonValue] = [{"type": "text", "text": text}]
    for item in attachments:
        blocks.append(await _image(item) if images and item.mime.startswith("image/") else _link(item))
    return blocks
