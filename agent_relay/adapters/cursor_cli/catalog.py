"""`cursor-agent status` and `cursor-agent models`, parsed and cached (10 minutes by default)."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from agent_relay.adapters.base import AdapterUnavailableError, ModelInfo
from agent_relay.scrub import scrub_text

type Runner = Callable[[Sequence[str]], Awaitable[tuple[int, str]]]

CACHE_TTL_S = 600.0
RUN_TIMEOUT_S = 30.0
LOGIN_HINT = "Cursor CLI needs a login: run `cursor-agent login` on the Mac."
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MODEL_LINE = re.compile(
    r"^(?:[-*•]\s+)?(?P<id>[A-Za-z0-9][\w.\-/\[\]=,]*)(?:(?:\s+[-–—]|\s*:)\s+(?P<label>.+?))?\s*$"
)
_MARKER = re.compile(r"\s*\((?P<mark>current|default)\)\s*$", re.IGNORECASE)
_NOT_LOGGED_IN = ("not logged in", "authentication required", "please log in", "agent login")


async def run_cli(argv: Sequence[str]) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        raise AdapterUnavailableError(f"could not run {Path(argv[0]).name}: {exc.strerror or exc}") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), RUN_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise AdapterUnavailableError(f"`{Path(argv[0]).name} {' '.join(argv[1:])}` did not finish") from None
    return proc.returncode or 0, scrub_text(_ANSI.sub("", out.decode(errors="replace")))


def needs_login(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in _NOT_LOGGED_IN)


def parse_models(text: str) -> list[ModelInfo]:
    """Tolerant parser: `id - Label`, `id: Label` or a bare id per line, `(current)`/`(default)` marks."""
    models: list[ModelInfo] = []
    seen: set[str] = set()
    for raw in _ANSI.sub("", text).splitlines():
        line = raw.strip()
        if (
            not line
            or line.endswith(":")
            or line.lower().startswith(("available", "tip", "usage", "loading"))
        ):
            continue
        default = False
        marked = _MARKER.search(line)
        if marked:
            default = True
            line = line[: marked.start()].rstrip()
        match = _MODEL_LINE.match(line)
        if match is None:
            continue
        model_id = match.group("id")
        if model_id in seen:
            continue
        seen.add(model_id)
        label = (match.group("label") or model_id).strip()
        models.append(ModelInfo(id=model_id, label=label, default=default))
    return models


class CursorCli:
    def __init__(
        self,
        cli: Path,
        runner: Runner = run_cli,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = CACHE_TTL_S,
    ) -> None:
        self.cli = cli
        self._run = runner
        self._clock = clock
        self._ttl = ttl
        self._models: tuple[float, list[ModelInfo]] | None = None
        self._logged_in_at: float | None = None

    def _fresh(self, stamp: float | None) -> bool:
        return stamp is not None and self._clock() - stamp < self._ttl

    async def status(self) -> tuple[bool, str]:
        if not self.cli.exists():
            return False, f"Cursor CLI is not installed at {self.cli}."
        if self._fresh(self._logged_in_at):
            return True, "Cursor CLI is logged in"
        code, output = await self._run([str(self.cli), "status"])
        if code != 0 or needs_login(output):
            self._logged_in_at = None
            return False, LOGIN_HINT
        self._logged_in_at = self._clock()
        first = next((line.strip() for line in output.splitlines() if line.strip()), "logged in")
        return True, f"Cursor CLI: {first}"

    async def require_login(self) -> None:
        ok, detail = await self.status()
        if not ok:
            raise AdapterUnavailableError(detail)

    async def models(self) -> list[ModelInfo]:
        if self._models is not None and self._fresh(self._models[0]):
            return list(self._models[1])
        await self.require_login()
        code, output = await self._run([str(self.cli), "models"])
        if code != 0:
            if needs_login(output):
                self._logged_in_at = None
                raise AdapterUnavailableError(LOGIN_HINT)
            first = next((line.strip() for line in output.splitlines() if line.strip()), f"exit {code}")
            raise AdapterUnavailableError(f"`cursor-agent models` failed: {first}")
        models = parse_models(output)
        self._models = (self._clock(), models)
        return list(models)

    def forget_login(self) -> None:
        self._logged_in_at = None
