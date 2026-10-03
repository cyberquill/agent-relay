"""Fake macOS app bundles: just enough Info.plist for version checks."""

from __future__ import annotations

import plistlib
from pathlib import Path


def write_plist(target: Path, version: str | None) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    info: dict[str, str] = {"CFBundleName": "App"}
    if version is not None:
        info["CFBundleShortVersionString"] = version
    with target.open("wb") as fh:
        plistlib.dump(info, fh)
    return target
