"""Logging with secret scrubbing on every record."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from agent_relay import paths
from agent_relay.scrub import scrub_text

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub_text(record.getMessage())
        record.args = None
        return True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not any(isinstance(f, ScrubFilter) for f in logger.filters):
        logger.addFilter(ScrubFilter())
    return logger


def configure(level: int = logging.INFO, log_file: Path | None = None, console: bool = True) -> None:
    """Configure the root logger once per process. Handlers scrub too, for third-party loggers."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter(_FORMAT)
    handlers: list[logging.Handler] = []
    if console:
        handlers.append(logging.StreamHandler())
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(ScrubFilter())
        root.addHandler(handler)


BODY_LIMIT = 2_000
_bodies = {"enabled": False}


def set_debug_bodies(enabled: bool) -> None:
    """Message bodies reach the logs only with `debug_bodies = true`, only at DEBUG, always scrubbed."""
    _bodies["enabled"] = enabled


def log_body(logger: logging.Logger, label: str, text: str) -> None:
    if _bodies["enabled"] and logger.isEnabledFor(logging.DEBUG):
        clipped = text if len(text) <= BODY_LIMIT else f"{text[:BODY_LIMIT]}… ({len(text)} chars)"
        logger.debug(f"{label}: {clipped}")


def default_log_file(name: str) -> Path:
    return paths.logs_dir() / f"{name}.log"
