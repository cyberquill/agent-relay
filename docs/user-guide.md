# User guide

agent-relay lets you drive the coding agents on your Mac from your phone. You chat with a Webex bot. Each
agent session gets its own thread in that chat: you see the result of each turn, answer the agent's
questions and send it new prompts. The relay never asks you to approve a tool. The agents keep running on
the Mac, in the same apps you use at the desk, so you can pick a session up again at the desk.

It works with these apps:

| App | What the phone can do | Word to use in `/new` |
|---|---|---|
| VS Code, Copilot agent (Agent Host) | Start sessions, get each turn's result, park and wake | `copilot` |
| VS Code, its own agent chat (VS Code calls it "Local"; through the relay-bridge extension) | Start sessions, get each turn's result, park and wake | `vscode` |
| Cursor CLI (`cursor-agent`) | Start sessions, get each turn's result, wake an idle session | `cursor` |
| OpenCode | Start sessions, get each turn's result, wake an idle session | `opencode` |
| Cursor desktop app | Sessions you start at the desk are handed to the phone while away mode is on | (started at the desk) |
| GitHub Copilot CLI (`copilot` in a terminal) | Sessions you start at the desk are handed to the phone while away mode is on | (started at the desk) |

This guide covers what to set up once, how to use the system day to day, and what to do when something
looks wrong. For every command and setting in detail, see [operations.md](operations.md). For the threat
model, see [security.md](security.md).

## How it fits together

- **relayd** is a small background service on the Mac, run by launchd. It holds the Webex connection,
  talks to each app and keeps track of the sessions in a local database.
- **Hooks and plugins** inside each app tell relayd what happens. When the session belongs to the phone,
  or you are away, relayd lets every tool call run (there are no approvals); otherwise the app asks you
  at the desk as it always does.
- **The Webex bot** is how the phone reaches relayd: one bot, one chat. relayd answers only your own Webex
  account.
- **Away mode** says "I am not at the desk". With away mode on, sessions you started at the desk also come
  to the phone, and a finished turn is *parked* (held open) so you can reply to it from the phone. You
  switch it by hand, with `/away` and `/back`.

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

The relay reads secrets only from the login Keychain, never from files. Each command takes the value
without it landing in your shell history.

