# User guide

Drive the coding agents on your Mac from your phone, by chatting with a Webex bot.

- One chat with the bot; **one thread per session**.
- You get one message per turn (the result), questions when an agent needs you, and files both ways.
- No approvals: the relay never asks you to OK a tool. `/panic` is the brake.
- Sessions run in the real apps on the Mac, so you can pick them up at the desk.

Not set up yet? See [setup.md](setup.md). How it works inside: [architecture.md](architecture.md).

## What each app can do

| App | `/new` word | From the phone |
|---|---|---|
| VS Code, Copilot agent (Agent Host) | `copilot` | Start, get results, park and continue |
| VS Code, its own agent chat ("Local", via relay-bridge) | `vscode` | Start, get results, park and continue |
| Cursor CLI (`cursor-agent`) | `cursor` | Start, get results, wake an idle session |
| OpenCode | `opencode` | Start, get results, wake an idle session |
| Cursor desktop | (desk only; `cursor-ide` with [CDP](#optional-cursor-desktop-chats-from-the-phone-cdp)) | Desk chats come to the phone while away |
| Copilot CLI (`copilot` in a terminal) | (desk only) | Desk chats come to the phone while away |

## Commands at a glance

| Command | Where | Does |
|---|---|---|
| `/new [app] [ws] [model] ["prompt"]` | any | Start a session (bare: a card) |
| plain text | thread | A prompt to that session |
| `/stop` | thread | Cancel the current turn |
| `/status` | thread | What it is doing (🏝️ first while away) |
| `/diff` | thread | Changes in the workspace |
| `/model [name]` | thread | Change the model from the next turn (bare: a picker card) |
| `/queue [queue\|steer\|interrupt\|clear]` | thread | Mid-turn behaviour; bare: a card |
| `/steer <message>` | thread | Nudge the running turn once |
| `/sessions` | any | Card: pick a session, then Status, Diff, Stop or Back (works 15 min) |
| `/digest` | top level | Every session in one message; "Waiting for you" lists open questions |
| `/apps` | any | Apps and whether each is reachable |
| `/ws` | any | List workspaces; at the top level with `ws.roots` set, a menu to add one |
| `/models <app>` | any | Models an app offers |
| `/away [on\|off]`, `/back` | any | Away mode on / off (same everywhere); `/back` in a thread also says what it released |
| `/doctor` | any | Same health report as `relay doctor` |
| `/panic` | any | Confirm card (60 s), then stop everything; `/panic now` skips the card |
| `/help` | any | Command list |

Plain text at the top level gets a hint to use a thread or `/new`.

## Start a session

```
/new copilot myproject "Add a unit test for the parser"
```

- Parts: app, workspace name, optional model, prompt in quotes. No model means the app's default.
- The bot opens a thread, e.g. `🟦 Copilot · myproject · <title>`. **Everything for that session happens there.**
- Bare `/new` sends one card:

  | Field | Notes |
  |---|---|
  | Workspace | Last used is preselected |
  | Agent | One list of "app · model", each app's *Default* first; last used preselected. Apps that are down are left out, with one line saying so |
  | Prompt | Required |
  | Start | Card disappears; the thread's first line confirms. On failure the card stays with a `⚠️` reason |

- A partial command (`/new copilot myproject`) opens the same card with those parts filled in.

## Talking to a session

**One message per turn.**

- While the agent works the phone is silent: no streamed text, no tool lines.
- When the turn ends: the agent's final answer (said after its last tool call) plus a footer like
  `⏱ 3m · 🔧 14 · 📝 4 files (+120 −8)`.
- The thread's first line is edited quietly: ⏳ running, ✅ idle, 🅿️ parked, ▶️ active, ⚫ ended, plus 🏝️ while away.
- Other notifications: questions, a `⚠️` line when a turn fails, replies to your commands.

**Long answers arrive as `answer.html`** (open it in the phone's browser):

| Trigger | Result |
|---|---|
| Answer over ~6 KB, or with a table, ≥ 3 code blocks or > 60 lines | One message: short preview + `answer.html` |
| `/diff` over 200 lines | `changes.html` |
| `renderer.html_threshold` | `auto` (default), `always`, or `never` (text chunks instead) |

- The page is self-contained, light/dark aware, runs no script; links are plain text.

**Model:** a bare `/model` sends a picker (current model selected). On Apply the card disappears and the thread
says `Model → X from the next turn.` A typed `/model x` is checked against the app's list (id or name).

## Messages sent mid-turn: `/queue` and `/steer`

| Mode | A message sent while a turn runs |
|---|---|
| `queue` (default, `queue.default_mode`) | Waits; goes in as the next turn |
| `steer` | Nudges the running turn |
| `interrupt` | Stops the turn, then goes in as a new one |

- The status line shows the mode and how many messages wait.
- `/queue` card: the mode and each waiting message with Remove. `/queue clear` drops them.
- A session the phone cannot stop (e.g. a VS Code desk chat) cannot be interrupted; messages queue instead.

## Photos and files

**To the agent:**

| Where you send it | What happens |
|---|---|
| In a session's thread, with text | Goes to the agent with the text |
| In a thread, on its own | Waits up to 10 min for the text it belongs to |
| In a *parked* desk session's thread | Reaches the chat at once, as its path in the text (desk apps cannot take files) |
| With a `/new` message | Goes with that session |
| At the top level, on its own | **Held 10 min** (`attachments.hold_s`) for your next `/new` (card or typed). Bot says "📎 Got N file(s)…"; the card shows "📎 N files will be attached". Kept if the start fails |

- Saved under `<workspace>/.agent-relay/inbox/` (kept out of `git status`); deleted a day after the session ends.
- Copilot, OpenCode and Cursor CLI get the file itself; the other apps get its path in the prompt.
- Too large (> 20 MB), unsupported or failed download: one `⚠️ Skipped …` line names it; the text still goes.
- Settings: [operations.md](operations.md).

**From the agent:** `send_phone_file` posts a file from the session's workspace to its thread (for a report:
the agent writes HTML and sends it). Only sessions that reach the phone; only files inside their workspace.

## No approval cards

- While a session belongs to the phone, or you are away, every tool call the relay handles is **allowed at once**.
- Phone sessions also start with each app's own auto-approve flag where one exists.
- A desk session with away mode off keeps the app's own prompt at the desk.
- After `/panic` the relay answers nothing; desk apps ask you themselves.
- What an agent may do while you are away is bounded by the `agent-relay-away` skill's "never do while away" list, not by the relay.

## When a turn ends

| Session | Your reply in the thread |
|---|---|
| Started from the phone | Always works: starts the next turn |
| Desk chat in VS Code (`vscode`, `copilot`), Copilot CLI, Cursor desktop, away on | Turn is **parked** (🅿️, held open); your reply continues the same turn. Nothing extra is posted for parking |
| OpenCode, Cursor CLI | Never park; they go idle and your reply wakes them |
| Ended (⚫) | Revives it and starts a turn; if the app cannot resume it, the reply says so: use `/new` |

- Parking is what lets you answer a chat started **at the desk**, which the relay cannot start turns for.
- A parked turn is released by `/back` in the thread, or by itself after 4 h (`park.deadline_s`).

## Cards

| Card | Sent by | Offers | After use |
|---|---|---|---|
| New session | `/new` | Workspace, Agent (app · model), Prompt, Start | Disappears |
| Question | `ask_phone` or an app's own question | Its options, free text, or Skip | Disappears |
| Model | `/model` in a thread | The app's models, current one selected | Disappears |
| Cancel everything? | `/panic` | Confirm or cancel, within 60 s | Disappears |
| Queue | `/queue` | Mode; each waiting message with Remove | Stays |
| Sessions | `/sessions` | Status, Diff, Stop or Back for a picked session | Stays |
| Workspaces | `/ws` (with `ws.roots`) | Add a folder, clone a repo, add a repo to a workspace | |

- A failed tap leaves the card so you can tap again.
- The text line above each card is what the phone notification shows.
- Links in agent text arrive as plain code, never tappable.

## Away mode

`/away` when you leave: desk chats come to the phone and a reply continues them. `/back` when you return.

- One switch for the whole Mac, and **only you flip it**. Screen lock or sleep never changes it.
- Switch it from anywhere:

  | Where | How |
  |---|---|
  | Phone (thread or top level) | `/away`, `/back` |
  | Any agent chat at the desk | `/away`, `/back` (the `agent-relay-away` skill runs `relay away on\|off`) |
  | Terminal | `uv run relay away on` / `off` |

- While on, `/status` and every thread's first line show 🏝️.
- Phone-started sessions behave the same with away on or off.
- `/back` releases parked desk sessions and hands VS Code and Cursor desktop chats started from the phone back to the desk.
- While away, agents can reach you via `relay-mcp`:

  | Tool | Does |
  |---|---|
  | `notify_phone` | Posts a line in the session's thread |
  | `ask_phone` | Asks a question, waits up to 15 min |
  | `send_phone_file` | Sends a file from the workspace |

  The skill tells agents to decide for themselves rather than block on small questions, and to follow its "never do while away" list.

## `/panic`

- Asks first (card, 60 s); `/panic now` skips it.
- Then: cancels every running turn, closes open questions, releases parked turns, turns away off, **pauses the relay**.
- While paused: away counts as off, desk sessions stay at the desk, no tool call is answered, and `notify_phone`, `ask_phone`, `send_phone_file` are refused.
- Send `/away` to resume.

## Adding workspaces from the phone: `/ws`

By default the phone only uses workspaces added with `relay ws add`. To allow more (then `relay start`):

```bash
uv run relay config set ws.roots '["~/code"]'
uv run relay config set ws.clone_hosts '["github.com"]'     # the default
```

Top-level `/ws` then sends a menu:

| Option | Does |
|---|---|
| Add a folder | Pick a folder under a root (git repos first), give it a name |
| Clone a repo | `https://<host>/<owner>/<repo>` or `git@<host>:<owner>/<repo>`, host in `ws.clone_hosts`; cloned under a root and registered. Prefer `https://`: under launchd your SSH keys may not be visible |
| Add a repo to a workspace | Makes a plain workspace a `<root>/<name>.code-workspace` with both folders |

- Names: letters, digits, `.`, `_`, `-`; up to 40 characters.
- The card can hand you straight to `/new` in the new workspace.

## Picking work up at the desk

| App | Where the phone session is |
|---|---|
| VS Code (`vscode`) | A new chat in the window for that workspace |
| VS Code Copilot (`copilot`) | The Chat view's session list, a few seconds after `/new`. A `.code-workspace` workspace: only in the window opened from that file. A plain folder: any window containing it. Type into it once the agent's turn is done |
| Cursor CLI | `agent resume <id>` in a terminal (id on the session card) |
| OpenCode | `opencode` in the folder, then the sessions picker |
| Cursor desktop | Desk chats carry on where you left them; `/back` turns away off |

- Avoid typing at the desk while the phone drives the same session: the two interleave.
- Copilot sessions started before D138 ran in a hidden second chat and do not appear: read them with `agent-relay-recall`.

### Finding an earlier chat: `agent-relay-recall`

The relay does not resume desk chats from the phone. Start a new session and ask it ("what did we decide in the
chat about the parser?"). The skill (installed to `~/.agents/skills`) tells the agent where transcripts are;
it reads them without changing anything.

| App | Transcripts |
|---|---|
| VS Code (Copilot chat) | `chatSessions/*.jsonl` in each workspace's storage folder (matched by `workspace.json`); no-folder chats in `emptyWindowChatSessions` |
| Copilot CLI | `session-state/<id>/events.jsonl`, folder in `workspace.yaml` |
| Cursor | `~/.cursor/projects/<slug>/agent-transcripts`, and the very large `state.vscdb` |
| OpenCode | `~/.local/share/opencode/opencode.db` |

## Optional: Cursor desktop chats from the phone (CDP)

By default `/new cursor` uses the Cursor CLI. An experimental adapter types into the Cursor desktop chat over
Chrome's debugging protocol. Off by default; breaks when Cursor's UI changes.

1. `uv sync --frozen --extra cdp` and `uv run relay config set cdp.enabled true`.
2. Quit Cursor. The relay restarts it with a debug port on `127.0.0.1` only.
3. Open Cursor's chat pane; run `uv run relay selftest cursor-cdp`. Every check must pass.
4. On the phone: `/new cursor-ide <workspace> "prompt"`.

- After every Cursor update, rerun the self-test; until it passes the adapter refuses to run.
- Details: [operations.md](operations.md).

## Optional: the browser front end (PWA)

- Same commands as the bot; off by default; `127.0.0.1` only; reach it from the phone over Tailscale.
- Does not replace Webex. Setup: [operations.md](operations.md).

## When something is wrong

Start with `uv run relay doctor` (desk) or `/doctor` (phone). Every non-✅ line names its fix.

| Symptom | Fix |
|---|---|
| The bot does not answer at all | Mac asleep/offline or relayd down. `relay status`; if it fails, `relay start`. `relay doctor` shows the Webex state |
| Doctor: "Webex token rejected (401)" | Token dead or regenerated. Copy the new one, `pbpaste \| uv run relay setup token --stdin`. No restart: picked up within ~5 min |
| Doctor lists `webex.bots`, `approvals.*` … | Removed in the November round, ignored. Run the `relay config unset <key>` it prints |
| Bot answers "paused" | `/panic` was used. Send `/away` |
| `relay start`: launchd still lists relayd | A stop is finishing. Wait a few seconds, retry |
| A desk session never reaches the phone | Away is off, or the app was opened before `relay install --apply`. `/away`; reload VS Code / restart Cursor |
| `/new`: app unavailable or missing from Agent | Open the app (VS Code must run for both VS Code apps), check `/apps`. Cursor CLI: `cursor-agent login` |
| `/new` shows only "Default" for Cursor CDP | Run `/models cursor-ide` once; it saves the list |
| A top-level file "never arrived" | It waits 10 min for `/new`, then is left behind. Resend with `/new` or in the thread |
| A file got `⚠️ Skipped …` | The line says why (size, type, download). Resend smaller or in another format |
| Cursor CLI reply ends "RetriableError: WritableIterable is closed" | Known `cursor-agent` issue; the reply is complete |
| `vscode` follow-up: "window is closed" | Follow-ups go only to the original window; a reload counts as closing. Start a new chat |

- Logs: `~/Library/Logs/agent-relay/` (`relayd.log`). Secrets are scrubbed, so logs are safe to share.
- Session database: `~/Library/Application Support/agent-relay/relay.db`.

## Security in short

- Only your Webex account (`webex.allowed_email`, pinned on first contact) can drive it; others are ignored and logged.
- Nothing listens on the network. OpenCode server, PWA and Cursor debug port bind `127.0.0.1` only.
- Secrets live only in the Keychain and are scrubbed from logs and every Webex message.
- **No approvals:** whoever controls your Webex account can run anything on the Mac. What remains: the pinned
  account, 1:1 chats only, `/panic`, the logs. A desk session with away off keeps the app's prompts.
- `answer.html` runs no script and loads nothing remote.
- Treat the bot token like a password. If it leaks: regenerate at developer.webex.com, store with `pbpaste | uv run relay setup token --stdin`.
- Full threat model: [security.md](security.md).
