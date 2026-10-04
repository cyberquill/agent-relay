# agent-relay: status and what's left

Last updated 2026-10-04, after commit `f8f5c4f` and the commit that adds the user guide.
The plan is [`docs/PLAN.md`](docs/PLAN.md). Departures are in [`docs/deviations.md`](docs/deviations.md)
(D1–D69), and each acceptance criterion maps to its tests in [`docs/acceptance.md`](docs/acceptance.md).
For users, start with [`docs/user-guide.md`](docs/user-guide.md).

## Where things stand

- **Code:** checkpoints C0–C14 are built and committed. C15 (live verification) is nearly done: every
  app the phone can drive has passed the live smoke test.
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
| relay-hook | Installed for Cursor, VS Code (Local and Copilot), Copilot CLI and OpenCode; fails open, and always answers Cursor with valid JSON (D61) |
| Adapters | VS Code Local (relay-bridge extension), VS Code Copilot (Agent Host Protocol), OpenCode server, Cursor CLI (ACP), Cursor IDE over CDP (gated off by default) |
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
| VS Code Local | ✅ register, stream, approve, park, reply, back |
| VS Code Copilot (Agent Host) | ✅ register, stream, approve, park, reply, back |
| OpenCode | ✅ register, stream, approve, idle, wake |
| Cursor CLI | ✅ register, stream, approve, idle, wake |
| Cursor IDE (hooks and parking) | not run yet; needs a person at the desk |

## What's left

### 1. Needs a person at the Mac

- [ ] **Cursor IDE smoke** (`--only cursor`). Start the script, then type its prompt in a Cursor chat
      opened on `.scratch`; the phone steps are driven from Webex as before.
- [ ] **G2, ask first:** the screen-locked test (S7). Get approval before locking the screen.
- [ ] **AC-24 / D35:** `uv sync --frozen --extra cdp`, `relay config set cdp.enabled true`,
      `relay start`, quit and relaunch Cursor with the debug port, then `relay selftest cursor-cdp`
      with the chat pane open. Relaunching Cursor ends the Cursor chat that is driving the build.
- [ ] **D61:** with away mode off, check that a command outside Cursor's allowlist still prompts at
      the desk.
- [ ] **D25:** check that Cursor hooks carry the ACP `sessionId` for a phone-started Cursor CLI session
      (relay.db shows no second `cursor` session for the same chat).
- [ ] Tick the matching boxes in `docs/acceptance.md` and update D25, D35 and D61.

How to drive a smoke run (see "The live smoke test" in `docs/operations.md`). Reset the answers file
in its own command first; don't run it alongside the smoke run.

```bash
cd ~/Desktop/Repository/Personal/agent-relay
: > /tmp/smoke_answers
(export RELAY_LIVE=1; tail -f /tmp/smoke_answers | uv run --frozen python -u scripts/smoke_live.py --only cursor --timeout 240 --record)
# answer each y/n with: echo y >> /tmp/smoke_answers
```

- Send messages with the Webex CLI:
  `webex_cli.py send --room <bot room> [--parent <thread root>] --markdown '…'`.
  Don't use the Webex MCP for this.
- **Send `/away` as a reply inside the session's thread, never at the top level.** A top-level
  `/away` turns away mode on for every workspace and gates the Cursor chat driving the test.
- Away mode is currently **on for `.scratch`** from the smoke runs. Turn it off when done:
  `uv run relay away off --workspace "$PWD/.scratch"`.

### 2. Definition of Done (PLAN §9)

- [ ] Every checkpoint C0–C15 committed with `uv run tox` green, including `ts`.
- [ ] AC-1 to AC-40 all ticked in `docs/acceptance.md` (AC-24 is the one open live criterion).
- [ ] `docs/spikes.md` complete; `docs/deviations.md` lists every departure.
- [x] No stubs or TODOs (`test_no_stubs` passes).
- [x] `relay doctor` all green after G1.
- [ ] Live smoke works for every app: four of five pass; the Cursor IDE run is left.
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
