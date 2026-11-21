# agent-relay: status and what's left

Last updated 2026-11-21, after the code of the November round (branch `feat/november-round`, P0–P8).
The plan is [`docs/PLAN.md`](docs/PLAN.md); the November round's plan is
[`sandbox/08-usability-round.md`](sandbox/08-usability-round.md) (decisions U1–U14, phases P0–P8).
Departures are in [`docs/deviations.md`](docs/deviations.md) (D1–D127), and each acceptance criterion maps
to its tests in [`docs/acceptance.md`](docs/acceptance.md) (AC-1–AC-40, OR-1–OR-20 for the October round,
UR-1–UR-8 for the November round). For users, start with [`docs/user-guide.md`](docs/user-guide.md).

## November round (2026-11-21)

Code is written and tested offline; **nothing has been run live yet except P0**. The merged tree has not
had a full `uv run tox`: each agent ran only targeted tests on its own files. Commits are made by the
orchestrator.

| Phase | State |
|---|---|
| P0 unblock (drop `webex.bots`, truncate the error log, restart relayd) | ✅ done live 2026-11-21 16:07: relayd up, `/help` answered, VS Code self-test passed on 1.147.0 |
| P1 one space (D116) and daemon robustness (D121) | code done and tested; live: a deliberately bad token on a running relayd not tried |
| P2 no approvals (D117, D124, D125) | code done and tested; the app auto-approve flags are unverified live |
| P3 final result only (D118, D127) | code done and tested; live: E2E scenarios 2 and 3 |
| P3b files in, HTML out, `send_phone_file` (D122) | code done and tested; live: image and PDF per app, `answer.html` on Android |
| P4 cards (D119) | code done and tested (Playwright PWA card tests not run); live: `scripts/card_check.py`, E2E scenarios 2 and 6, the Android look |
| P5 away, manual and global (D120) | code done and tested; live: E2E scenario 8 per desk app |
| P6 every app working | **open** (live): `relay install --apply`, `relay start`, per-app checks below |
| P7 docs and bookkeeping | ✅ docs updated with the code (this file, README, user guide, operations, security, deviations D116–D127, acceptance UR-1–UR-8) |
| P8 E2E driver (D123) | code done and tested against a fake relay; not yet run against Webex |

### Open live checks (plan §6 and the phase checks)

- [ ] Does Cursor desktop skip its own prompt when the hook answers `allow`? (Only matters while away.)
- [ ] Does VS Code honour `permissionDecision: "allow"` from `PreToolUse`, given
      `chat.tools.global.autoApprove: true` in your settings?
- [ ] Does `cursor-agent --force acp` stop sending `session/request_permission`? (D125)
- [ ] AHP: is `autoApprove` a valid `createSession` config value on VS Code 1.147? (D125)
- [ ] Is Cursor's last `afterAgentResponse` always the final answer, or can it be pre-tool narration?
      (Cleared when a tool hook follows it, so a turn ending after a tool yields no text.)
- [ ] Does a Webex Android card delete remove the card cleanly, with no "Unable to send your request"
      on a stale tap? (Now relevant to every one-shot card, D119.)
- [ ] Does Webex Android open `answer.html` in the app or hand it to the browser, and does the CSP survive
      either path? (D122)
- [ ] With away on, does a Cursor CDP chat also register as a second desk session through Cursor's hooks?
      `core/hooks.py` links `cursor` and `cursor_cli` but not `cursor_cdp`; if it does, link it.
- [ ] Do Copilot CLI hook payloads always carry `transcript_path`? It is the only source of the final text.
- [ ] Does the OAuth integration behind `webex-cli` have the scope for `POST /attachment/actions`? If not,
      the driver falls back to typed commands and card taps stay a phone check. (D123)
- [ ] Does a Webex client honour `label`, `isRequired` and `errorMessage` on the `/new` card's inputs?
      (`scripts/card_check.py` only proves Webex accepts them.)
