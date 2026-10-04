# agent-relay

Drive VS Code Copilot (Local and Copilot harnesses), Cursor and OpenCode on this Mac from an Android
phone over a Webex 1:1 space. Sessions run in the same harness the desktop uses.

New here? Start with the [user guide](docs/user-guide.md): what to prepare on the Mac, and how to use it
from the phone.

## Quick start

```bash
uv sync --frozen --all-extras
(cd extensions/relay-bridge && npm ci && npm run build)

# The Webex bot token goes in the Keychain; -w with no value prompts for it.
security add-generic-password -U -s agent-relay -a webex-bot-token -w

uv run relay config set webex.allowed_email you@example.com
uv run relay ws add scratch "$PWD/.scratch"

uv run relay install            # read what it will change
uv run relay install --apply    # hooks, MCP sync, skills, launch agents, relay-bridge
uv run relay start
uv run relay doctor
```

Then message the bot from the phone. The first message from `allowed_email` pins your Webex account;
nobody else is answered. `/help` lists the commands, `/away on` sends desk sessions to the phone and
`/panic` stops everything. `uv run relay uninstall --apply` restores every file it changed.

To try it without touching your home folder, set `RELAY_HOME` first: every path the relay reads or
writes is under it.

```bash
(export RELAY_HOME=$(mktemp -d); uv run relay install && uv run relay doctor)
```

## Development

```bash
uv run tox     # ruff format, ruff check, mypy --strict, pytest, and the extension's TypeScript tests
```

Live tests run only with `RELAY_LIVE=1`; see [`docs/operations.md`](docs/operations.md#the-live-smoke-test).

## Docs

- [`docs/user-guide.md`](docs/user-guide.md): Mac preparation and day-to-day use, at a high level
- [`docs/PLAN.md`](docs/PLAN.md): design and acceptance criteria
- [`docs/operations.md`](docs/operations.md): install, daily use, every command
- [`docs/security.md`](docs/security.md): threat model and controls
- [`docs/deviations.md`](docs/deviations.md): where the build departs from the plan, and why
- [`docs/acceptance.md`](docs/acceptance.md): each acceptance criterion and the tests that prove it
