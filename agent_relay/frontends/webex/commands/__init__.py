"""Chat commands. Every module here registers its commands on import; add a command by adding a
`register(CommandSpec(...))` call in the right module (or a new module in this package)."""

from __future__ import annotations

import importlib
import pkgutil


def load_all() -> list[str]:
    """Import every command module in this package. Returns their names."""
    loaded: list[str] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        importlib.import_module(f"{__name__}.{info.name}")
        loaded.append(info.name)
    return loaded


load_all()
