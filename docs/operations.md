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
`~/.agents/mcp.yaml`, links each relay skill (`agent-relay-away`, `agent-relay-recall`) into
`~/.agents/skills`, patches `stepping-away`, moves duplicate
skill folders, writes the launch agents and installs relay-bridge. Every touched file is backed up first
and listed in the manifest (`$RELAY_HOME/Library/Application Support/agent-relay/install-manifest.json`).
Running it again changes nothing, and only a launch agent whose plist changed is reloaded. If a path it
would link is a real folder, it stops before writing anything. Backups are `0600` in `0700` folders, and
the manifest records the sha256 of every file the relay wrote.

Uninstall restores a file only if it is still what the relay last wrote. A file changed since is
unmerged where the relay knows how (Cursor `hooks.json` and the MCP configs lose only the relay's own
entries) and otherwise left alone, with its backup path in the report. It then moves skill folders back,
boots out the launch agents and
uninstalls relay-bridge. It keeps `relay.toml`, `workspaces.toml`, the session history, the logs and the
Keychain items (remove those with Keychain Access, service names `agent-relay*`).

Launch agents: `relayd` always; the OpenCode server while `adapters.opencode` is on; a weekly
`cursor-agent update` when `cursor-agent` is installed. Logs are in `~/Library/Logs/agent-relay/`.
With `adapters.opencode` off, `relay start` boots an installed OpenCode server out and runs
`launchctl disable` on it, so it no longer starts at login; turning the adapter on and running
`relay start` enables it again.

## Running relayd

```bash
relay start                # load the launch agents (relayd, OpenCode server) and (re)start them
relay stop                 # unload them; they start again at the next login
relay status [--json]      # sessions, parked turns, open questions, adapter health
relay away on|off          # the one global flag; --workspace is accepted and ignored
```

Without `relay install`, run `uv run python -m agent_relay.daemon` in a terminal. relayd posts nothing
when it starts: the phone is not told about restarts, updates or adapter crashes. They are in the log
and in `/doctor`.

At start relayd also marks sessions left `running` with no live turn as `idle`, and housekeeping ends
idle sessions untouched for 7 days.

### relayd survives a Webex failure

A rejected Webex token (HTTP 401 or 403), a device-registration error, any other HTTP error, a network
error or a timeout, at start or later, does **not** stop relayd. The Webex side retries with backoff
(1 s doubling to 5 min) and logs once per change of state. Meanwhile the IPC socket keeps serving, so
hooks fail open at once and the desk apps are not slowed down. The stored token is read again before
every attempt, so after

```bash
pbpaste | relay setup token --stdin
```

the next retry picks it up with no restart (at most about 5 minutes). `relay doctor` and `/doctor` show the
state under *Webex*: connected; *token rejected (401)* with that command as the fix; unreachable; or
starting. `relay doctor` tests the token itself with `GET /people/me`, not just that the Keychain item
exists. A missing token, a locked Keychain and a missing `webex.allowed_email` still stop relayd with exit
2 and the message that fixes them.

### Logs