- [ ] `RELAY_LIVE=1 .venv/bin/python scripts/card_check.py`: every card accepted (HTTP 200) and deleted.
- [ ] `RELAY_LIVE=1 .venv/bin/python scripts/phone_e2e.py --apps copilot,cursor,opencode,vscode`: scenarios
      1 to 9, results into [`docs/operations.md`](docs/operations.md#live-results-november-round).
- [ ] P6 per app: VS Code (`vscode`), VS Code Copilot (`copilot`, AHP: clear `adapter.degraded.vscode_copilot`
      after `relay selftest`), Cursor CLI, OpenCode (`relay config set adapters.opencode true`, password in
      the Keychain), Cursor desktop hooks (restart Cursor), Cursor CDP (self-test on Cursor 3.30.8; selectors
      v4 if it fails), Copilot CLI hooks.
- [ ] The owner opens the `/new` and `/model` cards on Android and checks the layout.
- [ ] The owner deletes the three retired app bots at developer.webex.com.

### Open items from the November round

- [ ] **Full `uv run tox`** on the merged tree (ruff, `mypy --strict`, pytest, `tox -e ts`); the OpenCode
      plugin's TypeScript test and `tests/frontends/test_pwa_cards.py` (Playwright) were not run. Re-run
      `tests/test_e2e_driver.py` and `tests/live/test_phone_e2e.py` once the tree is quiet.
- [ ] `uv.lock` was edited by hand for `markdown-it-py` (D126); run `uv lock` once online.
- [ ] `scrub.scrub_text` is quadratic on one very long unbroken word (5,000 word characters take 0.5 s;
      60,000 take over a minute). `render_answer` scrubs three times and `validate_file` scrubs text, so a
      base64 or minified blob could stall the caller. Fix with possessive quantifiers or a length guard in
      `scrub.py`, and render answers through `asyncio.to_thread`.
- [ ] Dead approval and wizard code left behind for a cleanup pass: `PendingKind` still has `"approval"` (an
      old row is just closed), `HookService` still takes an unused `broker`, `ParkManager` takes an unused
      `notify`, and `handlers.DRAFT_PREFIX` and the draft sweep in housekeeping outlive the wizard (a test pins
      the two constants equal).
- [ ] `plugins/opencode/package.json` still says the plugin "relays tool approvals"; the installer's hook
      timeout for approval-kind hooks is `question_hook_timeout_s()` although they no longer block.
- [ ] `ws_client.DeviceError` still says "Webex rejected the bot token (401). Check the Keychain entry."; the
      host and doctor give the better message (`pbpaste | relay setup token --stdin`).
- [ ] `card.offer:<nonce>` keys of `/new` and `/model` cards that are never submitted stay in the registry
      (about 4 KB each); sweep the ones whose `card.nonce:` key is gone in housekeeping.
- [ ] The CDP self-test does not save the model list; only an explicit `/models cursor-ide` does
      (`core.service.discovery.CDP_MODELS_KEY`). The Cursor CLI's own 30 s `models` call keeps running in the
      background when the card's 3 s timeout gives up (it fills the cache).
- [ ] `webex_token_check` runs in both `relay doctor` and `/doctor` (one `GET /people/me` each).
- [ ] An installed copy of the old `stepping-away` patch now reads as drifted: restore the base skill and run
      `relay install --apply` (see [operations.md](docs/operations.md#the-stepping-away-skill-patch)).
- [ ] [`docs/PLAN.md`](docs/PLAN.md) §5 still describes approvals, per-workspace away, streaming and `answer.md`,
      and lists `scripts/smoke_live.py`; deviations D116–D127 and D57 record what replaced them.
- [ ] Squash-merge `feat/november-round` to `main` at the end; never push (U14).

## October round (2026-10-13)

Superseded in places by the November round (D116–D127). Kept for history.

| Phase | State | Commits |
|---|---|---|
| P0 groundwork (lifecycle, registry migrations, cancel-aware IPC, split service, cards and commands) | ✅ | `dc14985` |
| P1 safety fixes (L1 install, L2 core, L3a AHP/OpenCode, L3b Cursor/VS Code, L4 Webex) | ✅ | `94ffb44`, `90cf39b` |
| P2 phone UX (L5–L11: cards, queue modes, screen-lock away, attachments, `/ws`, recall skill) | ✅ | `aa28713`, `ce6712b`, `1b3f2ce`, `8936007`, `ae4de94` |
| P3 L12 one bot per app (removed in the November round, D116) | ✅ | `a0bde79` |
| P3 L13 create the bots (live) (retired, D116) | ✅ | none (live setup, D111) |
| P4 docs and final tox | ✅ | pending the orchestrator's commit |

October's human live checks that still stand, each now covered by a November check above where it applies:

- [ ] Notifications show the bot name clearly; space sections sync to the phone (now one space).
- [ ] A photo reaches each app natively; an HTTPS clone works under launchd.
- [ ] Window reload vs VS Code follow-ups (D85).
- Dropped by the November round: the show-card submit check and `webex.new_card_layout = "two"` (the
  show-card is gone, D119); "a card can be edited after a submit" (cards are deleted, D119); "screen lock
  turns away mode on and off" (there is no trigger, D120); regenerating the first app bot's token (the app
  bots are retired, D116).

## Where things stood after C15 (2026-10-04)

History from the first build. Its approval, park-with-lock and smoke-script parts are superseded by D116–D123.

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
| relayd | Router with a desk-session gate, SQLite registry, question deadlines, parking, one global away flag, IPC socket, launchd agent, rotating logs; survives a Webex failure (D120, D121) |
| relay-hook | Installed for Cursor, VS Code (its own agent and Copilot), Copilot CLI and OpenCode; fails open, and always answers Cursor with valid JSON (D61, D124) |
| Adapters | VS Code (its own agent, through the relay-bridge extension), VS Code Copilot (Agent Host Protocol), OpenCode server, Cursor CLI (ACP), Cursor IDE over CDP (gated off by default) |
| Front ends | Webex bot (one bot, one space; websocket, flat Adaptive Cards deleted after use, one message per turn, `answer.html`), optional PWA on loopback |
| relay-mcp | `notify_phone`, `ask_phone`, `send_phone_file` |
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

October's results, recorded in [`docs/operations.md`](docs/operations.md) (the "Live smoke 2026-10-04" tables),
driven from Webex on 2026-10-04 with a script that is gone (D57, D123). The November results go in
[*Live results (November round)*](docs/operations.md#live-results-november-round):

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

- [ ] **November round, open after the live run** (details in *Live results (November round)*): VS Code's
      next prompt after `/model` never arrives; re-run Copilot (AHP) scenarios 3 to 7 with the D129 fix (the
      three allowed sessions are used); scenario 8 (needs a desk turn); Cursor CDP self-test; the Android look
      of the `/new` and `/model` cards.

Live runs now use the end-to-end driver (see "The live end-to-end test" in `docs/operations.md`):
`RELAY_LIVE=1 .venv/bin/python scripts/phone_e2e.py`. Away mode is one global flag, so a scenario that turns
it on (scenario 8) affects every desk chat until its closing `/back`.

### 2. Definition of Done (PLAN §9)

- [ ] Every checkpoint C0–C15 committed with `uv run tox` green, including `ts`.
- [x] AC-1 to AC-40 all ticked in `docs/acceptance.md`.
- [ ] `docs/spikes.md` complete; `docs/deviations.md` lists every departure.
- [x] No stubs or TODOs (`test_no_stubs` passes).
- [x] `relay doctor` all green after G1.
- [x] Live smoke works for VS Code, VS Code Copilot, OpenCode, the Cursor IDE and the Cursor CLI.
- [ ] The README quick start works from a clean `RELAY_HOME`.

### 3. Smaller follow-ups noticed during testing

- [x] The VS Code model card listed internal utility models (`copilot-utility`, `copilot-utility-small`,
      `copilot-dictation-cleanup-luna`); the `/new` and `/model` cards, `/models` and a typed `/model` now
      hide them (D119).
- [x] Leftover sessions from earlier smoke runs: migration 5 ended every old session (D116), and
      housekeeping ends idle sessions after 7 days (D121).
- [ ] Upstream: `cursor-agent` sometimes appends "Error: RetriableError: WritableIterable is closed"
      to the reply of a woken turn. The reply itself is complete. Report it to Cursor if it persists.
- [ ] The OpenCode provider config can print provider tokens in a terminal. The
      scrubber now catches them (D69).
