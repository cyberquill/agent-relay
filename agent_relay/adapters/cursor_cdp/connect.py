"""Reaching the running Cursor over CDP, and starting it with the debug port when it is closed."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from playwright.async_api import Browser, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from agent_relay import paths
from agent_relay.adapters.base import AdapterUnavailableError
from agent_relay.adapters.cursor_cdp.selectors import SelectorSet
from agent_relay.log import get_logger

log = get_logger(__name__)

type Runner = Callable[[Sequence[str]], Awaitable[int]]

CONNECT_TIMEOUT_MS = 10_000
LAUNCH_WAIT_S = 30.0
NOT_DEBUGGABLE = (
    "Cursor is running without the debug port. Quit Cursor once; the relay restarts it with "
    "`--remote-debugging-port` bound to 127.0.0.1."
)


def endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def cursor_version(info: dict[str, object]) -> str:
    """`Cursor/<version>` from the user agent, or the browser string when the agent does not say."""
    agent = str(info.get("User-Agent") or "")
    found = re.search(r"Cursor/([\w.\-]+)", agent)
    if found:
        return found.group(1)
    return str(info.get("Browser") or "unknown")


async def fetch_version(port: int, timeout: float = 2.0) -> str | None:
    """The running Cursor's version, or None when nothing listens on the debug port."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{endpoint(port)}/json/version")
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return cursor_version(body) if isinstance(body, dict) else None


async def run_quiet(argv: Sequence[str]) -> int:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    return await proc.wait()


@dataclass
class Launcher:
    """Starts Cursor with the debug port; refuses to restart a Cursor that is already open."""

    app: Path = paths.CURSOR_APP
    runner: Runner = run_quiet

    def argv(self, port: int) -> list[str]:
        return [
            "open",
            "-a",
            str(self.app),
            "--args",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
        ]

    async def running(self) -> bool:
        return await self.runner(["pgrep", "-f", f"{self.app}/Contents/MacOS/"]) == 0

    async def ensure(self, port: int, wait_s: float = LAUNCH_WAIT_S) -> str:
        version = await fetch_version(port)
        if version is not None:
            return version
        if await self.running():
            raise AdapterUnavailableError(NOT_DEBUGGABLE)
        if not self.app.exists():
            raise AdapterUnavailableError(f"Cursor is not installed at {self.app}")
        code = await self.runner(self.argv(port))
        if code != 0:
            raise AdapterUnavailableError(f"`open -a Cursor` exited with {code}")
        log.info(f"started Cursor with the debug port on 127.0.0.1:{port}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while loop.time() < deadline:
            version = await fetch_version(port)
            if version is not None:
                return version
            await asyncio.sleep(0.5)
        raise AdapterUnavailableError(f"Cursor did not open its debug port within {wait_s:.0f} s")

    async def open_folder(self, workspace: str) -> None:
        """Asks the running Cursor to open `workspace` in a window (focusing it when already open)."""
        code = await self.runner(["open", "-a", str(self.app), workspace])
        if code != 0:
            raise AdapterUnavailableError(f"`open -a Cursor {workspace}` exited with {code}")


@dataclass
class Connection:
    playwright: Playwright
    browser: Browser

    @property
    def alive(self) -> bool:
        return self.browser.is_connected()

    def pages(self) -> list[Page]:
        return [page for context in self.browser.contexts for page in context.pages if not page.is_closed()]

    async def workbench(self, selectors: SelectorSet, workspace: str | None) -> Page | None:
        """The window for `workspace` (matched on its title), else any window showing the composer."""
        with_composer: list[Page] = []
        for page in self.pages():
            try:
                if await page.locator(selectors.css("composer")).count():
                    with_composer.append(page)
            except PlaywrightError:
                continue
        if workspace is None:
            return with_composer[0] if with_composer else None
        name = Path(workspace).name
        for page in with_composer:
            try:
                title = await page.title()
            except PlaywrightError:
                continue
            if name and name in title:
                return page
        return None

    async def close(self) -> None:
        try:
            await self.browser.close()
        except PlaywrightError:
            log.debug("the CDP connection was already closed")
        await self.playwright.stop()


type Connector = Callable[[int], Awaitable[Connection]]


async def connect(port: int) -> Connection:
    driver = await async_playwright().start()
    try:
        browser = await driver.chromium.connect_over_cdp(endpoint(port), timeout=CONNECT_TIMEOUT_MS)
    except PlaywrightError as exc:
        await driver.stop()
        raise AdapterUnavailableError(
            f"could not attach to Cursor on 127.0.0.1:{port}: {exc.message}"
        ) from None
    return Connection(driver, browser)
