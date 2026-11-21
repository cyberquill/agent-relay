"""`relay setup token --stdin`: store the Webex bot's token in the Keychain. The token is never shown."""

from __future__ import annotations

import sys
from typing import Annotated

import keyring.errors
import typer

from agent_relay.cli.output import fail, note, out
from agent_relay.settings import KEYCHAIN_SERVICE, KeychainLockedError, Secret, set_secret

app = typer.Typer(help="One-time setup steps.", no_args_is_help=True)

Stdin = Annotated[bool, typer.Option("--stdin", help="Read the token from standard input.")]
PIPE_HINT = "pbpaste | relay setup token --stdin"


def _token_from_stdin() -> str:
    if sys.stdin.isatty():
        raise fail(f"stdin is a terminal; pipe the token in: {PIPE_HINT}")
    token = sys.stdin.read().strip()
    if not token:
        raise fail("nothing came in on stdin; pipe the token in")
    if len(token.split()) != 1:
        raise fail("stdin held more than one word; pipe in just the token")
    return token


@app.command("token")
def token(stdin: Stdin = False) -> None:
    """Store the Webex bot's token in the Keychain. Pipe it in; no option takes it as an argument."""
    if not stdin:
        raise fail(f"pipe the token in so it stays out of shell history: {PIPE_HINT}")
    value = _token_from_stdin()
    try:
        set_secret(Secret.WEBEX_BOT_TOKEN, value)
    except keyring.errors.KeyringError as exc:
        locked = KeychainLockedError(Secret.WEBEX_BOT_TOKEN)
        raise fail(str(locked) if isinstance(exc, keyring.errors.KeyringLocked) else _refused(exc)) from None
    out(
        f"Stored the Webex bot's token in the Keychain "
        f"(item '{Secret.WEBEX_BOT_TOKEN.value}', service '{KEYCHAIN_SERVICE}')."
    )
    note("restart relayd to apply: `relay start`")


def _refused(exc: Exception) -> str:
    return f"the Keychain refused the write ({type(exc).__name__}); unlock it and run the command again"
