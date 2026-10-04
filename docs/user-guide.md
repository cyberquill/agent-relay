# User guide

agent-relay lets you drive the coding agents on your Mac from your phone. You chat with a Webex bot. Each
agent session gets its own thread in that chat: you see the agent's replies, answer its approval cards and
send it new prompts. The agents keep running on the Mac, in the same apps you use at the desk, so you can
pick a session up again at the desk.

It works with these apps:

| App | What the phone can do | Word to use in `/new` |
|---|---|---|
| VS Code, Copilot agent (Agent Host) | Start sessions, stream replies, approve tools, park and wake | `copilot` |
| VS Code, Local chat (through the relay-bridge extension) | Start sessions, stream replies, approve tools, park and wake | `local` |
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
- **Screen lock**: locking the screen should not stop anything, since relayd and the agents run in the
  background. This is still to be confirmed live (see [TODO.md](../TODO.md)).
- **Network**: the Mac needs to reach Webex (`webexapis.com` and `*.wbx2.com`). On VPN, check that Webex
  works in a browser.

## Using it from the phone

### Start a session

```
/new copilot myproject "Add a unit test for the parser"
```

The parts are the app (`copilot`, `local`, `cursor`, `opencode`), the workspace name, an optional model
and the prompt in quotes. Leave any part out and the bot asks for it with a card: `/new` on its own walks
you through all of them. With a prompt but no model, the app's default model is used. The bot answers
with a session card, and **everything about that session happens in the card's thread**.

### Talk to a session

Reply in the session's thread:

- Plain text is a prompt. While the agent is still working, the text is delivered at its next step.
- `/stop` cancels the current turn. `/status` shows what it is doing, `/diff` shows the changes it made
  in the workspace, and `/model [name]` shows or changes the model for the next turn.

Replies stream into the thread in chunks, and tool calls show as 🔧 lines.

### Approve tools

Before a tool that changes something runs (a shell command, a file edit, an MCP call), the thread gets an
**Approval needed** card with what the agent wants to run. Choose **Allow**, **Allow for session**
(the same tool is not asked again in this session) or **Deny**. Read-only tools such as reading files and
searching are allowed without asking (`approvals.auto_allow`).

An unanswered card is **denied after 10 minutes** (`approvals.deadline_s`), and the agent is told so. If
you come back to the desk and turn away mode off, cards from desk sessions move back to the desk prompt
(🖥️ on the card).

### When a turn ends

What happens when the agent finishes depends on the app:

- **VS Code (Copilot and Local) and the Cursor desktop app** *park* the finished turn: the thread shows
  **🅿️ Parked**. Reply in the thread and the agent carries on with your message. `/back` in the thread
  releases it without a reply. A parked turn is released by itself after 4 hours (`park.deadline_s`).
- **OpenCode and the Cursor CLI** simply go idle. Reply in the thread whenever you like and the session
  wakes up with your message.

Phone-started sessions behave this way all the time. Sessions started at the desk do so only while
away mode is on.

### Other commands

| Command | What it does |
|---|---|
| `/sessions` | Lists sessions, with their state |
| `/apps` | Lists the apps and whether each one is reachable |
| `/ws` | Lists the workspaces |
| `/models <app>` | Lists the models an app offers |
| `/away [on\|off]` | Turns away mode on or off (see below). A bare `/away` means on |
| `/back` | In a thread: releases that parked session |
| `/doctor` | The same health report as `relay doctor` |
| `/panic` | Cancels every running turn, denies every open card, releases parked turns, turns away mode off and pauses the relay |
| `/help` | The command list |

While paused, away mode counts as off everywhere, so desk sessions stay at the desk, and agents cannot
message the phone through `notify_phone` or `ask_phone`. Send `/away on` to resume.

## Away mode: handing desk sessions to the phone

Sessions started from the phone always report to the phone. Sessions you started **at the desk** stay at
the desk until you turn away mode on for their workspace. Then each one gets its own thread on the phone,
its approvals go to the phone instead of the desk prompt, and its finished turns park.

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

While away mode is on, agents can also reach you themselves: relay install registers the `relay-mcp`
server, whose `notify_phone` tool posts a line in the session's thread and whose `ask_phone` tool asks a
question and waits up to 15 minutes for your reply. The `/away` command tells the agent to use them,
and to decide for itself rather than block on small questions.

## Picking work up at the desk

- **VS Code**: phone sessions are ordinary VS Code chats. VS Code Local sessions open as a new chat in
  the window for that workspace; VS Code Copilot sessions run in VS Code's Agent Host, so look for them
  in VS Code's list of chat sessions.
- **Cursor CLI**: the session card shows the session id. Continue in a terminal with
  `agent resume <id>`.
- **OpenCode**: the session is in OpenCode's session list for that workspace (`opencode` in the folder,
  then the sessions picker).
- **Cursor desktop**: desk sessions simply carry on where you left them. Turn away mode off with `/back`.

Avoid typing into a session at the desk while the phone is driving it: the two will interleave.

### Optional: start Cursor desktop chats from the phone

By default the phone starts Cursor work through the Cursor CLI. An experimental adapter can instead type
phone prompts into the Cursor desktop chat over Chrome's debugging protocol (CDP). It is off by default
and breaks when Cursor's UI changes. To try it:

1. `uv sync --frozen --extra cdp` and `uv run relay config set cdp.enabled true`.
2. Quit Cursor. The relay restarts it with a debug port that listens only on `127.0.0.1`.
3. Open Cursor's chat pane and run `uv run relay selftest cursor-cdp`; every check must pass.
4. On the phone, use `/new cursor-ide <workspace> "prompt"`.

After every Cursor update, run the self-test again; until it passes, the adapter refuses to run.

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
- Treat the bot token like a password. If it leaks, regenerate it at developer.webex.com and update the
  Keychain item with the same `security add-generic-password -U …` command.
