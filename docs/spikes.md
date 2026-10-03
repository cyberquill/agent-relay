# Spikes

Run on 2026-10-03 against VS Code 1.140.0, Cursor 3.23.12, Cursor CLI 2026.07.17-3e2a980, OpenCode 1.18.15,
Python 3.13.5. Tokens are redacted in every command below. Fixtures live under `tests/fixtures/`.

| # | Outcome | Path chosen |
|---|---|---|
| S1 | **Yes**, with two corrections to the plan | Primary `ahp` path on the editor socket |
| S2 | Not measured live (needs hooks in the real home, so it waits for G1) | Sliced parking, always (the S2 fallback) |
| S3 | **No**, per vendor docs | AHP `chat/turnStarted` on the same chat |
| S4 | Not measured live | Park via `Stop` and also continue through `relay-bridge` (both built) |
| S5 | Not measured live | Fallback as specified, plus "open the workspace first" |
| S6 | Fuses allow it; live connect not attempted | CDP adapter ships disabled; `relay selftest cursor-cdp` gates it |
| S7 | Runs at C15, after gate G2 | — |
| S8 | **Imports, but unusable in an asyncio daemon** | Vendored async port of its websocket client |
| S9 | **Both variants exist** | Implement both; choose at runtime from `/doc` |
| S10 | **No**: `cursor-agent status` prints `Not logged in` | `doctor` prints `cursor-agent login`; the adapter reports "auth needed" |

## S1 — AHP on the editor socket

Command: `code agent endpoints`, then a WebSocket over the Unix socket in `endpoint.path`.

Evidence:
- The endpoint JSON is `{schemaVersion: 2, type: "editor", protocolVersion: "0.9.0", connectionToken, endpoint: {type: "socket", path}}`.
- Transport: the agent host is a WebSocket server. The token goes in the `tkn` query parameter of the upgrade
  URL (found in `agentHostMain.js`: `searchParams.getAll("tkn")`). One JSON-RPC message per text frame.
- `initialize {protocolVersions: ["0.9.0"], clientId, initialSubscriptions: ["ahp-root://"]}` returns the root
  snapshot with agents `copilotcli` (Copilot) and `claude`, each with a model list.
- `createSession {channel: "ahp-session:/<uuid>", provider: "copilotcli", workingDirectories: [file URI]}` works.
- **Correction 1:** chat URIs are `ahp-chat://<chat-id>/<base64url(session URI)>`, not `ahp-chat:/<uuid>`. The
  session's default chat is `ahp-chat://default/<base64url(session URI)>`. `ahp-chat:/<uuid>` fails with
  `Malformed AHP chat URI`.
- **Correction 2:** `createChat.initialMessage` is accepted but no turn runs (the chat snapshot keeps
  `turns: []`). A turn starts when the client dispatches `chat/turnStarted {turnId, startedAt, message}` with
  `dispatchAction`.
- Text arrives as `chat/responsePart {part: {kind: "markdown", content}}` as well as `chat/delta`. The adapter
  maps both.
- The `copilotcli` session config defaults to `isolation: "worktree"`, which created a branch in the scratch
  repo. The adapter passes `config: {isolation: "folder"}` unless the user asks for isolation.
- Live run (2 of 3 attempts used for this): `Reply with exactly: OK` returned `OK`, then `chat/turnComplete`.
  Trace in `tests/fixtures/ahp/ok_turn.trace.json` (scrubbed).

## S2 — Long-blocking stop hooks

Not measured: a 30-minute block needs hooks installed under the real home directory (G1) and a live IDE
turn. The documented limits are enough to choose a design:
- Copilot: command timeouts are configurable (`timeoutSec`) but **fail open**.
- Local: `timeout` in seconds, default 30.
- Cursor: per-script `timeout`; `stop` supports `followup_message`, `loop_limit: null`.

Path: **sliced parking everywhere**. The stop hook long-polls `relayd` for at most `park.slice_s` (default
25 min), which is always below the hook's configured timeout. When a slice expires with no phone message,
the hook returns a continuation that re-arms the park (`reason`/`followup_message` "parking renewed") and
the phone gets "parking renewed". Total parking is bounded by `park.deadline_s` (default 4 h). This is
correct whether or not the harness tolerates longer blocks, so no live measurement can change the design;
C15 smoke records the observed behaviour in `docs/operations.md`.

## S3 — Waking an idle Copilot session

