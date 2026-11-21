# agent-relay

Drive VS Code (its own agent and the Copilot harness), Cursor and OpenCode on this Mac from an Android
phone over one Webex 1:1 space. Sessions run in the same harness the desktop uses. The phone gets one
message per turn (the final answer, or an `answer.html` for a long or rich one), questions when an agent
needs you, and files in both directions. There are no approvals: the relay allows what it answers, and
`/panic` is the brake.

New here? Start with the [user guide](docs/user-guide.md): what to prepare on the Mac, and how to use it
from the phone.

## Quick start

```bash
uv sync --frozen --all-extras
(cd extensions/relay-bridge && npm ci && npm run build)

# One Webex bot, one token, into the Keychain (copy it from the bot's page first; it is never printed).
pbpaste | uv run relay setup token --stdin

uv run relay config set webex.allowed_email you@example.com
uv run relay ws add scratch "$PWD/.scratch"

uv run relay install            # read what it will change
uv run relay install --apply    # hooks, MCP sync, skills, launch agents, relay-bridge
uv run relay start
uv run relay doctor
```

Then message the bot from the phone. The first message from `allowed_email` pins your Webex account;
nobody else is answered. `/help` lists the commands, `/new` starts a session from one card, `/away` (by
hand: nothing triggers it) sends desk chats to the phone and `/back` returns them, and `/panic` stops
everything. A rejected Webex token never stops relayd: `relay doctor` says so, and the same
`pbpaste | uv run relay setup token --stdin` fixes it. `uv run relay uninstall --apply` restores every
file it changed.

Agents reach the phone through `relay-mcp`: `notify_phone`, `ask_phone` and `send_phone_file`.

To try it without touching your home folder, set `RELAY_HOME` first: every path the relay reads or
writes is under it.

```bash
(export RELAY_HOME=$(mktemp -d); uv run relay install && uv run relay doctor)
```

## Development

```bash
uv run tox     # ruff format, ruff check, mypy --strict, pytest, and the extension's TypeScript tests
```

Live tests run only with `RELAY_LIVE=1`. The end-to-end driver that acts as the owner is
`RELAY_LIVE=1 .venv/bin/python scripts/phone_e2e.py` (and `scripts/card_check.py` for the cards); see
[`docs/operations.md`](docs/operations.md#the-live-end-to-end-test).

## Docs

- [`docs/user-guide.md`](docs/user-guide.md): Mac preparation and day-to-day use, at a high level
- [`docs/PLAN.md`](docs/PLAN.md): design and acceptance criteria
- [`docs/operations.md`](docs/operations.md): install, daily use, every command
- [`docs/security.md`](docs/security.md): threat model and controls
- [`docs/deviations.md`](docs/deviations.md): where the build departs from the plan, and why
- [`docs/acceptance.md`](docs/acceptance.md): each acceptance criterion and the tests that prove it

## License

[MIT](LICENSE)