Under launchd relayd logs to the rotating `~/Library/Logs/agent-relay/relayd.log`, with tracebacks.
`relayd.err.log` (launchd's stderr, never rotated) is truncated at relayd's start when it is over 10 MiB.

## Workspaces and settings

```bash
relay ws add scratch ~/code/agent-relay/.scratch
relay ws add myapp ~/code/myapp.code-workspace
relay ws list
relay ws rm scratch
relay config show
relay config set cdp.enabled true          # values are JSON, else a string
relay config set questions.deadline_s 300  # how long a question to the phone waits
relay config unset webex.bots              # remove a key the relay no longer knows
```

A workspace is a folder or a multi-root `.code-workspace` file. For a file, `vscode` chats go to the
window that has every one of its folders open, and `code <file>` opens it when none does. The other
harnesses, and the claim that links a phone chat to its hooks, use the file's first folder: that is the
working directory VS Code reports for a multi-root window.

A setting the config model rejects (an unknown key or a bad value) leaves `relay.toml` unchanged. Restart
relayd (`relay start`) after changing a setting.

Settings the November round removed (`webex.bots`, `webex.new_card_layout`, `approvals.*`, `presence.*`,
`renderer.flush_bytes`, `flush_idle_s`, `progress_edit_interval_s` and `quick_reply`) are still accepted in an
old `relay.toml`: relayd ignores them and logs one warning that names them, so it never fails to start over
one. `relay doctor` lists each with the command that removes it (`relay config unset <key>`).

### Settings added in the October and November rounds

| Setting | Default | What it does |
|---|---|---|
| `queue.default_mode` | `queue` | What a message sent mid-turn does: `queue` (next turn), `steer` (nudge the turn), `interrupt` (stop, then send). `/queue` changes it per session |
| `questions.deadline_s` | `600` | How long a question to the phone (`ask_phone`, an app's own question) waits for your answer. The only deadline left: there are no approvals |
| `renderer.html_threshold` | `auto` | When an answer goes out as `answer.html`: `auto` (over `max_message_bytes`, or a table, 3 or more code blocks, over 60 lines), `always` or `never` (long answers then arrive as text chunks) |
| `renderer.max_message_bytes` | `6144` | Largest text message; also the `auto` size threshold for HTML |
| `attachments.enabled` | `true` | Accept photos and files sent to a thread or `/new` |
| `attachments.max_bytes` | `20971520` | Largest file accepted, and the largest `send_phone_file` sends (20 MB) |
| `attachments.hold_s` | `600` | How long a files-only message waits for its text, and how long a file sent at the top level waits for your next `/new` |
| `attachments.inbox_dir` | `.agent-relay/inbox` | Where files land, relative to the workspace; must stay inside it |
| `ws.roots` | `[]` | Folders the phone may add workspaces under; `/ws` only lists while empty |
| `ws.clone_hosts` | `["github.com"]` | Hosts `/ws` may clone from |
| `ws.clone_timeout_s` | `300` | A clone that takes longer is killed and removed |
| `ws.list_depth` | `2` | How deep `/ws` looks under each root for folders (1 to 4) |
| `cursor_cli.idle_s` | `900` | An idle `cursor-agent` process is stopped after this long and respawned on the next prompt |
| `cdp.start_deadline_s` | `20` | A CDP turn that has not started by then is cleared |

Array values are JSON: `relay config set ws.roots '["~/code"]'`.

## The Webex token (one bot, one space)

The relay has one bot and one 1:1 space with you, whichever app a session runs in. Setup:

1. Create a bot at developer.webex.com (**Create a New App → Bot**) and set `webex.allowed_email`.
2. Press **Copy Token** on its page, then `pbpaste | relay setup token --stdin`. The token goes to the
   Keychain (service `agent-relay`, account `webex-bot-token`) and is never printed. The command refuses
   a terminal on stdin, empty or multi-word input and a missing `--stdin`. Overwrite the clipboard
   afterwards.
3. `relay start`, then `relay doctor`: the Webex line says connected.

To replace a rejected token, run step 2 again; a running relayd picks it up on its next retry (see
*relayd survives a Webex failure*). A brand-new bot logs `WDM device list failed: HTTP 404` once before
it registers its first device; that is harmless.

The per-app bots of the October round are gone (D116). Migration 5, on the first start of the new
version, clears the thread ids of sessions in the old app spaces, deletes their `:vscode`, `:cursor` and
`:opencode` registry keys, expires their open questions and **ends every existing session**: the next
event opens a fresh thread in the one space, and the old threads stay readable in the retired spaces.
The three retired bots' Keychain items (`webex-bot-vscode`, `webex-bot-cursor`, `webex-bot-opencode`)
were deleted on 2026-11-21; delete the bots themselves at developer.webex.com.

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
`cancel`. The bridge answers no permissions: with no approvals (D117) the relay's hooks allow every call
they answer.

A follow-up prompt goes only to the window the chat was opened in. If that window is closed, or was
reloaded (which gives it a new id), the prompt fails with "The VS Code window for this chat is closed".

Build and package:

```bash
cd extensions/relay-bridge && npm ci && npm run build && npm test
uv run python -c "from pathlib import Path; from agent_relay.ops.vsix import build_vsix; build_vsix(Path('build/relay-bridge.vsix'))"
```

Without the extension, a new phone chat falls back to `code chat -m agent -r "<prompt>"` in the
workspace; follow-ups do not. `relay doctor` reports which path is active.

## Cursor CLI (ACP)

Phone chats for Cursor run `cursor-agent --workspace <ws> [--model <m>] [--worktree] --force acp` and talk
ACP over stdio (`--force` is the CLI's own auto-approve; whether it silences `session/request_permission`
is not verified live, and the relay allows any request that still arrives, D125). The CLI must be logged
in once: `cursor-agent login`. A session survives a `relayd` restart or
a CLI crash: the next prompt respawns the CLI and calls `session/load`. At the desk, continue it with
`agent resume <id>` (the id is on the session card). An idle CLI process is stopped after
`cursor_cli.idle_s` (900 s) and respawned the same way.

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

The `/new` and `/model` cards never open Cursor's model menu on the desk. Cursor CDP offers only the list
saved by an explicit `/models cursor-ide`, which opens the menu once and stores the result (registry key
`models.cursor_cdp`); until then the card offers only "Default".

The DOM fixture tests need a Chromium binary: the newest Playwright Chromium in
`~/Library/Caches/ms-playwright`, Google Chrome, or the path in `RELAY_TEST_CHROMIUM`.

## relay-mcp: `notify_phone`, `ask_phone` and `send_phone_file`

`relay-mcp` is a stdio MCP server that agents call to reach the phone through `relayd`. It finds the
session from `RELAY_WORKSPACE` (or its working directory) and picks the deepest live session that
contains it, busy sessions first.

- `notify_phone(text)` posts one line and returns `{delivered, reason}`.
- `ask_phone(question, options, timeout_s)` blocks for up to 900 s and returns `{outcome, answer, reason}`.
  `outcome` is `answered`, `timeout` or `denied`. It sends a progress notification every 20 s while
  waiting.
- `send_phone_file(path, caption?)` posts a file to the session's thread and returns `{delivered, reason,
  error}`. The path must resolve inside the session's workspace (symlinks followed; see
  [security.md](security.md)) and the file must be at most `attachments.max_bytes`. Text is scrubbed first.
  For a report, an agent writes HTML and sends it with this tool.

All three refuse (`delivered: false` or `denied`) when the relay is paused, when no session is live for the
workspace, or when a desk session is not in away mode. If `relayd` is down, the tools tell the agent to
run `relay start`. `relay-mcp` logs only to `~/Library/Logs/agent-relay/`; stdout belongs to MCP.

## `/away`, `/back` and the plugin bundles

Away mode is manual and global: only `/away`, `/back` (from the phone, in a thread or at the top level),
`relay away on|off` and the skill's two commands switch it. Nothing turns it on when the screen locks or
the Mac sleeps. `skills/agent-relay-away/SKILL.md` holds the away-mode contract. Its `commands/away.md`
and `back.md` run `relay away on` and `relay away off` (an old copy that still passes `--workspace` keeps
working: the option is ignored).

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

The November round changed the patch text (away is manual; no approvals). A copy installed from the old
patch is no longer equal to the new patch nor to the base, so `relay install` and `relay doctor` report it
as drifted. To refresh it, put the original `stepping-away/SKILL.md` back (or delete the installed file),
then run `relay install --apply`.

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
without the token. Each turn's final result (the page keeps the text since the last tool call and shows it
when the turn completes) and questions arrive over one event stream, scrubbed like everything the relay
sends. There are no approval cards and no per-tool progress lines.

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
| Webex | the connection (connected; token rejected (401), with `pbpaste \| relay setup token --stdin` as the fix; unreachable; starting), and the token itself, tested with `GET /people/me` |
| Away mode | the one global flag, the `/panic` pause, open questions |
| Updates | the outcome of the last app-update self-test per app (kept in the log and the registry, never posted to the phone) |
| Legacy settings | any removed setting still in `relay.toml` (`webex.bots`, `approvals.*`, `presence.*`, …) with the `relay config unset <key>` that removes it |
| Versions | VS Code, Cursor, the Cursor CLI, OpenCode and the Copilot CLI; Cursor CLI more than 14 days older than the desktop app |
| Hooks | Copilot hooks, Cursor hooks and the OpenCode plugin are installed and match what `relay install` would write now |
| Config | `~/.agents/mcp.yaml` exists, and every harness file matches it |
| Skills | duplicate skill folders, the `agent-relay-away` link, the `stepping-away` patch |
| Keychain | the Webex bot token; the OpenCode password when that adapter is on; the PWA token when the PWA is on; a locked Keychain is reported as locked |
| launchd | each relay launch agent is loaded |

Without relayd, `relay doctor` reports the daemon as failing and still runs the local checks.
`/doctor` in Webex runs everything inside relayd.

## App updates

relayd checks VS Code's and Cursor's `Info.plist` every 10 minutes. When a version changes, it re-runs
that adapter's self-test (an AHP `initialize` handshake for VS Code; selector checks for Cursor CDP, only if
Cursor is already running with its debug port). The outcome goes to the log and is stored in the registry
(`updates.notice:<app>`); `relay doctor` and `/doctor` show it under *Updates*. Nothing is posted to the
phone.

- **Fail:** VS Code's agent host adapter is *paused* and Cursor CDP is *disabled*. New sessions, model
  lists and model changes are refused with the reason. `/stop` still works, and a prompt to a running
  session is queued as steering. The self-test is retried every check, and a pass clears the pause.
- **Inconclusive** (the app is not running): nothing is recorded, and the next check retries.
- **First sighting** of an app only records its version.

## The Cursor CDP self-test

```bash
relay config set cdp.enabled true
relay selftest cursor-cdp
```

It attaches to Cursor on the debug port (launching Cursor with it only if Cursor is closed), checks every
selector in `selectors.json` and records a pass for this Cursor version. The CDP adapter refuses to run
until it passes. With `cdp.enabled` off the command refuses and nothing is opened.

## The live end-to-end test

The driver acts as you (the owner) through your Webex person token and checks what the relay does from
the phone's side. Before a run: relayd up (`relay doctor` all ✅/ℹ️), the `scratch` workspace registered,
`relay install --apply` done, and `webex-cli` signed in (the driver reads its token; it never prints or
stores it, see [security.md](security.md)).

```bash
export RELAY_LIVE=1
.venv/bin/python scripts/phone_e2e.py --apps copilot,cursor,opencode,vscode \
    [--only 1,2,3] [--report /tmp/e2e.md] [--room-id <id>] [--turn-timeout 240] [--quiet 8] [--away-wait 180]
.venv/bin/python scripts/card_check.py [--only new_session,model_switch]   # every card posted once, expects HTTP 200, deleted
```

`--apps` takes any `/new` spelling (`copilot`, `cursor`, `cursor-ide`, `opencode`, `vscode`); desk-only
harnesses (`copilot-cli`, hook-only `cursor`) are refused. Without `RELAY_LIVE=1` the driver exits 2 and
sends nothing. Exit code 0: no FAIL; 1: a FAIL (or nothing ran); 2: bad arguments or setup.

Rules the driver enforces: live sessions only in `scratch`, at most 3 per adapter per run, the prompt
`Reply with exactly: OK` except three fixed prompts (D123), a room that is a 1:1 with a bot, and the
messages it posted are deleted at the end (relay threads and cards stay). Scenario order: 1; then 2 to 7
for each app; then 8 and 9. `--only 4` also runs 2 (it needs the session).

| # | Scenario | Passes when |
|---|---|---|
| 1 | `/help` | One reply listing `/new`; nothing else posted |
| 2 | `/new` card, submitted as a tap | The card is deleted, a thread root appears, exactly one final message `OK`, nothing in between and nothing at the top level |
| 3 | A reply in the thread | One final message |
| 4 | A PNG and a PDF in the thread | The agent names both files |
| 5 | A prompt that yields three tables | One message with `answer.html`; downloaded: CSP present, no `<script`, no `href=`, no external resource |
| 6 | `/model` card | The card is deleted and the thread says `Model → X from the next turn.`; the root shows X |
| 7 | `echo relay-e2e-$((6*7))` | No approval card, and the reply contains `relay-e2e-42` |
| 8 | `/away`, a desk chat, `/back` | The desk chat's final result opens a new thread, a reply continues it, `/back` releases it. Needs **you** to run one desk turn in `.scratch` within `--away-wait` seconds; no turn means SKIP. It always ends with `/back` |
| 9 | `/panic` | The confirm card is submitted, everything stops; it ends with `/away`, `/help`, `/back`, leaving away off |

If the person token cannot submit cards (`POST /attachment/actions` needs a scope; probed once), scenarios
2, 6 and 9 run their typed forms (rows `2b`, `6b`, `9b`) and the card row is `SKIP (no scope: submit the
card on the phone)`. Then open the `/new` and `/model` cards on your phone once and check the layout.

### Live results (November round)

Run on 2026-11-21 by `scripts/phone_e2e.py` as the owner (webex-cli person token), live sessions only in
`.scratch`, at most 3 per adapter. Several runs, because each failure was fixed and the app run again;
the table is the last result per cell.

| Check | Result |
|---|---|
| `scripts/card_check.py`: every card accepted by Webex (HTTP 200) | ✅ 14 of 14 |
| 1 `/help` | ✅ |
| 2 `/new` card, one final OK | ✅ Copilot (AHP), VS Code, Cursor CLI, OpenCode |
| 3 reply in a thread | ✅ Cursor CLI, OpenCode, VS Code. Copilot: passed only because the reply matched `OK` inside the classifier JSON (D129); the check now needs the message to start with `OK` |
| 4 image and PDF reach the agent | ✅ VS Code (first run), Cursor CLI (after D131), OpenCode. Copilot: ❌ open (see below) |
| 5 long table arrives as `answer.html` (CSP, no script, no href) | ✅ VS Code, Cursor CLI, OpenCode. Copilot: ❌ the answer was the classifier JSON (D129) |
| 6 `/model` card, next turn uses it | ✅ Copilot, Cursor CLI, OpenCode. VS Code: the model reached the session (D132), the root line was out of edits (see below) |
| 7 shell command, no approval card | ✅ Cursor CLI (`--force`), OpenCode (`always`). Copilot: no card and the tool ran (`🔧 1`), but the text was the classifier JSON (D129). VS Code: ❌ queued behind item 1 |
| 8 away: desk chat reaches the phone, reply continues it, `/back` releases it | not run: it needs the owner to run a desk turn |
| 9 `/panic` | ✅ everything stopped, `/away` resumed, away left off |
| Cursor CDP on Cursor 3.31.15 (selectors v4, D133) | ✅ self-test passes; `/new cursor-ide scratch "Reply with exactly: OK"` made a thread and answered `OK ⏱ 2s`; `/models cursor-ide` lists the picker's models. The first attempts failed on the v3 selectors (`messages`, `send` on a window with no chat, a reply row that was a tool row, a model menu that did not open). Not run: `/model` on a CDP chat, and a desk-chat adoption |
| Android look of the `/new` and `/model` cards (by the owner) | not run: owner |

Second VS Code and Copilot run (2026-11-21, after the VS Code reload; the owner asked to go past the
three-session cap for these two apps):

- **VS Code**: scenario 2 ✅. The answers of 3, 5 and 7 were right (`OK`, the table, `relay-e2e-42`):
  the empty answers of D129 are gone. The run still "failed" 3 to 7 because the driver counted the
  relay's own `📨 delivered to the parked session` and `📨 Queued (N waiting)` receipts as extra messages;
  the driver now ignores them, like the `📎` receipt (`scripts/e2e/cardkit.py::is_receipt`). Scenario 6's
  model *did* reach the session (`model` column `claude-haiku-4.5`, the later turns ran), so D132 works;
  what failed was the root line, which still read "default model": the root has a budget of 8 edits
  (`renderer.root_edit_budget`) and a long thread spends it. The driver now falls back to the thread's
  `/status` reply (`model: …`).
- **Copilot (AHP)**: scenario 2 ✅, 3 ✅ (a real `OK`, not the classifier JSON). Scenario 4 left the
  session `running` with an open question ("I received the attached image. What would you like me to do
  with it?"): the driver sent the PNG alone, and a files-only message to a **parked** chat goes at once as
  `Files from the phone:` plus the path (D122c), so the agent got no question to answer. Scenarios 5 to 7
  then only queued behind that open question. The driver now sends each file together with the question.
  **Scenarios 4 to 7 for Copilot are not re-run**: the cap of three live sessions per adapter was used up.

Open items for the owner:

1. **Re-run after the driver fixes**: `RELAY_LIVE=1 .venv/bin/python scripts/phone_e2e.py --only 2,3,4,5,6,7
   --apps copilot,vscode` (one new session each; both are over the cap, so this is the owner's call).
   Close the stuck Copilot session first with `/stop` in its thread.
2. After a restart of VS Code, a Copilot thread made before it cannot be replied to
   (`Session was never created on the backend`, shown as "That failed (AhpRpcError)").
3. Scenario 8 (away round trip) needs the owner at the desk: see `TODO.md`.
4. Cursor CDP and Android, as in the table. The AHP `autoApprove` flag is only indirectly confirmed
   (scenario 7 ran a tool with no card); a desk-session check of the three apps is still open.

### Live smoke 2026-10-04 06:55 UTC (October round, `scripts/smoke_live.py`, since deleted; it checked approval cards that no longer exist)

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
