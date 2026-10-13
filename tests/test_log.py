"""Logging output is scrubbed in full, tracebacks and stack info included."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_relay.log import ScrubFormatter, configure, get_logger
from agent_relay.scrub import REDACTED

SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture(autouse=True)
def restore_root() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_a_secret_in_an_exception_message_is_redacted_in_the_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "relayd.log"
    configure(logging.INFO, log_file, console=False)
    logger = get_logger("tests.log.exception")
    try:
        raise RuntimeError(f"upstream said token {SECRET}")
    except RuntimeError:
        logger.exception("call failed")
    _flush()
    text = log_file.read_text(encoding="utf-8")
    assert "Traceback" in text and "call failed" in text
    assert SECRET not in text
    assert REDACTED in text


def test_a_secret_in_stack_info_is_redacted() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ScrubFormatter("%(message)s"))
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "msg", None, None)
    record.stack_info = f"stack with {SECRET}"
    handler.handle(record)
    assert SECRET not in stream.getvalue()
    assert REDACTED in stream.getvalue()


def test_every_relay_handler_uses_the_scrubbing_formatter(tmp_path: Path) -> None:
    configure(logging.INFO, tmp_path / "x.log", console=True)
    handlers = logging.getLogger().handlers
    assert len(handlers) == 2
    assert all(isinstance(h.formatter, ScrubFormatter) for h in handlers)
