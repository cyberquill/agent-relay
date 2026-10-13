---
name: agent-relay-recall
description: >-
  Finds and reads an earlier coding-agent chat from its transcript on this Mac, so a new session can
  use what was said or decided there. Use when the user asks to find or read an earlier chat,
  conversation or thread about a topic, asks what was decided in the other chat, or says to pick up
  where they left off in a session that is not this one. Covers VS Code Copilot chat, Copilot CLI,
  Cursor and OpenCode. Read-only: it starts a new chat that reads the old one, it does not resume it.
---

# Agent Relay: recall an earlier chat

The user cannot resume an old desk chat from the phone. They start a new session and ask you to read the
old one. **You are reading a transcript, not continuing that session.** Say so in your first line, then
summarise what you found.

## Rules

1. **Read-only.** Never write to, move, repair or delete any store below.
2. **Never paste secrets.** Transcripts hold tokens, keys, passwords and `Authorization` headers. Skip
   them, and write `[redacted]` if one is unavoidable in a quote.
3. **Summarise, don't dump.** Report the relevant turns: what was asked, what was decided, what was
   left open, which files or commands mattered. Quote at most a few short lines.
4. **Narrow before reading.** Filter by workspace, date and keyword first, then read only the matching
   session. Transcripts are large; print counts and titles before content, and cap output with `head`.
5. **Ambiguous match?** Name the two or three best candidates (app, date, title) and read the likeliest.
6. Some of these locations may be absent on a given machine. Skip what is not there and say which apps
   you checked.

Open every SQLite file read-only: `sqlite3 'file:<db>?mode=ro' '<query>'`. If it reports `locked` or
`busy`, the app has it open: for a small database copy it (`cp <db>* "$TMPDIR/"`) and query the copy.
Never copy a very large database (Cursor's can exceed 10 GB); retry `mode=ro` with a narrower query.

## VS Code chat (Copilot Chat in the editor)

- Per workspace: `~/Library/Application Support/Code/User/workspaceStorage/<hash>/chatSessions/<id>.jsonl`
  (older builds wrote `.json`). `<hash>/workspace.json` names the folder or `.code-workspace` file:
  `grep -l '<project>' ~/Library/Application\ Support/Code/User/workspaceStorage/*/workspace.json`.
- Windows with no folder open: `~/Library/Application Support/Code/User/globalStorage/emptyWindowChatSessions/<id>.json`.
- A `.jsonl` file starts with a snapshot line (`kind` 0, `v` holds `requests`, `creationDate`,
  `sessionId`) followed by patch lines (`kind` 1 or 2, a path `k`, a value `v`). The whole-session
  `.json` form has `requests` at the top level. Each request has the user's `message` (with `text`) and a
  `response` list of parts.
- Find the chat: list files by modified time (`ls -t`), then `grep -il '<keyword>'` across the
  workspace's `chatSessions`. Read the matching file with a short Python script that loads each line as
  JSON and prints request texts first.

## Copilot CLI

- `~/.copilot/session-state/<session-id>/`. `workspace.yaml` has `cwd`, `git_root`, `repository`,
  `branch`, `summary`, `created_at`, `updated_at`: grep these to pick the session.
- `events.jsonl`: one JSON event per line with `type`, `data`, `timestamp`. Read `user.message`
  (`data.content`) and `assistant.message` (`data.content`) in order; tool traffic is in other event types.
- Find the chat: `grep -il '<keyword>' ~/.copilot/session-state/*/workspace.yaml`, then
  `grep -l '<keyword>' ~/.copilot/session-state/*/events.jsonl`.

## Cursor

- Agent transcripts, plain JSONL: `~/.cursor/projects/<workspace-slug>/agent-transcripts/<id>/<id>.jsonl`.
  The slug is the workspace path with `/` replaced by `-` (some projects use a numeric id instead). Each
  line has `role` and `message.content`, a list of `text` and `tool_use` items. Easiest to read; try
  it first.
- Full history, SQLite: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`.
  - Table `composerHeaders` (columns `composerId`, `workspaceId`, `createdAt`, `lastUpdatedAt`,
    `isArchived`, `value`) lists chats: filter by date, then read `value` (JSON, has the `name`).
  - Table `cursorDiskKV`: key `composerData:<composerId>` holds the chat (`name`, `createdAt`,
    `fullConversationHeadersOnly`, an ordered list of bubble ids); each message is
    `bubbleId:<composerId>:<bubbleId>` with `type` (1 user, 2 assistant) and `text`.
  - Always use key ranges, never a full scan; the file is huge:
    `select value from cursorDiskKV where key >= 'bubbleId:<composerId>:' and key < 'bubbleId:<composerId>;'`
- `workspaceId` maps to `~/Library/Application Support/Cursor/User/workspaceStorage/<id>/workspace.json`.

## OpenCode

- One SQLite file: `~/.local/share/opencode/opencode.db` (there is no `storage/` folder on current
  versions; if `~/.local/share/opencode/storage/` exists, it is the older JSON layout of
  `session/`, `message/`, `part/` folders).
- Tables: `session` (`id`, `project_id`, `directory`, `title`, `time_created`), `message` (`id`,
  `session_id`, `time_created`, `data` JSON with `role`), `part` (`id`, `message_id`, `session_id`,
  `time_created`, `data` JSON with `type`; `type = 'text'` parts carry the words, `tool` parts the tool use).
- Find the chat: `select id, title, directory, datetime(time_created/1000,'unixepoch') from session
  where title like '%<keyword>%' or directory like '%<project>%' order by time_created desc limit 10;`
  (check the time unit first; it is milliseconds in current builds).
- Read it: join `message` and `part` on `message_id`, filter `session_id`, order by `part.time_created`,
  and select `json_extract(m.data,'$.role')` with `json_extract(p.data,'$.text')` where the part type is `text`.

## Report back

Start with: "This is a new chat reading <app> chat "<title>" from <date>; it is not a continuation."
Then give the decisions, open questions and next steps from the old chat, and ask which to act on.
