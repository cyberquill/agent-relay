# Security

Agent Relay lets a phone drive coding agents that can run commands on this Mac. This page is the
threat model, the controls that answer it, and the tests that prove each control (AC-40).

## What is worth protecting

- **The Mac's shell and files.** Every agent the relay drives can edit files and run commands.
- **Secrets the agents see.** Tokens in repo files, environment variables, MCP configs, tool output.
- **The relay's own credentials.** The Webex bot token, the OpenCode server password, the PWA token,
  MCP secrets moved into the Keychain by `relay config import`.

## Trust boundaries

| Boundary | Inside | Outside |
|---|---|---|
| Webex | The pinned `personId`, in a 1:1 room with the bot | Everyone else, every group room |
| The Mac | Processes of this user (uid), the Keychain | Other users' processes, the network |
| Agent output | Nothing — tool output and repo content are untrusted | — |

relayd only makes outbound connections (the Webex websocket and REST API, `127.0.0.1` app servers).
Nothing listens on the network unless the PWA is enabled, and then only on loopback.

## Threats and answers

### A stolen or unlocked phone

The thief can read relayed sessions and answer approval cards for as long as the Webex app is signed
in. Limits:

- Approvals are manual by default. Read-only tools are the only auto-allowed ones, and a pattern that
  matches every tool is rejected at config load, so there is no global auto-approve.
- "Allow for session" covers one tool in one session and is deleted when the session ends. "Allow this
  tool in this workspace" covers one exactly named tool in one workspace (and folders under it) for
  `approvals.workspace_grant_s` (4 h), then expires.
- `/panic` (from any device signed in to the account) denies every pending request, cancels running
  turns and writes a pause flag that survives restarts. It asks first with a card valid for 60 s;
  `/panic now` skips the question. On the Mac, `relay stop` stops everything.
- Revoke the Webex session from the account settings; the bot then sees nothing from that device.

### A compromised Webex account, or someone else messaging the bot

- The first person to message the bot, with `webex.allowed_email`, is pinned by immutable `personId`.
  Later messages from any other person are ignored, even with the right email.
- Group rooms are ignored and left. Card submissions are re-checked against the pinned person and the
  room type, because a card can be submitted from a room it was forwarded to.
- A compromised account *is* the user. The answers above (manual approvals, `/panic`, revoking the
  session) are the containment.

### Other local processes

- The IPC socket is `0600` in a `0700` directory, and every connection's peer uid is checked.
- Secrets live in the Keychain; files the relay writes hold Keychain references, never values
  (`relay.toml` holds no secrets at all).
- The PWA, when enabled, binds `127.0.0.1` only and needs a bearer token compared in constant time.
  Use `tailscale serve` (`relay pwa expose`), never Funnel.
- Cursor's debug port gives full control of Cursor to any local process. It is opened only while the
  CDP adapter is enabled, and only by `relay selftest cursor-cdp` or the adapter itself, never by the
  background update check.

### Prompt injection through repo content into approval cards

A file or tool result can contain text crafted to look like an instruction, or to make an approval
card misleading ("this is a harmless read").

- Cards are built by the relay from the tool name and arguments the harness reports, not from model
  prose. Arguments are shown verbatim (truncated), so a destructive command looks destructive.
- The decision options are fixed (Allow once, for the session, for the workspace, Deny, Deny with a
  reason). Nothing in a card body can add an option or change what a button does; a submission carries
  only the request id, the decision, an optional reason and the card's one-use nonce. A reason is
  scrubbed and capped before it reaches the agent.
- Expired or already-answered requests cannot be answered again, and a card's nonce is spent by the
  first successful tap.
- URLs in agent output reach the phone as code spans, not links (`frontends/webex/links.py`), so an
  injected link is never one tap away.
- Message text and cards are never executed or rendered as HTML (the PWA uses `textContent`).

### Secrets leaking out

- Everything sent to Webex or the PWA is scrubbed: message text, card content and attachments.
  Attachments are text only (diffs, long answers), decoded and scrubbed before upload; anything that
  is not UTF-8 text is refused.
- Logs are scrubbed on every record. Message bodies are never logged at INFO; with
  `debug_bodies = true` and DEBUG level they are logged scrubbed and truncated. Both are off by default.
- `relay` commands scrub what they print, and config diffs hide the secrets they reveal.

### Several Webex bots

