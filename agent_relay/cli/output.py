"""Shared output for `relay` commands: results on stdout, notes on stderr, secrets never shown."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

import typer

from agent_relay.scrub import scrub_text

DRY_RUN_HINT = "Dry run: nothing was written. Rerun with --apply to make these changes."


def out(text: str) -> None:
    typer.echo(scrub_text(text))


def note(text: str) -> None:
    typer.echo(scrub_text(text), err=True)


def notes(lines: Iterable[str]) -> None:
    for line in lines:
        note(f"• {line}")


def written(files: Iterable[Path], verb: str = "wrote") -> None:
    found = list(files)
    for file in found:
        out(f"{verb} {file}")
    if not found:
        out("nothing to change")


def python_option() -> str:
    return sys.executable


def fail(message: str, code: int = 1) -> typer.Exit:
    note(f"error: {message}")
    return typer.Exit(code)
