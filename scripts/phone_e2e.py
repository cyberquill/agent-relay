"""Drive the relay end to end as the owner (plan P8). Needs RELAY_LIVE=1 and a running relayd.

    (export RELAY_LIVE=1; .venv/bin/python scripts/phone_e2e.py --apps copilot,cursor [--only 1,2])

It sends messages to the 1:1 room with the relay bot through the Webex person token that `webex-cli`
already uses, and asserts on what the bot posts back. The token is never printed. The pieces live in
`scripts/e2e/`; `tests/test_e2e_driver.py` runs every scenario against a fake relay.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.e2e.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
