"""`relay setup bot <name> --stdin`: store a Webex bot's token in the Keychain. The token is never shown."""

from __future__ import annotations

import sys
from typing import Annotated

import keyring.errors
import typer

from agent_relay.cli.output import fail, note, out
from agent_relay.models.config import BOT_NAMES, BotName
from agent_relay.settings import (
    KEYCHAIN_SERVICE,
    KeychainLockedError,
    bot_item,
    load_config,
    set_bot_token,
)

app = typer.Typer(help="One-time setup steps.", no_args_is_help=True)

Stdin = Annotated[bool, typer.Option("--stdin", help="Read the token from standard input.")]


def _bot_name(raw: str) -> BotName:
    for name in BOT_NAMES:
        if name == raw:
            return name
    raise fail(f"unknown bot {raw!r}; the bots are {', '.join(BOT_NAMES)}")


def _token_from_stdin(name: BotName) -> str:
    if sys.stdin.isatty():
        raise fail(f"stdin is a terminal; pipe the token in: pbpaste | relay setup bot {name} --stdin")
    token = sys.stdin.read().strip()
    if not token:
        raise fail("nothing came in on stdin; pipe the token in")
    if len(token.split()) != 1:
        raise fail("stdin held more than one word; pipe in just the token")
    return token


@app.command("bot")
def bot(
    name: Annotated[str, typer.Argument(help="control, vscode, cursor or opencode")],
    stdin: Stdin = False,
) -> None:
    """Store the bot's token in the Keychain. Pipe it in; there is no option that takes it as an argument."""
    which = _bot_name(name)
    if not stdin:
        raise fail(
            f"pipe the token in so it stays out of shell history: pbpaste | relay setup bot {which} --stdin"
        )
    token = _token_from_stdin(which)
    config = load_config()
    item = bot_item(config.webex, which)
    try:
        set_bot_token(config.webex, which, token)
    except keyring.errors.KeyringError as exc:
        locked = KeychainLockedError(item)
        raise fail(str(locked) if isinstance(exc, keyring.errors.KeyringLocked) else _refused(exc)) from None
    out(
        f"Stored the {which} bot's token in the Keychain (item '{item.value}', service '{KEYCHAIN_SERVICE}')."
    )
    if which != "control" and which not in config.webex.bots:
        note(f"Tell relayd about it: relay config set webex.bots.{which} {item.value}")
    note("restart relayd to apply: `relay start`")


def _refused(exc: Exception) -> str:
    return f"the Keychain refused the write ({type(exc).__name__}); unlock it and run the command again"
