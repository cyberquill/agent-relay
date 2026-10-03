# agent-relay — rules for coding agents

The full spec is [`docs/PLAN.md`](docs/PLAN.md). This file is the binding summary of its §1.

## Scope

- Everything in PLAN §5 is in scope, including components that ship off by default (PWA, Cursor CDP).
- No stubs: no `pass`-only bodies, `NotImplementedError`, `TODO`/`FIXME`/`XXX`, or "placeholder" text in
  committed code. `tests/test_no_stubs.py` enforces this; only `tests/fakes/` is exempt.
- Never weaken an assertion or skip a test to get green. The only allowed skip is live tests gated by
  `RELAY_LIVE=1`.
- When reality differs from the plan, implement the documented fallback (PLAN §6) and record it in
  [`docs/deviations.md`](docs/deviations.md) with what, why and evidence.
- Every `[AC-x]` is mapped to its tests in [`docs/acceptance.md`](docs/acceptance.md).

## Safety

- Never write to the real home directory during development. Every home-relative path goes through
  `agent_relay/paths.py`, which honours `RELAY_HOME`. Tests use a temporary `RELAY_HOME`.
- Real writes happen only through `relay install --apply`, after gate G1 (human approval).
- Live session creation only in `.scratch/`, with the prompt `Reply with exactly: OK`, at most 3 times
  per adapter.
- Never print secrets. Logging goes through `agent_relay/log.py`, which scrubs.
- Never add a git remote or push.

## Gotchas on this Mac

- `code` is not on PATH: `/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code`.
- Cursor CLI is `~/.local/bin/cursor-agent`; OpenCode is `~/.opencode/bin/opencode`.
- zsh: no `!` in commands, never a variable called `path`, no `VAR=value cmd` prefixes (export instead).
- Send large output to `/tmp` and read it with `head`/`tail`.

## Standards

- Python 3.13, uv. `uv run tox` runs ruff format, ruff check, `mypy --strict`, pytest and the TypeScript
  builds (`tox -e ts`). It must be green before every commit.
- Pydantic models for every wire format; f-strings; no `print()` outside CLI rendering.
- The hook entry point `agent_relay/hook/relay_hook.py` is stdlib-only, for startup time.
- Docs move with the code: README, `docs/operations.md`, `docs/security.md`, `docs/deviations.md`,
  `docs/spikes.md`, `docs/acceptance.md`.
- Conventional Commits, one or more per checkpoint (PLAN §7).