Evidence (GitHub Copilot hooks reference, `notification` hook): `agent_idle` fires when "a **background**
agent finishes a turn and enters idle state". It does not fire for the main agent. `notification` is also
Copilot-CLI-only.

Path: wake the main agent with AHP `chat/turnStarted` on the session's chat. The hook still handles
`notification`/`agent_idle` for background agents.

## S4 — Local `Stop` reason

Evidence (VS Code Local hooks reference): `Stop` output `hookSpecificOutput {decision: "block", reason}`
"explains why the agent should continue", and `stop_hook_active` guards loops. Whether
`chat.agent.maxRequests` (default 25) caps parked continuations was not measured.

Path: park Local via `Stop` **and** build the fallback: when `stop_hook_active` is set and the session has
continued `park.local_max_continuations` times (default 20, below the 25 cap), the relay continues the chat
through `relay-bridge` `workbench.action.chat.open` with `preserveInput`. `docs/operations.md` recommends
`chat.agent.maxRequests: 100` for away mode.

## S5 — `code chat -r` window targeting

Not measured live (each attempt opens a chat in a real window). Path: the fallback runs
`code <workspace>` first, waits up to 20 s for `relay-bridge` to register a window containing the
workspace, then sends through the extension. Without the extension the relay runs
`code chat -m agent -r "<prompt>"` from the workspace directory and tells the phone "open the workspace
first" if no window holds it.

## S6 — Cursor remote debugging

Command: `npx @electron/fuses read --app /Applications/Cursor.app`.

Evidence: `RunAsNode Enabled`, `EnableNodeCliInspectArguments Enabled`; there is no fuse for
`--remote-debugging-port`, so the Chromium switch is not blocked. A live connect was not attempted:
relaunching Cursor with the flag would end the running Cursor session. Path: the adapter ships disabled;
`relay selftest cursor-cdp` checks every selector before it can be enabled, and `doctor` explains the state.

## S8 — `webex_bot` on Python 3.13

Command: `uv pip install webex_bot` in a 3.13 venv; import `WebexBot` and `WebexWebsocketClient`.

Evidence: version 1.3.1 imports cleanly (with `websockets` 16.1, `webexpythonsdk` 2.0.6). But its client:
- blocks its own loop with `asyncio.get_event_loop().run_until_complete(...)`, so it cannot run inside
  `relayd`'s event loop;
- acks each message with `asyncio.run(self.websocket.send(...))` from an executor thread, against a
  connection owned by another loop;
- uses blocking `requests` for every REST call.

Path: vendor its websocket client as `agent_relay/frontends/webex/ws_client.py`, ported to asyncio and
`httpx`, keeping its MIT notice. The protocol steps are unchanged: U2C catalog → WDM `devices` → websocket
→ `authorization` frame → `conversation.activity` → ack.

## S9 — OpenCode permission and question endpoints

Command: `opencode serve --port 4097` in `.scratch/`, then `GET /doc`.

Evidence (`tests/fixtures/opencode/doc.json`):
- permissions, current: `POST /permission/{requestID}/reply {reply: once|always|reject, message?}`
- permissions, legacy: `POST /session/{sessionID}/permissions/{permissionID} {response: once|always|reject}`
- questions: `POST /question/{requestID}/reply {answers: string[][]}`, `POST /question/{requestID}/reject`
- events: `permission.asked {id, sessionID, permission, patterns, tool?}`,
  `question.asked {id, sessionID, questions[{question, header, options[{label}]}]}`
- the SSE envelope on `/global/event` is `{directory, project, payload: {id, type, properties}}`.
- Live run (1 of 3): a session with `github-copilot/gpt-5-mini` streamed `message.part.delta "OK"` and then
  `session.idle`. Fixture `tests/fixtures/opencode/ok_session.sse`.

Path: the adapter reads `/doc` at connect time and uses the current endpoints when present, the legacy
ones otherwise.

Live attempt 2 of 3 (C6, AC-20, `tests/live/test_opencode_live.py`): the adapter started a session in
`.scratch/` on a throwaway `opencode serve`, streamed `OK` and aborted. One attempt remains for C15.

## S10 — Cursor CLI auth

Command: `cursor-agent status` → `Not logged in`.

Path: `relay doctor` reports "Cursor CLI not logged in: run `cursor-agent login`". The adapter checks status
before `authenticate` and reports `auth needed` to the phone instead of starting the process. Live test
AC-22 needs the login first; it is a C15 step.
