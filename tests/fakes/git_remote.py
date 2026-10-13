"""A local bare git repository, so clone tests never touch the network."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git_env(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }


def make_remote(base: Path, name: str = "remote") -> str:
    """A `file://` URL of a bare repo holding one commit."""
    home = base / "githome"
    home.mkdir(exist_ok=True)
    work = base / f"{name}-work"
    work.mkdir()
    env = git_env(home)
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "init"]):
        if args[0] == "add":
            (work / "README.md").write_text("hello\n")
        subprocess.run(["git", *args], cwd=work, env=env, check=True, capture_output=True)
    bare = base / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], env=env, check=True)
    return bare.as_uri()
