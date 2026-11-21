"""Post every card builder's maximal output to the owner's 1:1 space, expect HTTP 200, delete it again.

    (export RELAY_LIVE=1; .venv/bin/python scripts/card_check.py [--only new_session,model_switch])

Webex answers HTTP 400 for card JSON it does not support, so a live post is the real validator. The
control bot's token comes from the Keychain and is never printed. No session is created. The offline
twin is `tests/frontends/test_card_spec.py`, which runs the same samples through `card_spec.check_card`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import JsonValue

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_relay import settings  # noqa: E402
from agent_relay.frontends.webex.api import WebexApi, WebexError  # noqa: E402
from agent_relay.frontends.webex.cards.common import fit_card  # noqa: E402
from agent_relay.scrub import scrub_text  # noqa: E402
from tests.frontends.card_samples import maximal_cards  # noqa: E402
from tests.frontends.card_spec import check_card  # noqa: E402

PAUSE_S = 0.6  # Webex rate-limits message creation; stay well below it


@dataclass(frozen=True)
class Result:
    name: str
    ok: bool
    detail: str


async def check_one(api: WebexApi, email: str, name: str, card: dict[str, JsonValue]) -> Result:
    problems = check_card(card)
    if problems:
        return Result(name, False, f"offline check failed: {problems[0]}")
    fallback = f"card_check: {name}"
    if fit_card(fallback, card) is not card:
        return Result(name, False, "too large for one Webex message (the relay would drop the card)")
    try:
        posted = await api.create_message(fallback, to_person_email=email, card=card)
    except WebexError as exc:
        return Result(name, False, scrub_text(str(exc))[:200])
    except httpx.HTTPError as exc:
        return Result(name, False, f"network error: {type(exc).__name__}")
    try:
        await api.delete_message(posted.id)
    except (WebexError, httpx.HTTPError) as exc:
        return Result(name, True, f"posted (200), but the delete failed: {type(exc).__name__}")
    return Result(name, True, "posted (200) and deleted")


async def run(only: set[str]) -> int:
    config = settings.load_config()
    token = settings.require_secret(settings.Secret.WEBEX_BOT_TOKEN)
    samples = {k: v for k, v in maximal_cards().items() if not only or k in only}
    unknown = only - set(maximal_cards())
    if unknown:
        print(f"unknown card(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    results: list[Result] = []
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as client:
        api = WebexApi(client, base_url=config.webex.api_base)
        for name, card in samples.items():
            results.append(await check_one(api, config.webex.allowed_email, name, dict(card)))
            await asyncio.sleep(PAUSE_S)
    width = max(len(r.name) for r in results)
    print(f"{'card'.ljust(width)}  result  detail")
    for r in results:
        print(f"{r.name.ljust(width)}  {'PASS' if r.ok else 'FAIL'}    {r.detail}")
    failed = [r for r in results if not r.ok]
    print(f"{len(results) - len(failed)} of {len(results)} cards accepted")
    return 1 if failed else 0


def main() -> int:
    if os.environ.get("RELAY_LIVE") != "1":
        print("card_check posts to the real Webex space: set RELAY_LIVE=1 to run it.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--only", default="", help="comma-separated card names (default: all)")
    args = parser.parse_args()
    only = {n.strip() for n in args.only.split(",") if n.strip()}
    return asyncio.run(run(only))


if __name__ == "__main__":
    raise SystemExit(main())
