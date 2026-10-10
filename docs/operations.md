# Operations

## Install and uninstall

```bash
relay install            # dry run: every file, link, move and command it would run
relay install --diff     # the same, with a diff per file (secrets hidden)
relay install --apply    # do it
relay uninstall          # dry run
relay uninstall --apply  # undo it
```

Install writes the hooks for Copilot, Cursor and OpenCode, imports and syncs MCP config through
`~/.agents/mcp.yaml`, links `~/.agents/skills/agent-relay-away`, patches `stepping-away`, moves duplicate
skill folders, writes the launch agents and installs relay-bridge. Every touched file is backed up first
and listed in the manifest (`$RELAY_HOME/Library/Application Support/agent-relay/install-manifest.json`).
Running it again changes nothing. If a path it would link is a real folder, it stops before writing
anything.

Uninstall restores every file byte for byte, moves skill folders back, boots out the launch agents and
uninstalls relay-bridge. It keeps `relay.toml`, `workspaces.toml`, the session history, the logs and the
Keychain items (remove those with Keychain Access, service names `agent-relay*`).

Launch agents: `relayd` always; the OpenCode server while `adapters.opencode` is on; a weekly
`cursor-agent update` when `cursor-agent` is installed. Logs are in `~/Library/Logs/agent-relay/`.

## Running relayd

```bash
relay start                # load the launch agent and (re)start relayd
relay stop                 # unload it; it starts again at the next login
relay status [--json]      # sessions, parked turns, pending requests, adapter health
relay away on|off [--workspace <dir>]
```

Without `relay install`, run `uv run python -m agent_relay.daemon` in a terminal. After a start, relayd
posts "Mac online" in the bot room with any sessions it recovered.

## Workspaces and settings

```bash
relay ws add scratch ~/code/agent-relay/.scratch
relay ws add myapp ~/code/myapp.code-workspace
relay ws list
relay ws rm scratch
relay config show
relay config set cdp.enabled true          # values are JSON, else a string
relay config set approvals.deadline_s 300
```

A workspace is a folder or a multi-root `.code-workspace` file. For a file, `vscode` chats go to the
window that has every one of its folders open, and `code <file>` opens it when none does. The other
harnesses, and the claim that links a phone chat to its hooks, use the file's first folder: that is the
working directory VS Code reports for a multi-root window.

A setting the config model rejects (an unknown key, a global auto-approve pattern such as `*`) leaves
`relay.toml` unchanged. Restart relayd after changing a setting.

## relay-bridge (`vscode` chats)

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

Commands: `ping`, `models`, `start {query, model}` (new VS Code agent chat), `prompt {query, model}`,
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

## The PWA (off by default)

A browser front end with the same commands as Webex, for use over Tailscale.
It is not a replacement for Webex: relayd still needs the bot token.

1. Create a token: `security add-generic-password -U -s agent-relay -a pwa-token -w` (paste a long
   random value, for example from `openssl rand -base64 32`).
2. Set `pwa.enabled = true` in `relay.toml` (`pwa.port` defaults to 8787) and restart relayd. Without
   the token relayd refuses to start and says which command adds it.
3. Open `http://127.0.0.1:8787` and paste the token. It is kept for the browser tab only.
4. To reach it from the phone, `relay pwa expose` prints the `tailscale serve` command. The relay never
   runs it, and never uses Funnel.

The server listens on 127.0.0.1 only; the address is not configurable. Every `/api` route answers 401
without the token. Replies, token streams, tool lines, approvals and questions arrive over one event
stream, scrubbed like everything the relay sends.

## One MCP server list: `relay config import` and `relay config sync`

`~/.agents/mcp.yaml` is the single list of MCP servers. Edit it, then render it everywhere.

```sh
relay config import            # dry run: what would go into ~/.agents/mcp.yaml
relay config import --apply    # write it (once, or after adding a server in an app)
relay config sync              # diff of every harness file against mcp.yaml
relay config sync --apply      # write them; the first write of each file keeps a backup
```

- **Sources:** VS Code `mcp.json`, `~/.copilot/mcp-config.json`, `~/.cursor/mcp.json` and
  `opencode.jsonc`. Import takes the union. The `agent-relay` server (relay-mcp) is always added.
