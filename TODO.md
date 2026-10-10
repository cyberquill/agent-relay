# agent-relay: status and what's left

Last updated 2026-10-04, after commit `f8f5c4f` and the commit that adds the user guide.
The plan is [`docs/PLAN.md`](docs/PLAN.md). Departures are in [`docs/deviations.md`](docs/deviations.md)
(D1–D69), and each acceptance criterion maps to its tests in [`docs/acceptance.md`](docs/acceptance.md).
For users, start with [`docs/user-guide.md`](docs/user-guide.md).

## Where things stand

- **Code:** checkpoints C0–C14 are built and committed. C15 (live verification) is nearly done: all
  five apps have passed the live smoke test.
- **Checks:** `uv run tox` is green: format, lint, mypy `--strict`, pytest (641 passed, 6 skipped live
  tests) and `ts`.
- **Install:** applied to the real home directory after gate G1 and re-applied after D69. relayd runs
  under launchd, `relay doctor` reports 0 failing, and the Webex bot is connected.
- **Live tests:** AC-16 (Agent Host sessions), AC-20 (OpenCode) and AC-22 (Cursor CLI) pass on the real
  apps. AC-24 (Cursor CDP self-test) is still open.

## Done

| Area | What exists |
|---|---|
| relayd | Router, SQLite registry, approvals with deadlines, parking, away-mode scopes, IPC socket, launchd agent, rotating logs |
| relay-hook | Installed for Cursor, VS Code (its own agent and Copilot), Copilot CLI and OpenCode; fails open, and always answers Cursor with valid JSON (D61) |
| Adapters | VS Code (its own agent, through the relay-bridge extension), VS Code Copilot (Agent Host Protocol), OpenCode server, Cursor CLI (ACP), Cursor IDE over CDP (gated off by default) |
| Front ends | Webex bot (websocket, Adaptive Cards, threads per session), optional PWA on loopback |
| relay-mcp | `notify_phone`, `ask_phone` |
| Skills | `agent-relay-away` (`/away`, `/back`), the `stepping-away` patch, plugin bundles |
| relay CLI | `install`/`uninstall`, `start`/`stop`, `status`, `doctor`, `ws`, `away`, `config`, `skills dedupe`, `selftest`, update handling |
| Docs | README quick start, user guide, operations guide, security model, acceptance map, deviations, spikes |

## Bugs found by live testing (all fixed, tested and committed)

| Commit | Fix | Deviation |
|---|---|---|
| `0fdaa48` | Cursor's fail-closed hooks blocked every tool call when relay had no opinion | D61 |
| `3c20f46` | `/new` took the first prompt word as the model; duplicate thread roots; session stuck "running" after a Local error | D62 |
| `ea36882` | A one-line `/new` still asked for a model; a stale sticky model was offered; duplicate model choices | D62 |
| `9ba5871` | "Parked" was posted with away mode off; a bare `/away` only reported the state | D63 |
| `f5e4147` | Turning away mode off left desk approvals waiting for the phone until their 10-minute deadline | D64 |
| `f2454cb` | `/back` inside a thread was refused for every app ("does not support parking") | D65 |
| `e452caa` | An HTTP failure in the Webex websocket client stopped relayd instead of retrying | D66 |
| `98ee590` | A phone-started Agent Host session got a duplicate Copilot CLI session and a split thread | D67 |
| `5c4c802` | The Copilot CLI process's `SessionEnd` posted "Released (session ended)" for a live Agent Host session | D67 |
| `7065437` | Cursor CLI tool lines showed raw JSON for a single argument | D68 |
| `99dd699` | With relayd unreachable, OpenCode called the plugin's helper exports as plugins and returned HTTP 500 | D69 |
| `024489b` | `relay start` right after `relay stop` failed with launchd error 37 | D55 |
| `f8f5c4f` | `relay install --apply` failed with launchd error 5 and left relayd and OpenCode unloaded | D55 |
| `86f0e53` | The smoke script expected OpenCode and the Cursor CLI to park; they go idle and wake (D11) | D57 |

## Live smoke results

