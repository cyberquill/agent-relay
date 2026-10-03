from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import keyring
import pytest

from agent_relay import paths
from agent_relay.core.registry import Registry
from tests.fakes.keyring_backend import MemoryKeyring


@pytest.fixture(autouse=True)
def relay_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A short temporary home: Unix socket paths must stay under 104 bytes on macOS."""
    root = Path(tempfile.mkdtemp(prefix="rh", dir="/tmp"))
    monkeypatch.setenv("RELAY_HOME", str(root))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
async def registry(relay_home: Path) -> AsyncIterator[Registry]:
    reg = Registry(paths.db_path())
    await reg.open()
    yield reg
    await reg.close()


def live_enabled() -> bool:
    return os.environ.get("RELAY_LIVE") == "1"


live = pytest.mark.skipif(not live_enabled(), reason="live test: set RELAY_LIVE=1")
