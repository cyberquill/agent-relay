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