With `[webex.bots]`, relayd runs one bot per app. Each bot's token is its own Keychain item (service
`agent-relay`, account `webex-bot-<name>` by default); the config rejects two bots on one account and an
account that belongs to another secret. `relay setup bot <name> --stdin` takes the token from stdin only
(never argv, so never shell history or `ps`), refuses a terminal, and never prints it. Every bot pins
the same `webex.allowed_email` person and checks access on its own, exactly as the single bot does; a
thread stays with the bot that created it. A token that reached a transcript or a screen share should
be regenerated at developer.webex.com.

### The screen-lock probe

Auto away (`presence.auto_away_on_lock`) reads this login session's flags from CoreGraphics
(`CGSessionCopyCurrentDictionary`, in-process, no extra macOS permission) every `presence.poll_s`. Only
when that gives no answer for a session on the console does it run `/usr/sbin/ioreg -n Root -d1 -a`
directly (no shell), with a 5 s timeout; a hung probe is killed. Its output
is parsed and never logged. A probe that fails or reads nothing changes nothing, and a lock while paused
by `/panic` never turns the relay back on.

### Files from the phone

Incoming photos and files come only from the pinned person. The download goes only to the Webex API
host with the bot token. Each file is written under `<workspace>/<attachments.inbox_dir>/<session>/`
(default `.agent-relay/inbox`): folders `0700`, files `0600` created with `O_EXCL`/`O_NOFOLLOW`, a name
reduced to one safe path component, an inbox path that must stay inside the workspace, and no link
followed anywhere on the way. Files over `attachments.max_bytes` (20 MB) are refused even when the
download declares no length. The inbox is added to `.git/info/exclude`, never to the project's
`.gitignore`. Held files expire after `attachments.hold_s`, and a session's folder is deleted a day after
it ends. Incoming files are the user's own and are not scrubbed; what the relay sends out still is.

### Cloning from the phone

`/ws` clones only `https://<host>/<owner>/<repo>` or `git@<host>:<owner>/<repo>` with a host in
`ws.clone_hosts`, into a new folder directly under a configured root. It runs `git clone -- <url>
<target>` without a shell and with only `HOME`, `PATH`, `SSH_AUTH_SOCK` and `GIT_TERMINAL_PROMPT=0` in
the environment, in its own process group under `ws.clone_timeout_s`; on timeout the whole group is
killed. git's output is scrubbed and cut to its last lines before the phone sees it, and a failed or
partial clone is removed.

## Controls and their tests (AC-40)

Each §5.10 requirement, where it is enforced, and the test that fails if it regresses.

