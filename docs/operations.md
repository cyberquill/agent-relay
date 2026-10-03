# Operations

## relay-bridge (VS Code Local chats)

The `relay-bridge` extension runs in every VS Code window (`onStartupFinished`, `extensionKind: ui`)
and keeps one connection to `relay.sock` (the `relayBridge.socketPath` setting, then `RELAY_SOCKET`,
then `$RELAY_HOME/Library/Application Support/agent-relay/relay.sock`). It reconnects with backoff
(1 s doubling to 30 s) while relayd is down.

Protocol (length-prefixed JSON frames, as for every IPC peer):

| Direction | Frame |
|---|---|
| window → relayd | `{op: "bridge.register", protocol: 1, window_id, folders, version, vscode_version}` |
| relayd → window | `{ok: true, protocol: 1}`, then commands `{id, cmd, args}` |
| window → relayd | replies `{id, ok: true, result}` or `{id, ok: false, error}` |
| window → relayd | events `{event: "folders", folders}`, `{event: "turn.done"}`, `{event: "error", message}` |

Commands: `ping`, `models`, `start {query, model}` (new Local chat, agent mode), `prompt {query, model}`,
`cancel`, `acceptTool`, `skipTool`.

Build and package:

```bash
cd extensions/relay-bridge && npm ci && npm run build && npm test
uv run python -c "from pathlib import Path; from agent_relay.ops.vsix import build_vsix; build_vsix(Path('build/relay-bridge.vsix'))"
```

Without the extension, phone chats fall back to `code chat -m agent -r "<prompt>"` in the workspace;
`relay doctor` reports which path is active.

## Cursor CLI (ACP)

Phone chats for Cursor run `cursor-agent --workspace <ws> [--model <m>] [--worktree] acp` and talk ACP
over stdio. The CLI must be logged in once: `cursor-agent login`. A session survives a `relayd` restart or
a CLI crash: the next prompt respawns the CLI and calls `session/load`. At the desk, continue it with
`agent resume <id>` (the id is on the session card).

## Cursor desktop composer (CDP, off by default)

The CDP adapter types phone prompts into Cursor's own composer, so the chat appears on the desk. It is
fragile by design and stays off until all of these hold:

1. Install the extra: `uv sync --extra cdp`.
2. Set `[cdp] enabled = true` (and `port`, default 9223) in `relay.toml`.
3. Quit Cursor once. The relay starts it with `--remote-debugging-port=<port>
   --remote-debugging-address=127.0.0.1`; it never restarts a Cursor that is already open.
4. Open Cursor's chat pane and run `relay selftest cursor-cdp`. Every required selector must match.

The self-test result is stored per Cursor version and per `selectors.json` version. After a Cursor update
the adapter refuses to run (and `doctor` says why) until the self-test passes again. When a selector
breaks, edit `agent_relay/adapters/cursor_cdp/selectors.json`, bump its `version`, and rerun the
self-test. The debug port listens only on loopback and only while Cursor runs with it.

The DOM fixture tests need a Chromium binary: the newest Playwright Chromium in
`~/Library/Caches/ms-playwright`, Google Chrome, or the path in `RELAY_TEST_CHROMIUM`.
