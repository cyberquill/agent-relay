# agent-relay: status and what's left

Last updated 2026-10-04 02:45, at commit `f2454cb` plus the commit that adds this file.
The plan is [`docs/PLAN.md`](docs/PLAN.md). Departures are in [`docs/deviations.md`](docs/deviations.md)
(D1–D65), and each acceptance criterion maps to its tests in [`docs/acceptance.md`](docs/acceptance.md).

## Where things stand

- **Code:** checkpoints C0–C14 are built and committed. C15 (live verification) is in progress.
- **Checks:** `uv run tox` is green: format, lint, mypy `--strict`, pytest (624 passed, 6 skipped live
  tests) and `ts`.
- **Install:** applied to the real home directory after gate G1. relayd runs under launchd,
  `relay doctor` reports 0 failing, away mode is **off** everywhere, and the Webex bot is connected.
- **Live smoke test:** VS Code Local is the only app run so far, and 5 of its 6 steps pass (see below).

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
| Docs | README quick start, operations guide, security model, acceptance map, deviations, spikes |

## Bugs found by live testing (all fixed, tested and committed)

| Commit | Fix | Deviation |
|---|---|---|
| `0fdaa48` | Cursor's fail-closed hooks blocked every tool call when relay had no opinion | D61 |
| `3c20f46` | `/new` took the first prompt word as the model; duplicate thread roots; session stuck "running" after a Local error | D62 |
| `ea36882` | A one-line `/new` still asked for a model; a stale sticky model was offered; duplicate model choices | D62 |
| `9ba5871` | "Parked" was posted with away mode off; a bare `/away` only reported the state | D63 |
| `f5e4147` | Turning away mode off left desk approvals waiting for the phone until their 10-minute deadline | D64 |
| `f2454cb` | `/back` inside a thread was refused for every app ("does not support parking") | D65 |

## Live smoke results so far

VS Code Local, run 2026-10-04 02:33–02:42, driven from Webex:

| Step | Result | Evidence |
|---|---|---|
| register | ✅ | `vscode_local:phone-bd4de31822a74056ab31d8d9d7f216df` |
| stream | ✅ | the tool step streamed into the thread |
| approve | ✅ | the card arrived; Allow ran `echo relay-smoke`; "OK" came back |
| park | ✅ | `/away` in the thread, then a thread prompt; relayd showed `parked` |
| reply | ✅ | "📨 delivered to the parked session", then "PONG" |
| back | ❌ | "does not support parking", fixed in `f2454cb` after this run; needs a re-run |

## What's left

### 1. Finish the live smoke test (C15)

- [ ] Re-run VS Code Local in full on the fixed relayd (relayd was restarted on `f2454cb`).
- [ ] VS Code Copilot (`--only vscode_copilot`).
- [ ] OpenCode (`--only opencode`).
- [ ] Cursor CLI (`--only cursor_cli`).
- [ ] Cursor IDE, hooks and parking (`--only cursor`). The chat starts at the desk, so a person has
      to type the prompt in Cursor.
- [ ] Record every result in `docs/operations.md` (`--record`, or paste the table).

How to drive it (see "The live smoke test" in `docs/operations.md`):

```bash
cd ~/Desktop/Repository/Personal/agent-relay
: > /tmp/smoke_answers
(export RELAY_LIVE=1; tail -f /tmp/smoke_answers | uv run --frozen python -u scripts/smoke_live.py --only vscode_local --timeout 240 --record)
# answer each y/n with: echo y >> /tmp/smoke_answers
```

- Send messages with the Webex CLI:
  `webex_cli.py send --room <bot room> [--parent <thread root>] --markdown '…'`.
  Don't use the Webex MCP for this.
- **Send `/away` as a reply inside the session's thread, never at the top level.** A top-level
  `/away` turns away mode on for every workspace and gates the Cursor chat driving the test (this
  happened once; see D64).
- Card clicks: `/tmp/card_submit.py <message id> '<inputs json>'` is a throwaway helper outside the
  repo and may be gone after a reboot. It posts an attachment action as the user via `webex_cli`.
- Read an approval card's `request_id` from relay.db (read-only):
  `sqlite3 ~/Library/Application\ Support/agent-relay/relay.db "select request_id from pending where status='open'"`.

### 2. Other live checks still open

- [ ] **G2, ask first:** the screen-locked test (S7). Get approval before locking the screen.
- [ ] **AC-16:** list sessions on the real editor host (`tests/live/test_ahp_live.py`).
- [ ] **AC-22:** the Cursor CLI returns `OK` (`tests/live/test_cursor_cli_live.py`); `cursor-agent` is now logged in.
- [ ] **AC-24 / D35:** `relay config set cdp.enabled true`, then `relay selftest cursor-cdp` with Cursor open.
- [ ] **D18:** check that a Copilot CLI hook session is refined into a VS Code Copilot session.
- [ ] **D25:** check that Cursor hooks carry the ACP `sessionId` for a phone-started Cursor CLI session.
- [ ] **D61:** with away mode off, check that a command outside Cursor's allowlist still prompts at the desk.
- [ ] Tick the matching boxes in `docs/acceptance.md` and update D18, D25, D35 and D61.

### 3. Definition of Done (PLAN §9)

- [ ] Every checkpoint C0–C15 committed with `uv run tox` green, including `ts`.
- [ ] AC-1 to AC-40 all ticked in `docs/acceptance.md`.
- [ ] `docs/spikes.md` complete; `docs/deviations.md` lists every departure.
- [x] No stubs or TODOs (`test_no_stubs` passes).
- [x] `relay doctor` all green after G1.
- [ ] Live smoke works for VS Code Local, VS Code Copilot, OpenCode, the Cursor IDE and the Cursor CLI.
- [ ] The README quick start works from a clean `RELAY_HOME`.

### 4. Smaller follow-ups noticed during testing

- [ ] The VS Code model card lists internal utility models (`copilot-utility`, `copilot-utility-small`,
      `copilot-dictation-cleanup-luna`) under the same labels as real models. Consider hiding them.
- [ ] Leftover sessions from earlier runs (for example `vscode_local:phone-f5533e57…`) are still listed;
      end them with `/stop` or let them age out.
- [ ] Unexplained: tool calls failed for about 8 minutes around 01:55–02:03. relayd logged nothing
      then, so the cause looks like Cursor, not relay. Watch for a repeat.
