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

## relay-mcp: `notify_phone` and `ask_phone`

`relay-mcp` is a stdio MCP server that agents call to reach the phone through `relayd`. It finds the
session from `RELAY_WORKSPACE` (or its working directory) and picks the deepest live session that
contains it, busy sessions first.

- `notify_phone(text)` posts one line and returns `{delivered, reason}`.
- `ask_phone(question, options, timeout_s)` blocks for up to 900 s and returns `{outcome, answer, reason}`.
  `outcome` is `answered`, `timeout` or `denied`. It sends a progress notification every 20 s while
  waiting.

Both refuse (`delivered: false` or `denied`) when the relay is paused, when no session is live for the
workspace, or when a desk session is not in away mode. If `relayd` is down, the tools tell the agent to
run `relay start`. `relay-mcp` logs only to `~/Library/Logs/agent-relay/`; stdout belongs to MCP.

## `/away`, `/back` and the plugin bundles

`skills/agent-relay-away/SKILL.md` holds the away-mode contract. Its `commands/away.md` and `back.md`
run `relay away on|off --workspace "$PWD"`.

`relay install` writes hooks, skills and MCP config into each harness directly. To use a harness's plugin
manager instead, build the bundles. They are written to `dist/plugins/<harness>/agent-relay` and point at
this checkout's Python.

```bash
uv run python -c "from pathlib import Path; from agent_relay.ops.bundles import build_all; \
from agent_relay.models.config import RelayConfig; print(build_all(Path('dist/plugins'), RelayConfig()))"
```

| Harness | Install the bundle |
|---|---|
| Copilot CLI | `copilot plugin install dist/plugins/copilot/agent-relay` |
| Cursor | copy `dist/plugins/cursor/agent-relay` to `~/.cursor/plugins/local/agent-relay`, then restart Cursor |
| OpenCode | copy the contents of `dist/plugins/opencode/agent-relay` into `~/.config/opencode/` |

Use one route per harness, never both: hooks would fire twice.

## The `stepping-away` skill patch

`relay install` replaces `~/.agents/skills/stepping-away/SKILL.md` with `patches/stepping-away/SKILL.md`.
That version has no Webex posting, keeps the decision rules and points to `agent-relay-away`. It does
this only when the installed file's sha256 matches `patches/stepping-away/base.sha256`. The original is
backed up, and `relay uninstall` restores it byte for byte. If you edited the skill since, install
leaves it alone and says so. In that case, merge the patch by hand.