- **Secrets:** a literal secret found in a harness file (a token in `env`, an `Authorization`
  header, an OAuth client secret) moves to the Keychain under service `agent-relay-mcp`.
  `mcp.yaml` keeps a `${keychain:server.location}` reference. `sync` writes the literal back into
  each harness file, because the apps cannot read the Keychain. Diffs show `<redacted>` in its place.
  `${env:NAME}` references stay references.
- **Conflicts:** when a server's command or URL differs between apps, each app keeps its own
  definition (`variants` in `mcp.yaml`). Import lists every conflict.
- **App-only references:** a server that uses `${input:…}` (VS Code) or `{file:…}` (OpenCode) is
  rendered only into that app.
- **Comments:** VS Code's `mcp.json` and `opencode.jsonc` are rewritten as plain JSON. Comments are
  dropped; the backup keeps the original, and `relay uninstall` restores it.
- **Cursor CLI permissions:** `sync` also copies Cursor desktop's allowlist (shell commands, web
  domains, MCP tools) into `~/.cursor/cli-config.json`. Rules you added to the CLI by hand stay. A rule
  removed in the desktop is removed from the CLI on the next sync.

`sync` refuses to run before the first `import`: an empty list would remove every server.

## One copy of each skill: `relay skills dedupe`

```sh
relay skills dedupe            # what would move
relay skills dedupe --apply    # move the duplicates
```

A skill folder under `~/.copilot/skills`, `~/.cursor/skills`, `~/.claude/skills` or
`~/.config/opencode/skills` moves to `~/.agents/.skill-dup-backup/<timestamp>/<harness>/<name>` only
when it is byte-identical to `~/.agents/skills/<name>`. Symbolic links are compared by target and never
followed. A different copy is listed and kept.

## `relay doctor` and `/doctor`

```sh
relay doctor          # grouped, one line per check with the fix
relay doctor --json   # {"ok", "counts", "checks": [{area, name, status, detail, fix}]}
```

Each check is `ok`, `info`, `warn` or `fail`. The command exits with 1 when any check fails.

| Area | What it checks |
|---|---|
| relayd | the daemon answers on its socket |
| Adapters | each adapter's health; a degraded adapter shows the update that broke it; the Cursor CDP gate (why it is closed, or which self-test opened it) |
| Webex | the websocket is connected, or the last error |
| Away mode | global and per-workspace state, `/panic` pause, waiting requests |
| Versions | VS Code, Cursor, the Cursor CLI, OpenCode and the Copilot CLI; Cursor CLI more than 14 days older than the desktop app |
| Hooks | Copilot hooks, Cursor hooks and the OpenCode plugin are installed and match what `relay install` would write now |
| Config | `~/.agents/mcp.yaml` exists, and every harness file matches it |
| Skills | duplicate skill folders, the `agent-relay-away` link, the `stepping-away` patch |
| Keychain | the Webex bot token; the OpenCode password when that adapter is on; the PWA token when the PWA is on |
| launchd | each relay launch agent is loaded |

Without relayd, `relay doctor` reports the daemon as failing and still runs the local checks.
`/doctor` in Webex runs everything inside relayd.

## App updates

relayd checks VS Code's and Cursor's `Info.plist` every 10 minutes. When a version changes, it re-runs
that adapter's self-test (AHP protocol discovery for VS Code; selector checks for Cursor CDP, only if
Cursor is already running with its debug port) and posts the result.

- **Fail:** VS Code's agent host adapter is *paused* and Cursor CDP is *disabled*. New sessions, model
  lists and model changes are refused with the reason. `/stop` still works, and a prompt to a running
  session is queued as steering. The self-test is retried every check, and a pass is posted.
- **Inconclusive** (the app is not running): nothing is posted, and the next check retries.
- **First sighting** of an app only records its version.

## The Cursor CDP self-test

```bash
relay config set cdp.enabled true
relay selftest cursor-cdp
```

It attaches to Cursor on the debug port (launching Cursor with it only if Cursor is closed), checks every
selector in `selectors.json` and records a pass for this Cursor version. The CDP adapter refuses to run
until it passes. With `cdp.enabled` off the command refuses and nothing is opened.

## The live smoke test

With relayd running, the `scratch` workspace registered and the phone to hand:

```bash
(export RELAY_LIVE=1; uv run python scripts/smoke_live.py)               # every app
(export RELAY_LIVE=1; uv run python scripts/smoke_live.py --only opencode)
(export RELAY_LIVE=1; uv run python scripts/smoke_live.py --record)      # append results below
```