All recorded in [`docs/operations.md`](docs/operations.md#the-live-smoke-test), driven from Webex on
2026-10-04:

| App | Result |
|---|---|
| VS Code (`vscode`) | ✅ register, stream, approve, park, reply, back |
| VS Code Copilot (Agent Host) | ✅ register, stream, approve, park, reply, back |
| OpenCode | ✅ register, stream, approve, idle, wake |
| Cursor CLI | ✅ register, stream, approve, idle, wake |
| Cursor IDE (hooks and parking) | ✅ register, stream, approve, park, reply, back |

## What's left

### 1. Needs a person at the Mac

- [x] **Cursor IDE smoke** (`--only cursor`): passed 2026-10-04 08:41 UTC.
- [x] **G2 / S7:** the screen-locked test passed for all four phone-driven apps (see `docs/spikes.md`).
- [x] **AC-24 / D35:** the self-test passes on Cursor 3.23.12 with selectors v2 (the first run found
      three stale selectors, now fixed). `cdp.enabled` is **true** in `~/.config/agent-relay/relay.toml`
      and Cursor runs with the debug port.
- [x] **CDP live (D35):** a real phone prompt and a follow-up both returned correct replies, and the
      `model_option` selector is fixed (selectors v3, nine models found).
- [ ] **CDP, still unverified live:** `/models cursor-ide` after the v3 fix, choosing a model with
      `/new cursor-ide scratch "…" <model>`, and `/stop` on a running turn (one live session
      creation is left of the three allowed).
- [ ] **D61, partly done:** the A/B run showed no difference with and without the relay's hooks for
      commands Cursor auto-runs (see D61). Still to find: a command Cursor prompts for on its own (for
      example with sandboxed auto-run turned off), then confirm it still prompts with the hooks in place.
- [x] **D25:** the Cursor CLI smoke produced no duplicate `cursor` hook session and one approval card
      (recorded in D25).
- [x] AC-24 is ticked in `docs/acceptance.md` and D35 is updated; D61 stays partly open (above).

How to drive a smoke run (see "The live smoke test" in `docs/operations.md`). Reset the answers file
in its own command first; don't run it alongside the smoke run.

```bash
cd ~/Desktop/Repository/Personal/agent-relay
: > /tmp/smoke_answers
(export RELAY_LIVE=1; tail -f /tmp/smoke_answers | uv run --frozen python -u scripts/smoke_live.py --only vscode --timeout 240 --record)
# answer each y/n with: echo y >> /tmp/smoke_answers
```

- Send messages with the Webex CLI:
  `webex_cli.py send --room <bot room> [--parent <thread root>] --markdown '…'`.
  Don't use the Webex MCP for this.
- **Send `/away` as a reply inside the session's thread, never at the top level.** A top-level
  `/away` turns away mode on for every workspace and gates the Cursor chat driving the test.
- A smoke run that parks needs away mode on for `.scratch` first
  (`uv run relay away on --workspace "$PWD/.scratch"`); turn it off afterwards with `away off`.

### 2. Definition of Done (PLAN §9)

- [ ] Every checkpoint C0–C15 committed with `uv run tox` green, including `ts`.
- [x] AC-1 to AC-40 all ticked in `docs/acceptance.md`.
- [ ] `docs/spikes.md` complete; `docs/deviations.md` lists every departure.
- [x] No stubs or TODOs (`test_no_stubs` passes).
- [x] `relay doctor` all green after G1.
- [x] Live smoke works for VS Code, VS Code Copilot, OpenCode, the Cursor IDE and the Cursor CLI.
- [ ] The README quick start works from a clean `RELAY_HOME`.

### 3. Smaller follow-ups noticed during testing

- [ ] The VS Code model card lists internal utility models (`copilot-utility`, `copilot-utility-small`,
      `copilot-dictation-cleanup-luna`) under the same labels as real models. Consider hiding them.
- [ ] Leftover sessions from earlier smoke runs are still listed; end them with `/stop` or let them
      age out.
- [ ] Upstream: `cursor-agent` sometimes appends "Error: RetriableError: WritableIterable is closed"
      to the reply of a woken turn. The reply itself is complete. Report it to Cursor if it persists.
- [ ] The OpenCode provider config can print provider tokens in a terminal. The
      scrubber now catches them (D69).
