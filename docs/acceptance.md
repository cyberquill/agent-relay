# Acceptance

Every **[AC-x]** in [PLAN.md](PLAN.md), with the tests that prove it. `tests/test_acceptance.py` checks
this file: each AC appears once, each listed file exists and names its AC, and an open box says what
it waits for. Live tests run only with `RELAY_LIVE=1`.

## Foundations (C2)

- [x] **AC-1** No module outside `paths.py` builds a home-relative path — `tests/test_paths.py`
- [x] **AC-2** A missing token exits non-zero with the Keychain command — `tests/test_daemon.py`
- [x] **AC-3** State survives a daemon restart — `tests/core/test_registry.py`
- [x] **AC-4** Concurrent writes from hooks and adapters are safe — `tests/core/test_registry.py`
- [x] **AC-5** A raising adapter does not stop other sessions' events — `tests/core/test_router.py`
- [x] **AC-6** Capabilities are declared and the UI hides what is unsupported — `tests/core/test_router.py`, `tests/frontends/test_flows.py`
- [x] **AC-7** The daemon refuses connections from other uids — `tests/test_ipc.py`
- [x] **AC-8** A corpus of secret shapes is fully redacted — `tests/test_scrub.py`

## Webex and the PWA (C4, C13)

- [x] **AC-9** After a reconnect, missed messages are fetched from the last seen id — `tests/frontends/test_ws_client.py`
- [x] **AC-10** Other people, group rooms and their card submissions are ignored — `tests/frontends/test_access.py`
- [x] **AC-11** The command parser has full table tests — `tests/frontends/test_commands.py`
- [x] **AC-12** Adaptive Cards v1.3, one per message, edited once with the outcome — `tests/frontends/test_cards.py`
- [x] **AC-13** No message exceeds a Webex limit or the edit budget — `tests/frontends/test_renderer.py`
- [x] **AC-14** PWA: nothing binds when off, loopback only, 401 without the token, ordered SSE — `tests/frontends/test_pwa.py`

## Adapters (C6–C10)

- [x] **AC-15** The AHP adapter turns replayed traces into the expected events — `tests/adapters/test_ahp.py`
- [x] **AC-16** Live: list sessions on the real editor host — `tests/live/test_ahp_live.py` (passed at C15, 2026-10-04; the C15 smoke run of the Copilot harness is in docs/operations.md)
- [x] **AC-17** relay-bridge protocol unit tests with a mocked `vscode` — `extensions/relay-bridge/test/bridge.test.ts`
- [x] **AC-18** The vscode adapter against a fake bridge — `tests/adapters/test_vscode.py`
- [x] **AC-19** OpenCode contract test against a fake server with a recorded `/doc` and SSE — `tests/adapters/test_opencode.py`
- [x] **AC-20** Live: an OpenCode session in `.scratch` streams `OK` and aborts — `tests/live/test_opencode_live.py` (run at C6, docs/spikes.md S9; re-run at C15 after D69)
- [x] **AC-21** Cursor CLI contract test against a fake ACP agent — `tests/adapters/test_cursor_cli.py`
- [x] **AC-22** Live: the Cursor CLI returns `OK` — `tests/live/test_cursor_cli_live.py` (passed at C15, 2026-10-04)
- [x] **AC-23** Cursor CDP unit tests against a saved DOM fixture — `tests/adapters/test_cursor_cdp.py`
- [x] **AC-24** Live: the Cursor CDP self-test passes — `tests/live/test_cursor_cdp_live.py` (passed at C15, 2026-10-05, Cursor 3.23.12, selectors v2, after the first run found three stale selectors; see D35)

## Hooks, approvals and parking (C3, C5)

- [x] **AC-25** `relay-hook` p95 under 80 ms when away is off or relayd is down — `tests/hook/test_relay_hook.py`
- [x] **AC-26** Installing hooks twice produces the same output — `tests/hook/test_installers.py`
- [x] **AC-27** Uninstall restores hook backups byte for byte — `tests/hook/test_installers.py`
- [x] **AC-28** Existing `git-ai` entries are kept — `tests/hook/test_installers.py`
- [x] **AC-29** Every hook payload fixture parses — `tests/hook/test_shims.py`
- [x] **AC-30** Approvals fail closed on every harness; the relay's deadline denies first — `tests/core/test_hooks.py`
- [x] **AC-31** Parking releases on `/back`, `/panic`, away off and the deadline — `tests/core/test_hooks.py`
- [x] **AC-32** Loop guards: `stop_hook_active` and Copilot's 8-block cap — `tests/core/test_hooks.py`

