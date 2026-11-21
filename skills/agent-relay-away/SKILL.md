---
name: agent-relay-away
description: >-
  Switches Agent Relay away mode on or off and sets how the agent behaves while the user is away
  from the desk. Use when the user types /away or /back, says they are stepping away, going afk,
  heading out, in a meeting, on their phone, or back at the desk, or asks to be kept posted on
  Webex while a task runs. Agent Relay sends the final result of each turn and your questions to the
  phone on its own; this skill only flips the mode and states the away-mode contract.
---

# Agent Relay: away mode

Away mode is one global switch, flipped by hand: `/away` when the user leaves, `/back` when they
return. Nothing turns it on by itself (not the screen lock, not sleep).

While it is on, Agent Relay mirrors this session to the user's phone (Webex) through hooks. The phone
gets **the final result of each turn**, your questions, and errors; it does not see tool calls or
progress. A reply from the phone continues this chat. **Do not post progress yourself.**

## Turning it on: `/away`

Run:

```bash
relay away on
```

Say in one line that away mode is on, then keep working under the contract below.

## Turning it off: `/back`

```bash
relay away off
```

Say in one line that away mode is off. Ask questions normally again.

If `relay` is not found or prints `relayd is not running`, say so once and keep working under the
contract anyway. Never retry it in a loop.

## The away-mode contract

While away mode is on, the user reads a phone, not this chat.

1. **Decide instead of asking.** Resolve every question in this order: the repo (existing patterns,
   `AGENTS.md`, config), the task's stated goal, reversibility, then the smallest scope. Never post
   a decision as a question.
2. **Prefer reversible changes.** Commit often on a feature branch. When unsure, pick the option that
   is cheapest to undo and say so.
3. **Log decisions.** Each judgement call goes in your reply as one line: `Decided: <choice>, because
   <why>; not <alternative>`. Commit messages carry the same reason.
4. **End every turn with a status summary of at most 3 lines**: what is done, what is next, and
   anything that needs the user. It is what the phone shows.
5. **Rich output goes out as a file.** For a report, a table or anything long, write a self-contained
   HTML file in the workspace and call `send_phone_file` with its path (and a one-line caption).
   Keep the chat reply short.

## Phone tools

- `ask_phone`: only for real blockers, such as a missing credential or an action you must not take
  alone. It waits up to 15 minutes and returns `answered`, `timeout` or `denied`. On `timeout` or
  `denied`, take the safe path and continue with other work.
- `notify_phone`: sparingly, for one line the phone would not otherwise see (for example "starting a
  20-minute build").
- `send_phone_file`: sends a file from the workspace to the phone (reports, screenshots, HTML).

## Never do these while away

Choose another path and note it in the status summary instead:

- force-push, push to `main`/`master`/`develop`, merge a PR, or delete a branch;
- `git reset --hard`, `git clean -fd`, or discard work you did not create in this session;
- publish a release, tag, package or image; run a migration against a shared environment;
- rotate or write credentials, or put a secret in a tracked file;
- `rm -rf` outside build output and temp folders;
- add a dependency nobody asked for, bump a major version, or change CI or deployment config;
- message anyone other than through Agent Relay.

The relay has no deny list of its own: this list and `/panic` are the safety net.

## When blocked

Fix it; else work around it and say what was faked; else narrow the scope; else park it and take
the next independent piece. Never retry the same failing step more than twice: change approach.
Stop only when nothing else can proceed, and then say exactly what would resume the work.
