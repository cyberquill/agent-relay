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

The thief can read relayed sessions and start or steer agents for as long as the Webex app is signed
in. **Nothing slows them down: the relay has no approvals** (see the next section). Limits:

- `/panic` (from any device signed in to the account) closes every open question, cancels running
  turns, turns away mode off and writes a pause flag that survives restarts. While paused the relay
  answers nothing, so a desk app asks at its own prompt. It asks first with a card valid for 60 s;
  `/panic now` skips the question. `/away` resumes. On the Mac, `relay stop` stops everything.
- Revoke the Webex session from the account settings; the bot then sees nothing from that device.

### A compromised Webex account, or someone else messaging the bot

- The first person to message the bot, with `webex.allowed_email`, is pinned by immutable `personId`.
  Later messages from any other person are ignored, even with the right email.
- Group rooms are ignored and left. Card submissions are re-checked against the pinned person and the
  room type, because a card can be submitted from a room it was forwarded to.
- **Whoever controls the Webex account can run anything on the Mac.** That was already true through a
  prompt ("run this command"); since D117 there is also no approval step that could slow it. A
  compromised account *is* the user.
- The controls that are left, and the only containment: the pinned person id, 1:1 rooms only, `/panic`,
  revoking the Webex session, the logs (`relayd.log`, scrubbed), and the "never do while away" list in the `agent-relay-away` skill, which binds the agent,
  not the relay.

### No approvals (a documented deviation)

PLAN §5 and the earlier version of this page said the relay would "never" have a global auto-approve.
That is reversed by [D117](deviations.md): every tool call the relay answers is allowed at once.

- **Why (F7):** of 68 approval cards, the phone answered 33 and every answer was an allow; 21 (31 %)
  timed out and were auto-denied, which stalled the turn; 14 were answered at the desk. The card was
  friction, not control, because the same person could already send any prompt.
- **What the relay answers:** only phone-held sessions and, while away mode is on, desk sessions. A desk
  session at the desk, and every session while `/panic` has paused the relay, keeps the app's own prompt
  (`{}` for hooks, no answer for adapters). A hook that fails returns `{}` too (D124).
- **App flags:** phone sessions are also started with the app's own auto-approve where one exists
  (Cursor CLI `--force`, AHP `autoApprove`, OpenCode `always`; D125). They are not verified live; the
  relay's own allow does not depend on them.
- **Questions still wait.** `ask_phone`, AHP input, OpenCode questions and Cursor `ask_question` are
  answered by you, with the one-use nonce and the `questions.deadline_s` deadline.
- There is no deny list.

### Other local processes

- The IPC socket is `0600` in a `0700` directory, and every connection's peer uid is checked.
- Secrets live in the Keychain; files the relay writes hold Keychain references, never values
  (`relay.toml` holds no secrets at all).
- The PWA, when enabled, binds `127.0.0.1` only and needs a bearer token compared in constant time.
  Use `tailscale serve` (`relay pwa expose`), never Funnel.
- Cursor's debug port gives full control of Cursor to any local process. It is opened only while the
  CDP adapter is enabled, and only by `relay selftest cursor-cdp` or the adapter itself, never by the
  background update check.

### Prompt injection through repo content into what the phone shows

A file or tool result can contain text crafted to look like an instruction, or to make an agent's
answer or question misleading. Question cards are the only cards that carry agent text.

- Question options are fixed by the question; nothing in a card body can add a button or change what a
  button does. A submission carries only the request id, the chosen option or typed answer and the
  card's one-use nonce, and the server checks it again. Expired or already-answered questions cannot be
  answered again, and a card's nonce is spent by the first successful tap.
- The `/new` Start value (`harness:model`) is checked again against the offer stored under the card's
  nonce, so a forged `agent` is refused. Card JSON is checked in CI against the Webex subset of
  Adaptive Cards 1.3 (`tests/frontends/card_spec.py`).
- URLs in agent output reach the phone as code spans, not links (`frontends/text.py`, re-exported by
  `frontends/webex/links.py`), so an injected link is never one tap away.
- Message text and cards are never executed or rendered as HTML by the relay (the PWA uses
  `textContent`); the one HTML artefact is `answer.html`, below.

### HTML output (`answer.html`, `changes.html`)

A long or rich answer, and a `/diff` over 200 lines, goes out as one self-contained HTML file
(`frontends/rich.py`, shared by Webex and the PWA). It must stay inert even when the agent's text is
hostile.

- Rendered with `markdown-it-py` with `html=False`: raw HTML in agent text is escaped, never passed
  through. A `<script>` in a table cell, heading or emphasis is escaped too.