## Skills, MCP, sync and doctor (C11, C12)

- [x] **AC-33** Each plugin bundle validates by schema and, where one exists, the vendor loader — `tests/ops/test_bundles.py`, `tests/live/test_bundle_loaders_live.py`
- [x] **AC-34** relay-mcp contract tests with an MCP client — `tests/mcp/test_relay_mcp.py`
- [x] **AC-35** Config sync round-trips today's files, renders valid JSON/JSONC, never prints secrets — `tests/sync/test_config_sync.py`
- [x] **AC-36** Skills dedupe moves only byte-identical folders — `tests/sync/test_skills_dedupe.py`
- [x] **AC-37** `relay doctor` covers every area, as text or `--json` — `tests/doctor/test_local.py`, `tests/doctor/test_daemon.py`
- [x] **AC-38** An app update re-runs the self-test and degrades on failure — `tests/core/test_updates.py`

## Operations and security (C14)

- [x] **AC-39** Install, uninstall, install inside `RELAY_HOME` leaves no stray files — `tests/ops/test_install.py`
- [x] **AC-40** Security unit tests cover each control (map in [security.md](security.md)) — `tests/test_security.py`

## October round (beyond the plan)

Features and fixes added after the plan (deviations D72–D111). These are not plan ACs; `tests/test_acceptance.py`
checks that the ids run from OR-1 without gaps, every box is ticked and every cited file exists.

- [x] **OR-1** Uninstall restores only files unchanged since the relay's last write, else unmerges or keeps the backup; backups are private — `tests/test_fileops.py`, `tests/ops/test_install.py`
- [x] **OR-2** An answer that cannot reach the app reopens its request — `tests/core/test_approvals_delivery.py`
- [x] **OR-3** Approvals are scoped: recent decisions per session for 60 s, workspace grants per tool and workspace — `tests/core/test_hooks.py`, `tests/core/test_approvals_grants.py`, `tests/core/test_housekeeping.py`
- [x] **OR-4** Parking uses the full block limit before releasing — `tests/core/test_hooks.py`
- [x] **OR-5** The approval card offers allow once, for the session, for the workspace, deny and deny with a reason — `tests/frontends/test_approval_card.py`
- [x] **OR-6** Question cards answer, skip or take custom text, and their notification line carries the question — `tests/frontends/test_cards.py`
- [x] **OR-7** `/new` is one card, or two with `webex.new_card_layout = "two"` — `tests/frontends/test_new_card.py`
- [x] **OR-8** `/sessions` is a dashboard card with a reusable nonce — `tests/frontends/test_dashboard.py`
- [x] **OR-9** A bare `/model` in a thread sends a model picker — `tests/frontends/test_model_switch.py`
- [x] **OR-10** A bare top-level `/away` sends a scope card — `tests/frontends/test_away_scope.py`
- [x] **OR-11** `/panic` asks first; `/panic now` does not — `tests/frontends/test_panic_confirm.py`
- [x] **OR-12** A turn ends with a completion card — `tests/frontends/test_completion_card.py`, `tests/frontends/test_renderer_status.py`
- [x] **OR-13** `/queue` shows a card with the mode and removable waiting messages — `tests/frontends/test_queue_card.py`
- [x] **OR-14** Queue modes (`queue`, `steer`, `interrupt`) and `/steer` — `tests/core/test_queue_modes.py`
- [x] **OR-15** Away mode follows the screen lock and undoes only what it did — `tests/core/test_presence.py`, `tests/doctor/test_presence.py`
- [x] **OR-16** Photos and files reach a thread or `/new`, natively where the app takes them — `tests/core/test_inbox.py`, `tests/frontends/test_attachments_intake.py`, `tests/adapters/test_attachments.py`
- [x] **OR-17** `/ws` adds a folder, clones a repo on an allowed host, and adds a repo to a workspace — `tests/core/test_workspaces_phone.py`, `tests/frontends/test_ws_card.py`
- [x] **OR-18** The `agent-relay-recall` skill ships in every bundle and install — `tests/ops/test_bundles.py`
- [x] **OR-19** A reply in an ended session's thread revives it — `tests/core/test_lifecycle.py`
- [x] **OR-20** One bot per app: routing by harness, per-space commands, `relay setup bot --stdin`, one websocket per bot — `tests/frontends/test_bots.py`, `tests/test_setup_bot.py`, `tests/test_daemon.py`, `tests/doctor/test_local.py`, `tests/doctor/test_daemon.py`