| Control | Enforced in | Tests |
|---|---|---|
| No TCP listener except the PWA, on loopback | `frontends/pwa/server.py` (`LOOPBACK`) | `tests/test_security.py::test_only_the_pwa_opens_a_tcp_listener_and_only_on_loopback`, `tests/frontends/test_pwa.py` |
| Unix sockets `0600`, directory `0700`, peer uid checked | `ipc.py` | `tests/test_ipc.py::test_roundtrip_and_mode`, `::test_refuses_other_uid` |
| Only the pinned `personId`; 1:1 rooms only | `frontends/webex/access.py` | `tests/frontends/test_access.py` (first contact pins, others ignored, group rooms left) |
| Every card submission re-checked | `frontends/webex/bot.py`, `frontends/actions.py` | `tests/frontends/test_access.py::test_card_submission_from_other_person_is_ignored`, `::test_card_submission_from_group_room_is_ignored`, `::test_card_before_any_pinning_is_ignored` |
| Approvals manual by default | `models/config.py` (`READ_ONLY_TOOLS`) | `tests/test_security.py::test_approvals_are_manual_by_default_for_anything_that_acts`, `tests/core/test_approvals.py::test_read_only_policy` |
| No global auto-approve | `ApprovalConfig._no_global_auto_approve` | `tests/test_security.py::test_no_global_auto_approve`, `tests/test_cli_ops.py::test_config_set_rejects_bad_values_and_leaves_the_file` |
| "Allow for session" ends with the session | `core/registry.py`, `core/approvals.py` | `tests/test_security.py::test_allow_for_session_ends_with_the_session`, `tests/core/test_hooks.py::test_allow_for_session_skips_later_prompts` |
| The relay's deadline denies before the hook times out | `core/approvals.py`, hook installers | `tests/core/test_hooks.py` (AC-30) |
| Scrub everything outbound | `frontends/webex/api.py`, `frontends/pwa/hub.py`, `log.py`, `cli/output.py` | `tests/test_scrub.py`, `tests/frontends/test_pwa.py`, `tests/test_cli_sync.py` |
| Attachments scrubbed as text before upload | `frontends/webex/api.py::scrub_attachment` | `tests/test_security.py::test_attachments_are_scrubbed_as_text_and_binary_is_refused` |
| No bodies at INFO; DEBUG bodies scrubbed and off by default | `log.py::log_body`, `debug_bodies` | `tests/test_security.py::test_message_bodies_are_off_by_default_never_at_info_and_scrubbed` |
| CDP port only while the adapter is enabled | `adapters/factory.py`, `cli/selftest.py`, `adapters/selftests.py` | `tests/test_security.py::test_the_cdp_adapter_exists_only_when_enabled`, `tests/test_cli_ops.py::test_cdp_selftest_refuses_while_the_adapter_is_off`, `tests/adapters/test_selftests.py` |
| PWA fails closed without a token | `daemon.py`, `frontends/pwa/server.py` | `tests/frontends/test_pwa.py` |
| `/panic` pauses across restarts | `core/service.py::panic`, `paths.pause_flag` | `tests/frontends/test_flows.py::test_panic_pauses_and_cancels`, `tests/test_cli_ops.py::test_online_message_mentions_recovery_and_pause` |
| Secrets only in the Keychain | `settings.py`, `sync/secrets.py` | `tests/test_settings.py`, `tests/sync/test_config_sync.py` |
| Bot tokens through stdin only, never printed; one account per bot | `cli/setup.py`, `models/config.py::WebexConfig` | `tests/test_setup_bot.py::test_setup_bot_stores_the_token_from_stdin_and_never_prints_it`, `::test_setup_bot_refuses_without_storing_or_echoing`, `::test_config_set_adds_a_bot_and_rejects_shared_accounts`, `tests/test_daemon.py::test_a_missing_app_bot_token_exits_2_with_the_stdin_setup_command` |
| Every bot pins the same person; threads stay with their bot | `daemon.py`, `frontends/webex/routing.py` | `tests/test_daemon.py::test_serve_runs_one_websocket_per_bot_and_each_app_bot_says_hello`, `tests/frontends/test_bots.py::test_events_follow_the_bot_recorded_on_the_session`, `::test_new_in_an_app_space_refuses_another_apps_session` |
| Screen-lock probe: no shell, bounded, fails safe | `core/presence.py` | `tests/core/test_presence.py::test_probe_gives_up_on_a_hung_command`, `::test_probe_is_unknown_when_the_command_fails`, `::test_an_unknown_probe_changes_nothing`, `::test_a_paused_relay_is_not_resumed_by_a_lock` |
| Phone files stay owner-only inside the workspace, capped, never through a link | `core/inbox.py`, `frontends/webex/attachments.py` | `tests/core/test_inbox.py::test_a_file_lands_in_the_session_folder_owner_only`, `::test_a_planted_link_with_the_same_name_is_never_written_through`, `::test_an_inbox_dir_must_stay_inside_the_workspace`, `::test_a_file_over_the_cap_is_refused_and_nothing_is_left`, `::test_sweeping_never_follows_a_link_or_deletes_a_foreign_folder`, `tests/frontends/test_attachments_intake.py::test_a_file_named_by_the_sender_cannot_choose_where_it_lands`, `::test_the_cap_holds_even_when_the_download_declares_no_length`, `::test_the_bot_token_never_leaves_the_webex_api_host` |
| Clones: allowed hosts only, no shell, minimal env, killed on timeout, nothing left on failure | `core/workspaces.py` | `tests/core/test_workspaces_phone.py::test_other_urls_are_refused`, `::test_a_host_outside_the_allow_list_is_refused_and_the_list_is_named`, `::test_git_sees_only_the_allow_listed_environment`, `::test_clone_runs_git_without_a_shell_and_with_the_allow_listed_environment`, `::test_a_clone_that_takes_too_long_is_killed_and_removed`, `::test_a_failure_removes_a_partial_checkout`, `::test_git_output_is_scrubbed_and_cut_to_the_last_lines` |
| Agent links neutralised | `frontends/webex/links.py` | `tests/frontends/test_links.py::test_links_become_code_spans`, `::test_agent_text_posted_to_webex_has_no_live_links` |
| Workspace grants match one tool in one workspace and expire | `core/approvals.py` | `tests/core/test_approvals_grants.py::test_a_grant_never_applies_to_a_different_workspace`, `::test_tool_matching_is_exact`, `::test_a_grant_lasts_until_its_expiry_and_no_longer`, `::test_reasons_are_scrubbed_collapsed_and_capped` |

## What this does not protect against

- Malware running as this user. It can read the Keychain items it is allowed to, talk to the socket,
  and drive the apps directly; the relay adds no new capability for it beyond what the apps expose.
- An agent doing something harmful with a tool the user approved. The card shows the command; read it.
- Webex itself. Message content transits the Webex service like any other Webex message.
