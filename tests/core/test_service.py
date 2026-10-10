"""App names the phone may type for each harness."""

from __future__ import annotations

import pytest

from agent_relay.core.service import RelayError, resolve_app
from agent_relay.models.events import Harness


@pytest.mark.parametrize(
    ("name", "harness"),
    [
        ("vscode", Harness.VSCODE),
        ("VSCode", Harness.VSCODE),
        ("copilot", Harness.VSCODE_COPILOT),
        ("vscode-copilot", Harness.VSCODE_COPILOT),
        ("ahp", Harness.VSCODE_COPILOT),
        ("cursor", Harness.CURSOR_CLI),
        ("cursor-ide", Harness.CURSOR_CDP),
        ("oc", Harness.OPENCODE),
        ("vscode_copilot", Harness.VSCODE_COPILOT),
    ],
)
def test_aliases_resolve(name: str, harness: Harness) -> None:
    assert resolve_app(name) is harness


@pytest.mark.parametrize("name", ["local", "vscode-local", "vscode_local"])
def test_the_old_local_names_are_gone(name: str) -> None:
    with pytest.raises(RelayError, match="Unknown app"):
        resolve_app(name)
