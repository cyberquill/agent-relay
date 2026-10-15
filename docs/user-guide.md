# User guide

agent-relay lets you drive the coding agents on your Mac from your phone. You chat with a Webex bot. Each
agent session gets its own thread in that chat: you see the agent's replies, answer its approval cards and
send it new prompts. The agents keep running on the Mac, in the same apps you use at the desk, so you can
pick a session up again at the desk.

It works with these apps:

| App | What the phone can do | Word to use in `/new` |
|---|---|---|
| VS Code, Copilot agent (Agent Host) | Start sessions, stream replies, approve tools, park and wake | `copilot` |
| VS Code, its own agent chat (VS Code calls it "Local"; through the relay-bridge extension) | Start sessions, stream replies, approve tools, park and wake | `vscode` |
| Cursor CLI (`cursor-agent`) | Start sessions, stream replies, approve tools, wake an idle session | `cursor` |
| OpenCode | Start sessions, stream replies, approve tools, wake an idle session | `opencode` |
| Cursor desktop app | Sessions you start at the desk are handed to the phone while away mode is on | (started at the desk) |
| GitHub Copilot CLI (`copilot` in a terminal) | Sessions you start at the desk are handed to the phone while away mode is on | (started at the desk) |

This guide covers what to set up once, how to use the system day to day, and what to do when something
looks wrong. For every command and setting in detail, see [operations.md](operations.md). For the threat
model, see [security.md](security.md).

## How it fits together

- **relayd** is a small background service on the Mac, run by launchd. It holds the Webex connection,
  talks to each app and keeps track of the sessions in a local database.
- **Hooks and plugins** inside each app ask relayd before a risky tool runs. When the session belongs to
  the phone, or you are away, relayd sends you an approval card and waits for your answer.
- **The Webex bot** is how the phone reaches relayd. relayd answers only your own Webex account.
- **Away mode** says "I am not at the desk". With away mode on, sessions you started at the desk also
  send their approvals to the phone, and a finished turn is *parked* (held open) so you can reply to it
  from the phone.

Nothing listens on the network: relayd connects out to Webex, and every app talks to relayd over a socket
on the Mac.

## Before you start: what you need

