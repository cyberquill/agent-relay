"""Process launchers used by the launchd agents. They read Keychain so plists never hold a secret.

`python -m agent_relay.ops.launchers opencode` execs `opencode serve` on loopback with
`OPENCODE_SERVER_PASSWORD` set from Keychain. The password only ever lives in that process's
environment; it is never printed or written.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from agent_relay import paths
from agent_relay.models.config import OpenCodeConfig
from agent_relay.settings import MissingSecretError, Secret, get_secret, load_config

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
EXIT_CONFIG = 2

type Exec = Callable[[str, list[str], Mapping[str, str]], object]


class LaunchError(RuntimeError):
    pass


def opencode_argv(config: OpenCodeConfig, binary: Path) -> list[str]:
    parts = urlsplit(config.url)
    host = parts.hostname or ""
    if host not in LOOPBACK:
        raise LaunchError(f"opencode.url must be on loopback, not {host or config.url!r}")
    port = parts.port or 4096
    return [str(binary), "serve", "--hostname", host, "--port", str(port)]


def opencode_env(base: Mapping[str, str], password: str) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k != "OPENCODE_SERVER_PASSWORD"}
    env["OPENCODE_SERVER_PASSWORD"] = password
    return env


def opencode_main(exec_: Exec = os.execve) -> int:
    config = load_config()
    password = get_secret(Secret.OPENCODE_PASSWORD)
    if password is None:
        sys.stderr.write(f"relay opencode launcher: {MissingSecretError(Secret.OPENCODE_PASSWORD)}\n")
        return EXIT_CONFIG
    binary = paths.opencode_cli()
    if not binary.exists():
        sys.stderr.write(f"relay opencode launcher: {binary} not found; install OpenCode first\n")
        return EXIT_CONFIG
    try:
        argv = opencode_argv(config.opencode, binary)
    except LaunchError as exc:
        sys.stderr.write(f"relay opencode launcher: {exc}\n")
        return EXIT_CONFIG
    exec_(argv[0], argv, opencode_env(os.environ, password))
    return 0


LAUNCHERS: dict[str, Callable[[], int]] = {"opencode": opencode_main}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in LAUNCHERS:
        sys.stderr.write(f"usage: python -m agent_relay.ops.launchers {{{'|'.join(LAUNCHERS)}}}\n")
        return EXIT_CONFIG
    return LAUNCHERS[args[0]]()


if __name__ == "__main__":
    raise SystemExit(main())