- The page carries `<meta http-equiv="Content-Security-Policy" content="default-src 'none';
  style-src 'unsafe-inline'; img-src data:">`: no script and no external resource can load, whichever
  app opens the file.
- No link is live: a URL stays text (the label, then the address in a code span), there is no `<a>` and
  no `href` anywhere, images become `[image: alt]`, and `javascript:` and reference links get the same
  treatment (D90).
- The source text, the finished HTML and the preview each go through `scrub` before they leave the
  process. If rendering raises, the answer falls back to text chunks.
- Open check: whether Webex Android opens the file in the app or hands it to the browser, and that the
  CSP survives either path. The E2E driver checks the downloaded file (CSP present, no `<script`, no
  `href=`, no external `src=`, `url(http` or `@import`).

### Secrets leaking out

- Everything sent to Webex or the PWA is scrubbed: message text, card content and attachments.
  Generated attachments (diffs, `answer.html`) are scrubbed before upload. A file an agent sends with
  `send_phone_file` is scrubbed when it is text and passes through byte for byte when it is binary (a
  PNG, a PDF); `scrub_attachment` still refuses non-text for the relay's own attachments.
- Logs are scrubbed on every record. Message bodies are never logged at INFO; with
  `debug_bodies = true` and DEBUG level they are logged scrubbed and truncated. Both are off by default.
- `relay` commands scrub what they print, and config diffs hide the secrets they reveal.

### The Webex token

There is one bot and one token (Keychain service `agent-relay`, account `webex-bot-token`).
`relay setup token --stdin` takes it from stdin only (never argv, so never shell history or `ps`),
refuses a terminal, and never prints it. relayd reads it again before every Webex attempt and keeps
running when Webex rejects it (D121); `relay doctor` tests it with `GET /people/me` and reports a
rejection without printing it. A token that reached a transcript or a screen share should be
regenerated at developer.webex.com. The three app bots of the October round are retired (D116); their
Keychain items are deleted and the bots are deleted by the owner at developer.webex.com.

### Away mode is manual

Nothing turns away mode on by itself: there is no screen-lock probe and no automatic trigger (D120).
The flag is global and only `/away`, `/back` and `relay away on|off` change it, so a locked or sleeping
Mac never hands desk sessions to the phone on its own. While away is off, the router withholds the
events of desk-started sessions from the phone and the relay answers none of their tool calls.

### The E2E driver's person token

`scripts/phone_e2e.py` acts as the owner so that card taps and file uploads can be checked through Webex
(D123). It needs a person token, which is far more powerful than the bot's.

- It is read through `webex-cli`'s `get_access_token()` (the OAuth token `webex-cli` already keeps),
  never from a file the relay writes, and the relay's Keychain never holds it.
- It is sent only to `https://webexapis.com/v1/`, redirects are never followed, and every error text goes
  through a redactor that knows the token plus `agent_relay.scrub`. It is never printed, logged, put in
  an exception, a report or a fixture; the tests use a fake token.
- The driver refuses to start without `RELAY_LIVE=1`, refuses a room whose other member is not a
  `@webex.bot` account, runs live sessions only in `.scratch` with at most 3 per adapter per run, and
  deletes the messages it posted at the end.

### `send_phone_file`: the workspace jail

The relay-mcp tool `send_phone_file(path, caption?)` lets an agent hand a file to the phone, so the path
is untrusted input (`core/send_file.py`).

- The path (relative to the workspace, absolute, or `~/…`, which is expanded and then judged like any
  absolute path) is resolved with symlinks followed and must lie inside the session's resolved
  workspace. A link inside the workspace that points out is refused, and so is a linked folder.
- The target must be a regular file (directories, FIFOs and devices are refused; it is opened with
  `O_NOFOLLOW|O_NONBLOCK` and checked again with `fstat`) and at most `attachments.max_bytes`, checked
  before reading and again after scrubbing.
- Text is scrubbed; binary goes through as it is. Error messages name the path the agent gave, never the
  resolved target.
- Like `notify_phone` it is honoured only for phone-held sessions and, while away, desk sessions, and
  never while the relay is paused.

### Files from the phone

