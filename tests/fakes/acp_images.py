"""The fake `cursor-agent`, but its `initialize` says the agent takes images in a prompt."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.fakes import acp_agent


class ImageAgent(acp_agent.Agent):
    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = super().handle(method, params)
        if method == "initialize":
            result["agentCapabilities"]["promptCapabilities"] = {"image": True}
        return result


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "acp" in argv:
        ImageAgent().serve()
        raise SystemExit(0)
    raise SystemExit(acp_agent.main(argv))
