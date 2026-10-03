"""A headless Chromium with a debug port, showing the saved Cursor composer fixture."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cursor_cdp" / "composer.html"
WORKSPACE_NAME = "fixture-ws"
FLAGS = (
    "--headless=new",
    "--remote-debugging-port=0",
    "--remote-debugging-address=127.0.0.1",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-breakpad",
    "--disable-crash-reporter",
    "--disable-features=Translate",
)


def chromium_binary() -> Path:
    """`RELAY_TEST_CHROMIUM`, else the newest Playwright Chromium, else Google Chrome."""
    override = os.environ.get("RELAY_TEST_CHROMIUM")
    if override:
        return Path(override)
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    found = sorted(cache.glob("chromium-*/chrome-mac*/*.app/Contents/MacOS/*"), reverse=True)
    if found:
        return found[0]
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.exists():
        return chrome
    raise RuntimeError("No Chromium found: set RELAY_TEST_CHROMIUM to a Chromium or Chrome binary")


@dataclass
class Chromium:
    proc: subprocess.Popen[bytes]
    port: int

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def start_chromium(profile: Path, url: str = FIXTURE.as_uri()) -> Chromium:
    profile.mkdir(parents=True, exist_ok=True)
    argv = [str(chromium_binary()), *FLAGS, f"--user-data-dir={profile}", url]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    marker = profile / "DevToolsActivePort"
    for _ in range(300):
        if marker.exists():
            first = marker.read_text().splitlines()[:1]
            if first and first[0].isdigit():
                return Chromium(proc, int(first[0]))
        if proc.poll() is not None:
            raise RuntimeError(f"Chromium exited with {proc.returncode}")
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("Chromium did not open its debug port")


@dataclass
class FakeRunner:
    """Stands in for `open`/`pgrep`; records every command."""

    running: bool = False
    calls: list[list[str]] = field(default_factory=list)

    async def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        if argv[0] == "pgrep":
            return 0 if self.running else 1
        return 0
