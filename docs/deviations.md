# Deviations

Every departure from `PLAN.md`, with the reason and where it lives. Spike evidence is in
[spikes.md](spikes.md).

| # | Plan said | Built instead | Why | Where |
|---|---|---|---|---|
| D1 | AHP chat URIs `ahp-chat:/<uuid>`; `createChat.initialMessage` starts the turn | `ahp-chat://<chat-id>/<base64url(session URI)>`; turns start with a dispatched `chat/turnStarted` action | The editor host rejects the planned URI and does not run `initialMessage` (S1) | `adapters/ahp/` |
| D2 | AHP sessions use the default isolation | `config: {isolation: "folder"}` unless the user asks for isolation | The default (`worktree`) created a branch in the user's repo during S1 | `adapters/ahp/` |
| D3 | Use `webex_bot` if it works on 3.13 | Vendored async port of its websocket client (MIT notice kept) | It imports, but blocks its own loop and sends across loops (S8) | `frontends/webex/ws_client.py` |
| D4 | One long-blocking stop hook for the whole park deadline | Sliced parking: each hook call blocks at most `park.slice_s` (25 min), then re-arms with "parking renewed" until `park.deadline_s` | S2 could not be measured before G1; slices stay safely under any hook timeout | `core/parking.py` |
| D5 | Wake idle sessions with `agent_idle` | Main sessions are woken with an AHP `chat/turnStarted` | `agent_idle` fires only for background agents (S3) | `adapters/ahp/`, `core/parking.py` |
| D6 | Cold-start Local into a closed workspace directly | Open the workspace first and wait for `relay-bridge` to register | No direct command exists (S5 fallback) | `adapters/vscode_local.py` |
| D7 | Cursor CDP adapter verified live | Ships disabled; enabling it is a config change after the live self-test | S6 confirmed the fuses allow a debug port but a live connect was not attempted | `adapters/cursor_cdp/` |
| D8 | "A card is edited once with its outcome" | The card message's text is edited once with the outcome; the card body itself stays (Webex `PUT /messages` cannot change attachments). Later clicks get "already answered" because the first answer wins in the registry | Webex API limitation | `frontends/webex/renderer.py`, `core/service.py` |
| D9 | Session root is "the session card" | The root is an editable markdown status line (`app · workspace · model · status`) | Only text can be edited, and the root needs its 8-edit budget | `frontends/webex/cards.py` |
| D10 | Device cleanup unspecified | Devices are registered as `agent-relay-relayd`; on start every older device with that name is deleted | Avoids touching registrations other clients of the same bot token made | `frontends/webex/ws_client.py` |
| D11 | The OpenCode plugin forwards `session.idle`, which is used for parking | OpenCode never parks. The adapter reads turn ends from each server's `/global/event` stream, and an idle session is woken with `prompt_async` | The server accepts a prompt at any time, so holding a hook open would only add a timeout to lose | `adapters/opencode/` |
| D12 | The plugin registers "its server" with `relayd` | Each registered server is its own key (`opencode.server:<url>`), and the adapter follows every one of them. A server that stops answering is forgotten | One TUI can run beside the managed server; a single key would lose one of them | `core/hooks.py`, `adapters/opencode/adapter.py` |
| D13 | Away mode decides what reaches the phone | Sessions started from the phone always reach it (approvals included), even with away mode off. Desk sessions still need away mode | A phone session has nobody at the desk to approve its tools | `core/hooks.py`, `adapters/opencode/adapter.py` |
| D14 | The launchd agent runs `opencode serve` with the password from Keychain | The agent runs `python -m agent_relay.ops.launchers opencode`, which reads Keychain and execs `opencode serve` on loopback | A plist cannot read Keychain, and a password must never sit in a plist | `ops/launchers.py`, `ops/launchd.py` |
