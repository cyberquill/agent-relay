"""Away mode that follows the screen lock: lock turns it on, unlock turns it off, only if the relay did it.

The lock state comes from CoreGraphics' session dictionary, read in-process (no extra permission).
When this session is not on the console, or CoreGraphics gives nothing, `ioreg` is asked instead (no
shell). A probe that fails or shows nothing is "unknown" and never changes anything.
"""

from __future__ import annotations

import asyncio
import ctypes
import functools
import plistlib
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from agent_relay import paths
from agent_relay.core.registry import GLOBAL_SCOPE, Registry
from agent_relay.log import get_logger
from agent_relay.models.config import PresenceConfig
from agent_relay.models.sessions import SessionStatus

log = get_logger(__name__)

type LockState = Literal["locked", "unlocked", "unknown"]
type Probe = Callable[[], Awaitable[LockState]]
type Post = Callable[[str], Awaitable[None]]

IOREG = ("/usr/sbin/ioreg", "-n", "Root", "-d1", "-a")
PROBE_TIMEOUT_S = 5.0
CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
CG_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
CF_UTF8 = 0x08000100
ON_CONSOLE = "kCGSSessionOnConsoleKey"
SCREEN_LOCKED = "CGSSessionScreenIsLocked"
# kv marker: present only while away is on because the screen locked, not because the user asked.
AUTO_KEY = "away.auto"
AUTO_LOCK = "lock"
LOCKED_NOTICE = "🔒 Mac locked: away on"
UNLOCKED_NOTICE = "🔓 Mac unlocked: away off"


class AwayToggle(Protocol):
    async def set_away_for(self, enabled: bool, workspace: str | None) -> str: ...


def parse_lock_state(raw: bytes) -> LockState:
    """The console user's `CGSSessionScreenIsLocked` (absent while unlocked), else `IOConsoleLocked`."""
    try:
        document = plistlib.loads(raw)
    except Exception:  # plistlib raises ValueError and expat errors alike
        return "unknown"
    if isinstance(document, list) and document:
        document = document[0]
    if not isinstance(document, dict):
        return "unknown"
    users = [u for u in document.get("IOConsoleUsers") or [] if isinstance(u, dict)]
    active = [u for u in users if u.get("kCGSSessionOnConsoleKey") is True]
    if active:
        return "locked" if active[0].get("CGSSessionScreenIsLocked") is True else "unlocked"
    if document.get("IOConsoleLocked") is True:
        return "locked"
    return "unlocked" if users else "unknown"


@functools.cache
def _frameworks() -> tuple[ctypes.CDLL, ctypes.CDLL] | None:
    try:
        cf, cg = ctypes.CDLL(CF_PATH), ctypes.CDLL(CG_PATH)
    except OSError:
        return None
    ref = ctypes.c_void_p
    cg.CGSessionCopyCurrentDictionary.argtypes, cg.CGSessionCopyCurrentDictionary.restype = [], ref
    cf.CFStringCreateWithCString.argtypes = [ref, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = ref
    cf.CFDictionaryGetValue.argtypes, cf.CFDictionaryGetValue.restype = [ref, ref], ref
    cf.CFGetTypeID.argtypes, cf.CFGetTypeID.restype = [ref], ctypes.c_ulong
    cf.CFBooleanGetTypeID.argtypes, cf.CFBooleanGetTypeID.restype = [], ctypes.c_ulong
    cf.CFBooleanGetValue.argtypes, cf.CFBooleanGetValue.restype = [ref], ctypes.c_bool
    cf.CFRelease.argtypes, cf.CFRelease.restype = [ref], None
    return cf, cg


def read_session() -> dict[str, bool] | None:
    """The boolean flags of this process's login session, or None when there is no session to read."""
    frameworks = _frameworks()
    if frameworks is None:
        return None
    cf, cg = frameworks
    session = cg.CGSessionCopyCurrentDictionary()
    if not session:
        return None
    flags: dict[str, bool] = {}
    try:
        for name in (ON_CONSOLE, SCREEN_LOCKED):
            key = cf.CFStringCreateWithCString(None, name.encode(), CF_UTF8)
            if not key:
                continue
            try:
                value = cf.CFDictionaryGetValue(session, key)
                if value and cf.CFGetTypeID(value) == cf.CFBooleanGetTypeID():
                    flags[name] = bool(cf.CFBooleanGetValue(value))
            finally:
                cf.CFRelease(key)
    finally:
        cf.CFRelease(session)
    return flags


def session_lock_state(flags: dict[str, bool] | None) -> LockState:
    """Locked or unlocked only for a session on the console; anything else is for `ioreg` to judge."""
    if not flags or flags.get(ON_CONSOLE) is not True:
        return "unknown"
    return "locked" if flags.get(SCREEN_LOCKED) is True else "unlocked"


async def probe_lock_state() -> LockState:
    # About 30 µs in-process, against about 30 ms of CPU for an `ioreg` run and its 200 KB plist.
    state = session_lock_state(read_session())
    return state if state != "unknown" else await probe_ioreg()


async def probe_ioreg() -> LockState:
    try:
        proc = await asyncio.create_subprocess_exec(
            *IOREG, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return "unknown"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return "unknown"
    return parse_lock_state(out) if proc.returncode == 0 else "unknown"


class PresenceMonitor:
    def __init__(
        self,
        registry: Registry,
        service: AwayToggle,
        config: PresenceConfig,
        post: Post,
        probe: Probe = probe_lock_state,
    ) -> None:
        self._registry = registry
        self._service = service
        self._config = config
        self._post = post
        self._probe = probe
        self._last: LockState = "unknown"

    async def run(self) -> None:
        log.info(f"presence: following the screen lock every {self._config.poll_s:g}s")
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("presence: poll failed")
            await asyncio.sleep(self._config.poll_s)

    async def tick(self) -> None:
        state = await self._probe()
        if state == "unknown" or state == self._last:
            return
        self._last = state
        log.info(f"presence: screen {state}")
        if state == "locked":
            await self._on_lock()
        else:
            await self._on_unlock()

    async def _global_away(self) -> bool:
        return (await self._registry.away_scopes()).get(GLOBAL_SCOPE, False)

    async def _on_lock(self) -> None:
        if not self._config.auto_away_on_lock or paths.pause_flag().exists():
            return
        if await self._global_away():
            return
        await self._service.set_away_for(True, None)
        # Written after the call: any other way of changing global away clears it.
        await self._registry.kv_set(AUTO_KEY, AUTO_LOCK)
        log.info("presence: away on (screen locked)")
        await self._announce(LOCKED_NOTICE)

    async def _on_unlock(self) -> None:
        if await self._registry.kv_pop(AUTO_KEY) != AUTO_LOCK:
            return
        if not await self._global_away():
            return
        await self._service.set_away_for(False, None)
        log.info("presence: away off (screen unlocked)")
        await self._announce(UNLOCKED_NOTICE)

    async def _announce(self, text: str) -> None:
        sessions = await self._registry.list_sessions()
        if not any(s.status is not SessionStatus.ENDED for s in sessions):
            return
        try:
            await self._post(text)
        except Exception as exc:
            log.warning(f"presence: could not post the notice: {exc.__class__.__name__}")