- A Mac with macOS, which stays powered and online while you are away (see
  [Keeping the Mac reachable](#keeping-the-mac-reachable)).
- An Android or iPhone with the Webex app, signed in to your account.
- A Webex bot of your own, created at [developer.webex.com](https://developer.webex.com/my-apps)
  (**Create a New App → Create a Bot**). Keep the bot's access token: you need it once, during setup.
  Optionally, one more bot per app gives each app its own space on the phone; see
  [One bot per app](#optional-one-bot-per-app).
- The apps you want to drive, installed and signed in at the desk first: VS Code with GitHub Copilot,
  Cursor (desktop app and/or `cursor-agent`), and OpenCode.

## Preparing the Mac (once)

The `relay` command runs from the checkout as `uv run relay …`; the rest of this guide sometimes
shortens that to `relay …`. Settings changed with `relay config set` apply after `relay start`.

### 1. Developer tools

```bash
xcode-select --install                               # git and the compilers, if not already there
curl -LsSf https://astral.sh/uv/install.sh | sh      # uv; it fetches Python 3.13 for the relay itself
brew install node                                    # Node.js and npm, to build the VS Code extension
```

### 2. The apps

Set up only the apps you plan to use. relayd keeps working when an app is missing, and `relay doctor`
tells you which ones it found. To silence one you never use, for example
`uv run relay config set adapters.opencode false`.

- **VS Code**: install it, sign in to GitHub Copilot, and put the `code` command on the PATH (Command
  Palette → *Shell Command: Install 'code' command in PATH*). The Copilot agent sessions run in VS Code's
  Agent Host, which only exists while VS Code is open, so leave VS Code running when you step away.
- **Cursor CLI**: `curl https://cursor.com/install -fsS | bash`, then `cursor-agent login` once.
- **Cursor desktop**: nothing extra. relay install adds its hooks; restart Cursor once afterwards.
- **OpenCode**: `curl -fsSL https://opencode.ai/install | bash` (it installs to `~/.opencode/bin`), then
  sign in to a model provider with `opencode auth login`.

### 3. Get the relay and build it

```bash
git clone <this repository> ~/Desktop/Repository/Personal/agent-relay
cd ~/Desktop/Repository/Personal/agent-relay
uv sync --frozen --all-extras
(cd extensions/relay-bridge && npm ci && npm run build)
```

### 4. Put the secrets in the Keychain

The relay reads secrets only from the login Keychain, never from files. Each command prompts for the value,
so it never lands in your shell history.

```bash
# The Webex bot's access token.
security add-generic-password -U -s agent-relay -a webex-bot-token -w
# A password for the local OpenCode server (only if you use OpenCode). Any long random value works,
# for example the output of `openssl rand -base64 32`.
security add-generic-password -U -s agent-relay -a opencode-password -w
```

### 5. Tell the relay who you are and where you work

```bash
uv run relay config set webex.allowed_email you@example.com     # only this account is answered
uv run relay ws add scratch "$PWD/.scratch"                    # a safe folder to try things in
uv run relay ws add myproject ~/code/myproject                 # each folder the phone may start sessions in
```

A workspace is a folder the phone is allowed to start sessions in, under a short name you type on the
phone. `relay ws list` shows them and `relay ws rm <name>` removes one.

### 6. Install, start and check

```bash
uv run relay install            # a dry run: lists every file it would change, changes nothing
uv run relay install --apply    # hooks, plugin, MCP sync, skills, launch agents, VS Code extension
uv run relay start
uv run relay doctor             # every line should be ✅ or ℹ️
```

`relay install --apply` backs up every file before touching it, and `relay uninstall --apply` puts them
all back. After the install, reload your VS Code windows and restart Cursor so they pick up the hooks and
the relay-bridge extension. relayd then starts by itself at every login.

### 7. Say hello from the phone

In the Webex app, start a direct chat with your bot (its address ends in `@webex.bot`) and send `/help`.
The first message from your `allowed_email` pins your Webex account; from then on relayd ignores everyone
else. When relayd starts it posts **🖥️ Mac online** in this chat, so you can tell the Mac is reachable.

### Keeping the Mac reachable

relayd can only work while the Mac is awake and online.

- **Sleep**: a sleeping Mac answers nothing. On a laptop, plug it in and turn on *System Settings →
  Battery → Options → Prevent automatic sleeping on power adapter when the display is off* (on a desktop:
  *Energy → Prevent automatic sleeping when the display is off*). A closed lid puts a laptop to sleep
  unless an external display, keyboard and power are connected.
- **Waking up**: after sleep, relayd reconnects to Webex by itself and catches up on messages you sent
  in the meantime.
- **Screen lock**: locking the screen does not stop anything. VS Code Copilot, VS Code, OpenCode
  and the Cursor CLI all ran a full session from the phone with the screen locked (see
  [spikes.md](spikes.md#s7--does-anything-stop-working-with-the-screen-locked)). Locking it also turns
  away mode on (see [Away mode follows the screen lock](#away-mode-follows-the-screen-lock)). Logging
  out of macOS is different: it stops your apps and relayd with them. A brief network drop may happen
  as the screen locks; relayd reconnects within seconds.
- **Network**: the Mac needs to reach Webex (`webexapis.com` and `*.wbx2.com`). On VPN, check that Webex
  works in a browser.

## Using it from the phone

### Start a session

```
/new copilot myproject "Add a unit test for the parser"
```

The parts are the app (`copilot`, `vscode`, `cursor`, `opencode`), the workspace name, an optional model
and the prompt in quotes. With a prompt but no model, the app's default model is used. The bot answers
with a session card, and **everything about that session happens in the card's thread**.

`/new` on its own sends one card: pick the workspace, type the prompt, optionally turn away mode on for
that workspace, then open the app you want and pick its model. If your phone handles that card badly
(a button inside an opened section loses what you typed), set `uv run relay config set
webex.new_card_layout two`: the card then splits into app, workspace and prompt first, and the model
second.

### Talk to a session

Reply in the session's thread:

- Plain text is a prompt. What happens while the agent is still working depends on the session's
  queue mode (below).
- `/stop` cancels the current turn. `/status` shows what it is doing, `/diff` shows the changes it made
  in the workspace, and `/model [name]` changes the model for the next turn (a bare `/model` sends a
  picker).

Replies stream into the thread in chunks. The turn's tool calls are collected in one progress message
that is edited as they happen (at most every 30 s, `renderer.progress_edit_interval_s`). When the turn
ends, a **✅ Turn complete** card shows how long it took, how many tool calls it made and which files
changed, with **Diff**, **Continue** and, for sessions that park, **Back**.

### Messages sent while a turn runs: `/queue` and `/steer`

Each session has a queue mode, shown on its status line with the number of waiting messages:

| Mode | A message sent mid-turn |
|---|---|
| `queue` (default) | waits, and goes in as the next turn when this one ends |
| `steer` | nudges the running turn |
| `interrupt` | stops the turn, then goes in as a new one |

- `/queue` shows a card with the mode and the waiting messages; each can be removed.
- `/queue queue`, `/queue steer` or `/queue interrupt` sets the mode for this session; `/queue clear`
  drops what is waiting.
- `/steer <message>` steers the running turn once, whatever the mode.

The default for new sessions is `queue.default_mode`. A session that cannot be stopped from the phone
(a desk chat in VS Code, for example) cannot be interrupted either; setting `interrupt` there says so,
and messages queue instead.

### Send photos and files

Attach a photo or file to a message in a session's thread, or to a `/new` message, and it goes to the
agent with the text. It is saved in the workspace under `.agent-relay/inbox/` (kept out of `git status`).
The Copilot agent, OpenCode and the Cursor CLI receive the file itself; the other apps get its path in
the prompt. A file sent on its own waits up to 10 minutes for the text it belongs to. Files over 20 MB
are skipped, and a session's files are deleted a day after it ends. Files sent outside a thread are
refused with a pointer to a thread or `/new`. Settings: [operations.md](operations.md#settings-added-in-the-october-round).

### Approve tools

Before a tool that changes something runs (a shell command, a file edit, an MCP call), the thread gets an
**Approval needed** card with what the agent wants to run (the start and end of a long command) and when
it auto-denies. The choices:

| Button | Effect |
|---|---|
| Allow once | Runs this call |
| Allow for this session | The same tool is not asked again in this session |
| Allow this tool in this workspace for 4 h | The same tool is not asked again in this workspace, by any session, for 4 hours (`approvals.workspace_grant_s`) |
| Deny | Refuses the call |
| Deny with reason… | Refuses it and tells the agent why |

Read-only tools such as reading files and searching are allowed without asking (`approvals.auto_allow`).
Each card works once: a second tap, or a tap from another device, says the card was already answered.

An unanswered card is **denied after 10 minutes** (`approvals.deadline_s`), and the agent is told so. If
you come back to the desk and turn away mode off, cards from desk sessions move back to the desk prompt
(🖥️ on the card).

### When a turn ends

For a session started from the phone, a reply in its thread always works: when the turn has ended, the
relay starts a new one with your message.

With away mode on, VS Code (both `vscode` and `copilot`) and the Cursor desktop app also *park* the finished turn:
the thread shows **🅿️ Parked** and the agent's turn is held open, so your reply continues the same turn
instead of starting a new one. This is what lets you answer a session you started **at the desk**,
which the relay cannot start turns for. `/back` in the thread releases a parked turn without a reply,
and a parked turn is released by itself after 4 hours (`park.deadline_s`). OpenCode and the Cursor CLI
never park; they go idle and your reply wakes them.

A reply in the thread of a session that has **ended** brings it back: the relay revives the session and
starts a turn with your message. If the app cannot resume it, the reply says so and the session stays
ended; start a new one with `/new`.

### Cards on the phone

Most choices come as cards with buttons, so you rarely type ids:

| Card | Sent by | What it offers |
|---|---|---|
| New session | `/new` | Workspace, prompt, away toggle, then the app and its model |
| Approval needed | an agent's tool call | The five choices above, and when it auto-denies |
| Question | an agent's `ask_phone` | Its options, a free-text answer, or Skip |
| Turn complete | the end of a turn | Diff, Continue, Back |
| Queue | `/queue` | The queue mode, and each waiting message with Remove |
| Model | `/model` in a thread | The app's models |
| Sessions | `/sessions` | Status, Diff, Stop, Back or Away for the session you pick |
| Away mode | `/away` at the top level | All workspaces or one, then On or Off |
| Cancel everything? | `/panic` | Confirm or cancel, within 60 s |
| Workspaces | `/ws` (with `ws.roots`) | Add a folder, clone a repo, add a repo to a workspace |

The line of text above each card is what the phone's notification shows, so it names what needs you.
Links in an agent's text arrive as plain code, never as tappable links.

### Other commands

| Command | What it does |
|---|---|
| `/sessions` | A card: pick a session, then Status, Diff, Stop, Back or Away. The card keeps working for 15 minutes |
| `/digest` | Every session in one message, grouped by what needs you |
| `/apps` | Lists the apps and whether each one is reachable |
| `/ws` | Lists the workspaces; with `ws.roots` set, a menu to add one (see below) |
| `/models <app>` | Lists the models an app offers |
| `/away [on\|off]` | Turns away mode on or off (see below). A bare `/away` sends a card at the top level (all workspaces or one, then On or Off) and means on in a thread |
| `/back` | In a thread: releases that parked session, and hands a VS Code or Cursor desktop chat started from the phone to the desk |
| `/queue`, `/steer` | In a thread: see [Messages sent while a turn runs](#messages-sent-while-a-turn-runs-queue-and-steer) |
| `/doctor` | The same health report as `relay doctor` |
| `/panic` | Asks first with a card (good for 60 s), then cancels every running turn, denies every open card, releases parked turns, turns away mode off and pauses the relay. `/panic now` skips the question |
| `/help` | The command list |

While paused, away mode counts as off everywhere, so desk sessions stay at the desk, and agents cannot
message the phone through `notify_phone` or `ask_phone`. Send `/away on` to resume.

### Add workspaces from the phone: `/ws`

By default the phone can only use workspaces added at the desk with `relay ws add`. To let it add
more, name the folders it may work under (a change needs `relay start`):

```bash
uv run relay config set ws.roots '["~/code"]'
uv run relay config set ws.clone_hosts '["github.com"]'     # the default
```

Then a top-level `/ws` sends a menu card:

- **Add a folder**: pick a folder under a root (git repositories first) and give it a name.
- **Clone a repo**: paste `https://<host>/<owner>/<repo>` or `git@<host>:<owner>/<repo>`; only hosts in
  `ws.clone_hosts` are accepted. The repo is cloned under a root and registered as a workspace. Under
  launchd your SSH keys may not be visible, so an `https://` URL is the safer choice.
- **Add a repo to a workspace**: puts a second folder into a workspace. A plain workspace becomes a
  `<root>/<name>.code-workspace` file holding both folders.

Names are letters, digits, `.`, `_` and `-`, up to 40 characters. The card can then hand you straight to
`/new` in the new workspace. In a thread, `/ws` only lists.

## Away mode: handing desk sessions to the phone

Sessions started from the phone always report to the phone. Sessions you started **at the desk** stay at
the desk until you turn away mode on for their workspace. Then each one gets its own thread on the phone,
its approvals go to the phone instead of the desk prompt, and its finished turns park.

A chat you started from the phone in VS Code (or Cursor over CDP) also opens on the desktop. Turning away
mode off, or `/back` in its thread, hands it to the desk: its open approvals move to the desk prompt, and
from then on it behaves like a desk session. OpenCode and Cursor CLI sessions have no desk window, so they
stay with the phone.

Turn it on just before you leave, in any of these ways:

- **In the agent chat at the desk**: type `/away` (and `/back` when you return). relay install puts the
  `agent-relay-away` skill in `~/.agents/skills`, which Copilot, Cursor and OpenCode all read; it switches
  away mode for the chat's workspace.
- **In a terminal**: `uv run relay away on --workspace "$PWD"` (or `off`). Without `--workspace`, it
  covers every workspace.
- **From the phone**: `/away on` inside a session's thread covers that session's workspace. `/away on`
  in the main chat covers every workspace.

Turning away mode off releases the parked desk sessions it covers and hands their open approvals back to
the desk prompt, so nothing waits for a phone you are no longer watching. `relay doctor` and `/doctor`
show which workspaces are away.

### Away mode follows the screen lock

With `presence.auto_away_on_lock` on (the default), locking the Mac turns away mode on for every
workspace, and unlocking turns it off again. The relay checks the lock state every 10 seconds
(`presence.poll_s`). It only undoes what it did: if you turned away mode on yourself, unlocking leaves it
on, and if you change it by hand while locked, the unlock leaves your choice alone. A lock while the relay
is paused by `/panic` does nothing. When a session is live, the phone gets **🔒 Mac locked: away on** and
**🔓 Mac unlocked: away off**. To turn it off: `uv run relay config set presence.auto_away_on_lock false`.

While away mode is on, agents can also reach you themselves: relay install registers the `relay-mcp`
server, whose `notify_phone` tool posts a line in the session's thread and whose `ask_phone` tool asks a
question and waits up to 15 minutes for your reply. The `/away` command tells the agent to use them,
and to decide for itself rather than block on small questions.

## Picking work up at the desk

- **VS Code**: phone sessions are ordinary VS Code chats. `vscode` sessions open as a new chat in
  the window for that workspace; VS Code Copilot sessions run in VS Code's Agent Host, so look for them
  in VS Code's list of chat sessions.
- **Cursor CLI**: the session card shows the session id. Continue in a terminal with
  `agent resume <id>`.
- **OpenCode**: the session is in OpenCode's session list for that workspace (`opencode` in the folder,
  then the sessions picker).
- **Cursor desktop**: desk sessions simply carry on where you left them. Turn away mode off with `/back`.

Avoid typing into a session at the desk while the phone is driving it: the two will interleave.

### Finding an earlier chat: the `agent-relay-recall` skill

The relay does not resume chats you started at the desk from the phone. Instead, start a new session and
ask it to find the old one ("what did we decide in the chat about the parser?"). relay install puts the
`agent-relay-recall` skill in `~/.agents/skills`; it tells the agent where each app keeps its
transcripts, and the agent reads them without changing anything:

| App | Where transcripts live |
|---|---|
| VS Code (Copilot chat) | `chatSessions/*.jsonl` in each workspace's storage folder, matched to a folder by its `workspace.json`; chats with no folder in `emptyWindowChatSessions` |
| Copilot CLI | `session-state/<id>/events.jsonl`, with the folder in `workspace.yaml` |
| Cursor | `~/.cursor/projects/<slug>/agent-transcripts`, and the very large `state.vscdb` |
| OpenCode | `~/.local/share/opencode/opencode.db` |

### Optional: start Cursor desktop chats from the phone

By default the phone starts Cursor work through the Cursor CLI. An experimental adapter can instead type
phone prompts into the Cursor desktop chat over Chrome's debugging protocol (CDP). It is off by default
and breaks when Cursor's UI changes. To try it:

1. `uv sync --frozen --extra cdp` and `uv run relay config set cdp.enabled true`.
2. Quit Cursor. The relay restarts it with a debug port that listens only on `127.0.0.1`.
3. Open Cursor's chat pane and run `uv run relay selftest cursor-cdp`; every check must pass.
4. On the phone, use `/new cursor-ide <workspace> "prompt"`.

After every Cursor update, run the self-test again; until it passes, the adapter refuses to run.

### Optional: one bot per app

With one bot, every session's thread is in one chat. With a bot per app, VS Code, Cursor and OpenCode
each get their own 1:1 space, so their notifications and threads stay apart. The first bot becomes the
**control** bot. Apps without their own bot stay in the control space.

| Bot | Serves |
|---|---|
| `vscode` | VS Code, VS Code Copilot, Copilot CLI |
| `cursor` | Cursor desktop, Cursor CLI, Cursor over CDP |
| `opencode` | OpenCode |
| `control` | Everything else, and every app without its own bot |

1. At [developer.webex.com](https://developer.webex.com/my-apps), create one bot per app you want apart
   (**Create a New App → Create a Bot**), for example "Relay · VS Code".
2. On each bot's page, press **Copy Token**, then at once, in the checkout:

   ```bash
   pbpaste | uv run relay setup bot vscode --stdin
   uv run relay config set webex.bots.vscode webex-bot-vscode
   ```

   The token goes from the clipboard into the Keychain and is never shown. Copy something else
   afterwards so the token does not stay on the clipboard.
3. Repeat for `cursor` and `opencode`, then `uv run relay start`.
4. Each new bot sends you a one-time hello, which creates its space. `uv run relay doctor` shows one
   Keychain line and one websocket line per bot.

How the spaces behave:

- A session's thread stays in the space that created it. Threads from before you added bots stay in the
  control space.
- `/panic`, `/away` and `/back` work from every space.
- In an app's space, `/new`, `/sessions`, `/digest`, `/apps` and the `/new` card show only that app.
  `/new` for another app there is refused: start it in that app's space.
- `/new` in the control space for an app with its own bot opens the thread in the app's space and says
  **🧵 Started in the <App> space.**

Tip: in Webex desktop, make a space section called "Agent Relay" and drag the bot spaces into it; the
section syncs to the phone.

### Optional: a browser front end

The relay also has a small web app (the PWA) with the same commands as the Webex bot. It is off by
default, listens on `127.0.0.1` only, and is meant to be reached from the phone over Tailscale, if you
use it. It does not replace Webex. Setup is in
[operations.md → The PWA](operations.md#the-pwa-off-by-default).

## When something is wrong

Start with `uv run relay doctor` at the desk, or `/doctor` from the phone. Every line that is not ✅
names the fix.

| Symptom | What to do |
|---|---|
| The bot does not answer at all | The Mac is asleep or offline, or relayd is down. At the desk: `relay status`; if it fails, `relay start`. The **🖥️ Mac online** post confirms it is back. |
| The bot answers "paused" | `/panic` was used. Send `/away on`. |
| `relay start` says launchd still lists relayd | A stop is still finishing. Wait a few seconds and run it again. |
| A desk session never shows up on the phone | Away mode is off for its workspace (`relay doctor` shows the state), or the app was opened before `relay install --apply`. Reload VS Code or restart Cursor. |
| `/new` says an app is unavailable | Open the app (VS Code must be running for both VS Code apps) and check `/apps`. For the Cursor CLI, run `cursor-agent login`. |
| An approval card says "That request was already answered" | It was answered at the desk, or its 10 minutes ran out and it was denied. |
| A Cursor CLI reply ends with "RetriableError: WritableIterable is closed" | A known `cursor-agent` issue; the reply itself is complete. |
| relayd stops at start with `pbpaste \| relay setup bot <name> --stdin` | An app bot is in `webex.bots` but its token is not in the Keychain. Copy the token and run that command. |
| `/new` says an app "has its own space" | You are in another app's space. Start it in that app's space, or from the control space. |
| A follow-up to a `vscode` chat says its window is closed | Follow-ups go only to the window the chat was opened in, and a window reload counts as closing it. Start a new chat with `/new`. |

Logs are in `~/Library/Logs/agent-relay/` (`relayd.log` for the daemon). Tokens and passwords are
scrubbed before anything is written there, so the logs are safe to share. The session database is
`~/Library/Application Support/agent-relay/relay.db`.

To restart cleanly: `uv run relay stop`, then `uv run relay start`. After pulling a new version of the
relay, run `uv sync --frozen --all-extras`, rebuild relay-bridge, then `relay install --apply` and
`relay start`.

## Stopping and uninstalling

- **Pause for a while**: `uv run relay stop`. relayd stays stopped until `relay start` or the next login.
  Hooks with relayd down fall back to the desk, so the apps work as if the relay were not there.
- **Remove it**: `uv run relay uninstall` shows what it would restore, and `uv run relay uninstall
  --apply` restores every file `relay install` changed, byte for byte, and removes the launch agents.
  `relay.toml`, `workspaces.toml`, the session history and the logs are kept. To remove the secrets as
  well:

  ```bash
  security delete-generic-password -s agent-relay -a webex-bot-token
  security delete-generic-password -s agent-relay -a opencode-password
  # and, if you added app bots:
  security delete-generic-password -s agent-relay -a webex-bot-vscode
  ```

## Security in short

- Only your Webex account (`webex.allowed_email`, pinned on first contact) can drive the relay. Messages
  from anyone else are ignored and logged.
- Nothing listens on the network. relayd connects out to Webex; the apps talk to it over a Unix socket in
  your home directory. The optional OpenCode server, the PWA and the Cursor debug port bind to
  `127.0.0.1` only.
- Secrets live only in the login Keychain and are scrubbed from logs and from every message posted to
  Webex.
- Anything that changes your files or runs a command needs your approval, from the phone while you are
  away or at the desk otherwise. `/panic` stops everything at once.
- Treat each bot token like a password. If one leaks, regenerate it at developer.webex.com and store the
  new one with `pbpaste | uv run relay setup bot <name> --stdin`.
