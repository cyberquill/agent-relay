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
- "Allow for session" covers one tool in one session and is deleted when the session ends.
- `/panic` (from any device signed in to the account) denies every pending request, cancels running
  turns and writes a pause flag that survives restarts. On the Mac, `relay stop` stops everything.
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
- The decision options are fixed (Allow, Allow for session, Deny). Nothing in a card body can add an
  option or change what a button does; a submission carries only the request id and the decision.
- Expired or already-answered requests cannot be answered again.
- Message text and cards are never executed or rendered as HTML (the PWA uses `textContent`).

### Secrets leaking out

- Everything sent to Webex or the PWA is scrubbed: message text, card content and attachments.
  Attachments are text only (diffs, long answers), decoded and scrubbed before upload; anything that
  is not UTF-8 text is refused.
- Logs are scrubbed on every record. Message bodies are never logged at INFO; with
  `debug_bodies = true` and DEBUG level they are logged scrubbed and truncated. Both are off by default.
- `relay` commands scrub what they print, and config diffs hide the secrets they reveal.

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

## What this does not protect against

- Malware running as this user. It can read the Keychain items it is allowed to, talk to the socket,
  and drive the apps directly; the relay adds no new capability for it beyond what the apps expose.
- An agent doing something harmful with a tool the user approved. The card shows the command; read it.
- Webex itself. Message content transits the Webex service like any other Webex message.