```bash
# The Webex bot's access token: on the bot's page press "Copy Token", then
pbpaste | uv run relay setup token --stdin
# (copy something else afterwards so the token does not stay on the clipboard)
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
else. relayd posts nothing by itself when it starts, so `/help` (or `/doctor`) is how you check that
the Mac is reachable.

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
  [spikes.md](spikes.md#s7--does-anything-stop-working-with-the-screen-locked)). Locking it does not
  change away mode either: you switch that by hand. Logging out of macOS is different: it stops your
  apps and relayd with them. A brief network drop may happen as the screen locks; relayd reconnects
  within seconds.
- **Network**: the Mac needs to reach Webex (`webexapis.com` and `*.wbx2.com`). On VPN, check that Webex
  works in a browser.

## Using it from the phone

### Start a session

```
/new copilot myproject "Add a unit test for the parser"
```

The parts are the app (`copilot`, `vscode`, `cursor`, `opencode`), the workspace name, an optional model
and the prompt in quotes. With a prompt but no model, the app's default model is used. The bot opens a
thread for the session ("🟦 Copilot · myproject · <title>"), and **everything about that session happens
in that thread**.

`/new` on its own sends one flat card with four things: **Workspace** (the last one you used is
preselected), **Agent** (one list of "app · model", with each app's own *Default* first and the one you
used last preselected), **Prompt** (required) and **Start**. There are no sections to open and no model
step. When you tap Start the card disappears and the thread's first line is your confirmation; if the
start fails, the card stays and a `⚠️` line says why, so you can tap Start again. An app that is down is
left out of the list, with one line saying so. A partial command (`/new copilot myproject`) opens the
same card with those parts filled in.

### Talk to a session

Reply in the session's thread:

- Plain text is a prompt. What happens while the agent is still working depends on the session's
  queue mode (below).
- `/stop` cancels the current turn. `/status` shows what it is doing (and starts with 🏝️ while away
  mode is on), `/diff` shows the changes it made in the workspace, and `/model [name]` changes the model
  for the next turn: a bare `/model` sends a picker with the current model selected, and when you apply
  it the card disappears and the thread says `Model → X from the next turn.` A typed `/model x` is
  checked against the app's list (id or name) and answers the same way.

**One message per turn.** While the agent works the phone is silent: no streamed text, no tool
lines, nothing when a turn parks. When the turn ends you get **one message**: the agent's final answer
(what it said after its last tool call, not its narration) and a footer such as `⏱ 3m · 🔧 14 · 📝 4
files (+120 −8)`. The thread's first line is edited quietly as things change: ⏳ running, ✅ idle, 🅿️
parked, ▶️ active, ⚫ ended, plus 🏝️ while away mode is on. Only a few things notify you besides the
answer: a question, a `⚠️` line when a turn fails, and replies to commands you just sent. Use `/diff`
to see what changed.

**Long answers arrive as `answer.html`.** An answer over about 6 KB, or one with a table, three or more
code blocks or over 60 lines, comes as one message with a short preview and a file, `answer.html`: open it
in the phone's browser. It is a self-contained page that works in light and dark mode; links in it are plain
text, and it runs no script. A `/diff` of more than 200 lines arrives as `changes.html` the same way. To turn
this off or on for every answer, set `renderer.html_threshold` to `never` or `always` (default `auto`).

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

There are two ways to hand the agent a photo or file:

- **In a session's thread**: attach it to a message there. It goes to the agent with the text. A file sent
  on its own waits up to 10 minutes for the text it belongs to. In the thread of a *parked* desk session,
  a reply with a file reaches the chat at once, with the file's path in the text (the desk apps cannot
  take a file themselves).
- **With `/new`**: attach it to a `/new` message, or send it on its own at the top level first. A file
  sent at the top level is **held for 10 minutes** (`attachments.hold_s`) and goes with the next `/new`,
  whether you start it from the card or with a one-line command; the bot says "📎 Got N file(s); it'll go with
  your next /new (within 10 min)" and the `/new` card shows "📎 2 files will be attached". If the start fails, the files
  are kept for your next try.

The file is saved in the workspace under `.agent-relay/inbox/` (kept out of `git status`). The Copilot
agent, OpenCode and the Cursor CLI receive the file itself; the other apps get its path in the prompt.
A file that cannot be used (too large, over 20 MB, or the download failed) gets one `⚠️ Skipped …` line
naming it, and the text still goes. A session's files are deleted a day after it ends. Settings:
[operations.md](operations.md#settings-added-in-the-october-and-november-rounds).

Agents can send you a file too: an agent that writes a report can save it as HTML in the workspace and
send it with its `send_phone_file` tool (only from a session that reaches the phone, and only a file
inside its own workspace).

### No approval cards

The relay never asks you to approve a tool. While a session belongs to the phone, or you are away, every
tool call it handles is allowed at once, and phone sessions are started with each app's own auto-approve
flag where there is one. A session you are sitting at, with away mode off, keeps the app's own prompt at
the desk. `/panic` stops the relay answering: while paused the desk apps ask you themselves. What the
agent may do while you are away is bounded by the `agent-relay-away` skill's "never do while away" list,
not by the relay. The thread is quiet during a turn; use `/diff` afterwards to see what changed.

### When a turn ends

For a session started from the phone, a reply in its thread always works: when the turn has ended, the
relay starts a new one with your message.

With away mode on, VS Code (both `vscode` and `copilot`), the Copilot CLI and the Cursor desktop app also
*park* the finished turn: the final answer arrives as usual, the thread's first line shows **🅿️**, and the
agent's turn is held open, so your reply continues the same turn instead of starting a new one. Nothing
extra is posted for the parking. This is what lets you answer a session you started **at the desk**,
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
| New session | `/new` | One flat card: Workspace, Agent (app · model), Prompt, Start |
| Question | an agent's question (`ask_phone`, or an app's own) | Its options, a free-text answer, or Skip |
| Queue | `/queue` | The queue mode, and each waiting message with Remove |
| Model | `/model` in a thread | The app's models, with the current one selected |
| Sessions | `/sessions` | Status, Diff, Stop or Back for the session you pick |
| Cancel everything? | `/panic` | Confirm or cancel, within 60 s |
| Workspaces | `/ws` (with `ws.roots`) | Add a folder, clone a repo, add a repo to a workspace |

A card that has done its job **disappears**: `/new` after Start, `/model` after Apply, a question after
you answer it, `/panic` after you confirm. `/sessions` and `/queue` stay, because you use them again.
If a tap fails, the card stays so you can tap again.

The line of text above each card is what the phone's notification shows, so it names what needs you.
Links in an agent's text arrive as plain code, never as tappable links.

### Other commands

| Command | What it does |
|---|---|
| `/sessions` | A card: pick a session, then Status, Diff, Stop or Back. The card keeps working for 15 minutes |
| `/digest` | Every session in one message; the "Waiting for you" part lists open questions |
| `/apps` | Lists the apps and whether each one is reachable |
| `/ws` | Lists the workspaces; with `ws.roots` set, a menu to add one (see below) |
| `/models <app>` | Lists the models an app offers |
| `/away [on\|off]` | Turns away mode on (or off); the same at the top level and in a thread (see below) |
| `/back` | Turns away mode off: releases parked desk sessions and hands VS Code and Cursor desktop chats started from the phone to the desk. In a thread it also says what it released |
| `/queue`, `/steer` | In a thread: see [Messages sent while a turn runs](#messages-sent-while-a-turn-runs-queue-and-steer) |
| `/doctor` | The same health report as `relay doctor` |
| `/panic` | Asks first with a card (good for 60 s), then cancels every running turn, closes every open question, releases parked turns, turns away mode off and pauses the relay. `/panic now` skips the question |
| `/help` | The command list |

While paused, away mode counts as off everywhere, so desk sessions stay at the desk, the relay answers no
tool call, and agents cannot message the phone through `notify_phone`, `ask_phone` or `send_phone_file`.
Send `/away` to resume.

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

`/away` when you leave: your desk chats come to the phone, and a reply continues them. `/back` when you
return.

That is the whole feature. It is one switch for the whole Mac, and **you** flip it: nothing turns it on
when the screen locks or the Mac sleeps. Send `/away` or `/back` from the phone (in a thread or in the main
chat, the same either way), type `/away` or `/back` in any agent chat at the desk (the `agent-relay-away`
skill runs `relay away on|off`), or run `uv run relay away on` (or `off`) in a terminal. While it is on,
`/status` and every thread's first line show 🏝️, so you do not forget it. Sessions you start from the
phone work the same with away mode on or off.

While away mode is on, agents can also reach you themselves with the `relay-mcp` tools: `notify_phone`
posts a line in the session's thread, `ask_phone` asks a question and waits up to 15 minutes for your
reply, and `send_phone_file` sends a file from the workspace (for a report: write HTML, send it). The
skill tells the agent to use them, to decide for itself rather than block on small questions, and to
follow its "never do while away" list.

## Picking work up at the desk

- **VS Code**: phone sessions are ordinary VS Code chats. `vscode` sessions open as a new chat in
  the window for that workspace. VS Code Copilot sessions run in VS Code's Agent Host and appear in the
  Chat view's session list once their first turn has started (a few seconds after `/new`):
  - A workspace with a `file` (a `.code-workspace` file) in `workspaces.toml`, which is also what
    adding a repo to a plain workspace from the phone creates, shows its sessions **only in the window opened from that file**, like a chat started there.
  - A workspace that is a plain folder shows its sessions in **any window that contains the folder**.
  - You can type in the session at the desk once the agent has finished its turn. Sessions started
    before this change ran in a hidden second chat and do not appear; read them through the
    `agent-relay-recall` skill (below).
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
| The bot does not answer at all | The Mac is asleep or offline, or relayd is down. At the desk: `relay status`; if it fails, `relay start`. `relay doctor` also shows whether Webex is connected. |
| `relay doctor` says "Webex token rejected (401)" | The bot's token is dead or was regenerated. Copy the new one and run `pbpaste \| uv run relay setup token --stdin`. relayd keeps running meanwhile and picks the token up on its next retry (within about 5 minutes); no restart is needed. |
| `relay doctor` lists a setting such as `webex.bots` or `approvals.*` | It was removed in the November round and is ignored. Run the `relay config unset <key>` command it prints. |
| The bot answers "paused" | `/panic` was used. Send `/away`. |
| `relay start` says launchd still lists relayd | A stop is still finishing. Wait a few seconds and run it again. |
| A desk session never shows up on the phone | Away mode is off (`/status` or `relay doctor` shows it), or the app was opened before `relay install --apply`. Send `/away`; reload VS Code or restart Cursor if needed. |
| `/new` says an app is unavailable, or the app is missing from the Agent list | Open the app (VS Code must be running for both VS Code apps) and check `/apps`. For the Cursor CLI, run `cursor-agent login`. The card names the apps it left out. |
| `/new` shows only "Default" for Cursor over CDP | The card never opens Cursor's model menu. Run `/models cursor-ide` once; it saves the list for the cards. |
| A file I sent at the top level "never arrived" | It waits up to 10 minutes for your next `/new`; after that it is left behind. Send it again with `/new`, or in the session's thread. |
| A file got a `⚠️ Skipped …` line | The line names the file and why (too large, an unsupported type, or the download failed). Send it again, smaller or in another format. |
| A Cursor CLI reply ends with "RetriableError: WritableIterable is closed" | A known `cursor-agent` issue; the reply itself is complete. |
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
  ```

## Security in short

- Only your Webex account (`webex.allowed_email`, pinned on first contact) can drive the relay. Messages
  from anyone else are ignored and logged.
- Nothing listens on the network. relayd connects out to Webex; the apps talk to it over a Unix socket in
  your home directory. The optional OpenCode server, the PWA and the Cursor debug port bind to
  `127.0.0.1` only.
- Secrets live only in the login Keychain and are scrubbed from logs and from every message posted to
  Webex.
- The relay has **no approvals**: whoever controls your Webex account can run anything on the Mac, and
  nothing slows them down. What is left is the pinned account, one-to-one chats only, `/panic`, which
  stops everything at once, and the logs. A session at the desk with away mode off keeps the app's own
  prompts. See [security.md](security.md).
- `answer.html` runs no script and loads nothing from the internet; links in it are plain text.
- Treat the bot token like a password. If it leaks, regenerate it at developer.webex.com and store the
  new one with `pbpaste | uv run relay setup token --stdin`.