Incoming photos and files come only from the pinned person. The download goes only to the Webex API
host with the bot token. A file sent at the top level is held in `<app-support>/inbox-staging/` (`0700`,
files `0600`) until the next `/new` or `attachments.hold_s`, then moved into the new session's inbox or
swept by housekeeping. A failed or unsupported download is named in one `⚠️` line, never skipped
silently. Each file is written under `<workspace>/<attachments.inbox_dir>/<session>/`
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
| The relay posts no approval card and allows every call it answers (D117; the old "never a global auto-approve" is superseded, reason F7) | `core/hooks.py`, `core/approvals.py` (`intercept`), `hook/outputs.py` | `tests/core/test_no_approvals.py::test_relay_never_posts_an_approval_card`, `::test_every_relay_answered_tool_call_is_allowed`, `::test_every_hook_answered_tool_call_is_allowed`, `tests/frontends/test_no_approval_card.py::test_there_is_no_approval_action_and_no_approval_card_builder` |
| A desk session at the desk, and everything after `/panic`, is left to the app's own prompt | `core/hooks.py`, `core/approvals.py` | `tests/core/test_no_approvals.py::test_desk_sessions_at_the_desk_are_left_to_the_app`, `::test_nothing_is_answered_after_a_panic`, `tests/core/test_hooks.py::test_a_hook_failure_leaves_the_tool_call_to_the_app` |
| Legacy `approvals.*` settings are ignored; only questions wait for the phone | `models/config.py` | `tests/test_security.py::test_there_is_no_approval_config_and_a_legacy_one_is_ignored`, `::test_questions_are_the_only_thing_that_waits_for_the_phone` |
| Nothing turns away mode on by itself; desk events stay off the phone while it is off | `core/away.py`, `core/router.py` | `tests/core/test_away_global.py::test_nothing_turns_away_on_by_itself`, `::test_desk_events_are_dropped_while_not_away`, `::test_an_adopted_desk_chat_stops_streaming_the_moment_away_goes_off` |
| Scrub everything outbound | `frontends/webex/api.py`, `frontends/pwa/hub.py`, `log.py`, `cli/output.py` | `tests/test_scrub.py`, `tests/frontends/test_pwa.py`, `tests/test_cli_sync.py` |
| Attachments scrubbed as text before upload | `frontends/webex/api.py::scrub_attachment` | `tests/test_security.py::test_attachments_are_scrubbed_as_text_and_binary_is_refused` |
| No bodies at INFO; DEBUG bodies scrubbed and off by default | `log.py::log_body`, `debug_bodies` | `tests/test_security.py::test_message_bodies_are_off_by_default_never_at_info_and_scrubbed` |
| CDP port only while the adapter is enabled | `adapters/factory.py`, `cli/selftest.py`, `adapters/selftests.py` | `tests/test_security.py::test_the_cdp_adapter_exists_only_when_enabled`, `tests/test_cli_ops.py::test_cdp_selftest_refuses_while_the_adapter_is_off`, `tests/adapters/test_selftests.py` |
| PWA fails closed without a token | `daemon.py`, `frontends/pwa/server.py` | `tests/frontends/test_pwa.py` |
| `/panic` pauses across restarts | `core/service.py::panic`, `paths.pause_flag` | `tests/frontends/test_flows.py::test_panic_pauses_and_cancels`, `tests/core/test_away_global.py::test_panic_pauses_forces_away_off_and_closes_questions`, `::test_set_away_for_on_resumes_after_a_panic`, `tests/frontends/test_panic_confirm.py::test_confirming_cancels_everything_and_pauses` |
| Secrets only in the Keychain | `settings.py`, `sync/secrets.py` | `tests/test_settings.py`, `tests/sync/test_config_sync.py` |
| The Webex token goes through stdin only and is never printed | `cli/setup.py`, `settings.py` | `tests/test_setup_token.py::test_setup_token_stores_the_token_from_stdin_and_never_prints_it`, `::test_setup_token_refuses_without_storing_or_echoing`, `tests/test_daemon.py::test_missing_token_exits_2_with_setup_command` |
| A rejected Webex token never stops relayd and is never printed | `frontends/webex/host.py`, `doctor/local.py` | `tests/test_daemon.py::test_startup_401_keeps_relayd_up`, `tests/doctor/test_local.py::test_doctor_reports_rejected_token` |
| A forged `/new` agent is refused | `frontends/webex/handlers.py`, `core/service/discovery.py` | `tests/frontends/test_new_card.py::test_a_forged_agent_is_refused_even_for_an_enabled_app_model`, `::test_the_card_is_checked_again_on_the_server` |
| Card JSON stays inside the Webex subset of Adaptive Cards 1.3 | `tests/frontends/card_spec.py` | `tests/frontends/test_card_spec.py::test_every_card_builder_passes_the_webex_subset_with_maximal_inputs`, `::test_action_toggle_visibility_is_refused`, `::test_an_oversized_card_is_refused` |
| `answer.html` is inert: CSP, raw HTML escaped, no `href`, scrubbed | `frontends/rich.py` | `tests/test_rich.py::test_the_csp_is_present_exactly_once_and_forbids_everything_but_inline_css`, `::test_raw_html_in_the_text_is_escaped`, `::test_script_in_a_table_cell_heading_and_emphasis_is_escaped`, `::test_links_are_text_with_no_href_anywhere`, `::test_the_scrubber_runs_on_the_html_and_the_preview`, `::test_the_diff_page_has_the_same_csp_and_escapes_everything` |
| `send_phone_file` stays inside the workspace, never through a link, capped | `core/send_file.py`, `daemon.py` (`send_file` op) | `tests/core/test_send_file.py::test_dot_dot_out_of_the_workspace_is_refused`, `::test_an_absolute_path_outside_the_workspace_is_refused`, `::test_a_symlink_inside_pointing_outside_is_refused`, `::test_a_symlinked_folder_pointing_outside_is_refused`, `::test_an_oversized_file_is_refused`, `::test_a_fifo_is_refused_without_blocking`, `::test_a_text_file_is_scrubbed`, `tests/test_daemon.py::test_send_file_goes_to_the_session_thread_and_stays_inside_the_workspace` |
| Top-level files are held privately and left behind when their time runs out | `core/inbox.py`, `frontends/webex/attachments.py` | `tests/frontends/test_attachments_intake.py::test_files_held_at_the_top_level_are_left_behind_when_their_time_runs_out`, `tests/core/test_inbox.py::test_held_top_level_is_none_once_the_time_is_up_and_leaves_the_files_for_the_sweep` |
| The driver's person token never leaves `webexapis.com` and never reaches an error or report | `scripts/e2e/webex_cli_user.py`, `scripts/e2e/model.py` | `tests/live/test_phone_e2e.py::test_an_error_from_webex_never_carries_the_token`, `::test_the_token_is_only_ever_sent_to_webexapis`, `::test_a_failed_refresh_in_webex_cli_is_a_safe_error`, `::test_a_setup_error_is_scrubbed_and_exits_two`, `tests/test_e2e_driver.py::test_an_error_text_never_carries_a_secret_into_the_report`, `::test_the_session_cap_stops_a_run_before_it_sends_anything`, `tests/live/test_phone_e2e.py::test_the_driver_refuses_to_run_without_relay_live_one` |
| Phone files stay owner-only inside the workspace, capped, never through a link | `core/inbox.py`, `frontends/webex/attachments.py` | `tests/core/test_inbox.py::test_a_file_lands_in_the_session_folder_owner_only`, `::test_a_planted_link_with_the_same_name_is_never_written_through`, `::test_an_inbox_dir_must_stay_inside_the_workspace`, `::test_a_file_over_the_cap_is_refused_and_nothing_is_left`, `::test_sweeping_never_follows_a_link_or_deletes_a_foreign_folder`, `tests/frontends/test_attachments_intake.py::test_a_file_named_by_the_sender_cannot_choose_where_it_lands`, `::test_the_cap_holds_even_when_the_download_declares_no_length`, `::test_the_bot_token_never_leaves_the_webex_api_host` |
| Clones: allowed hosts only, no shell, minimal env, killed on timeout, nothing left on failure | `core/workspaces.py` | `tests/core/test_workspaces_phone.py::test_other_urls_are_refused`, `::test_a_host_outside_the_allow_list_is_refused_and_the_list_is_named`, `::test_git_sees_only_the_allow_listed_environment`, `::test_clone_runs_git_without_a_shell_and_with_the_allow_listed_environment`, `::test_a_clone_that_takes_too_long_is_killed_and_removed`, `::test_a_failure_removes_a_partial_checkout`, `::test_git_output_is_scrubbed_and_cut_to_the_last_lines` |
| Agent links neutralised | `frontends/text.py`, `frontends/webex/links.py` | `tests/frontends/test_links.py::test_links_become_code_spans`, `::test_agent_text_posted_to_webex_has_no_live_links` |

## What this does not protect against

- Malware running as this user. It can read the Keychain items it is allowed to, talk to the socket,
  and drive the apps directly; the relay adds no new capability for it beyond what the apps expose.
- An agent doing something harmful with a tool call. There are no approvals and no deny list (D117): the
  relay allows every call it answers, and the "never do while away" list in the `agent-relay-away`
  skill binds the agent, not the relay. `/panic` stops the turns; it cannot undo what already ran.
- Whoever controls the Webex account (see above): they can run anything on this Mac, and nothing
  slows them.
- Webex itself. Message content transits the Webex service like any other Webex message.
