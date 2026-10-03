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
- [ ] **AC-16** Live: list sessions on the real editor host — `tests/live/test_ahp_live.py` (pending C15: the live run is recorded with the smoke test)
- [x] **AC-17** relay-bridge protocol unit tests with a mocked `vscode` — `extensions/relay-bridge/test/bridge.test.ts`
- [x] **AC-18** The vscode_local adapter against a fake bridge — `tests/adapters/test_vscode_local.py`
- [x] **AC-19** OpenCode contract test against a fake server with a recorded `/doc` and SSE — `tests/adapters/test_opencode.py`
- [x] **AC-20** Live: an OpenCode session in `.scratch` streams `OK` and aborts — `tests/live/test_opencode_live.py` (run at C6, docs/spikes.md S9)
- [x] **AC-21** Cursor CLI contract test against a fake ACP agent — `tests/adapters/test_cursor_cli.py`
- [ ] **AC-22** Live: the Cursor CLI returns `OK` — `tests/live/test_cursor_cli_live.py` (pending C15: needs `cursor-agent login`)
- [x] **AC-23** Cursor CDP unit tests against a saved DOM fixture — `tests/adapters/test_cursor_cdp.py`
- [ ] **AC-24** Live: the Cursor CDP self-test passes — `tests/live/test_cursor_cdp_live.py` (pending C15: `relay selftest cursor-cdp` with Cursor open)

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