For each app the script asks you to start a session from the phone (or the desk, for desk-started
Cursor chats) with "Run the shell command `echo relay-smoke`, then reply with exactly: OK", then checks:

| Step | Checked by |
|---|---|
| register | relayd: a new session appears |
| stream | you: text streams into the Webex thread |
| approve | you: the approval card arrives and Allow runs the command |
| park | relayd: with `/away` on, the session parks after the turn |
| reply | you: a phone reply reaches the parked session |
| back | relayd: `/back` releases it |

A step that times out (`--timeout`, 300 s by default) fails and the script moves to the next app.

Send `/away` as a reply in the session's thread, not at the top level. A top-level `/away` turns
away mode on for every workspace, and every desk session with relay hooks then asks the phone before
each tool call. That includes the Cursor or VS Code chat you may be running the test from: its tools
wait for the phone until away mode goes off again (`relay away off`, or `/away off` in Webex), which
hands the waiting approvals back to the desk.

### Live smoke 2026-10-04 06:55 UTC

| App | Step | Result | Evidence |
|---|---|---|---|
| VS Code Local | register | ✅ | vscode_local:phone-6f3b92d31b1f41f2a34e8ef424aa0bd0 |
| VS Code Local | stream | ✅ | human: y |
| VS Code Local | approve | ✅ | human: y |
| VS Code Local | park | ✅ | vscode_local:phone-6f3b92d31b1f41f2a34e8ef424aa0bd0 parked |
| VS Code Local | reply | ✅ | human: y |
| VS Code Local | back | ✅ | vscode_local:phone-6f3b92d31b1f41f2a34e8ef424aa0bd0 released |

### Live smoke 2026-10-04 07:14 UTC

| App | Step | Result | Evidence |
|---|---|---|---|
| VS Code Copilot harness | register | ✅ | vscode_copilot:f077b1e0-6b86-4ade-a287-823a5f51b634 |
| VS Code Copilot harness | stream | ✅ | human: y |
| VS Code Copilot harness | approve | ✅ | human: y |
| VS Code Copilot harness | park | ✅ | vscode_copilot:f077b1e0-6b86-4ade-a287-823a5f51b634 parked |
| VS Code Copilot harness | reply | ✅ | human: y |
| VS Code Copilot harness | back | ✅ | vscode_copilot:f077b1e0-6b86-4ade-a287-823a5f51b634 released |

### Live smoke 2026-10-04 07:22 UTC

| App | Step | Result | Evidence |
|---|---|---|---|
| OpenCode | register | ✅ | opencode:ses_efa3584c7ffe8OcQqD0xpE3otB |
| OpenCode | stream | ✅ | human: y |
| OpenCode | approve | ✅ | human: y |
| OpenCode | idle | ✅ | opencode:ses_efa3584c7ffe8OcQqD0xpE3otB idle |
| OpenCode | reply | ✅ | human: y |

### Live smoke 2026-10-04 07:23 UTC

| App | Step | Result | Evidence |
|---|---|---|---|
| Cursor CLI | register | ✅ | cursor_cli:221381c9-a008-4f1a-afc5-50645d66fb3d |
| Cursor CLI | stream | ✅ | human: y |
| Cursor CLI | approve | ✅ | human: y |
| Cursor CLI | idle | ✅ | cursor_cli:221381c9-a008-4f1a-afc5-50645d66fb3d idle |
| Cursor CLI | reply | ✅ | human: y |

### Live smoke 2026-10-04 08:41 UTC

| App | Step | Result | Evidence |
|---|---|---|---|
| Cursor IDE (hooks and parking) | register | ✅ | cursor:0ebc1785-3f0c-432b-a495-4a4d4ee23c25 |
| Cursor IDE (hooks and parking) | stream | ✅ | human: y |
| Cursor IDE (hooks and parking) | approve | ✅ | human: y |
| Cursor IDE (hooks and parking) | park | ✅ | cursor:0ebc1785-3f0c-432b-a495-4a4d4ee23c25 parked |
| Cursor IDE (hooks and parking) | reply | ✅ | human: y |
| Cursor IDE (hooks and parking) | back | ✅ | cursor:0ebc1785-3f0c-432b-a495-4a4d4ee23c25 released |
