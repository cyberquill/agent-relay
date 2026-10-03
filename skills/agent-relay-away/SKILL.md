---
name: agent-relay-away
description: >-
  Switches Agent Relay away mode on or off for this workspace and sets how the agent behaves while
  the user is away from the desk. Use when the user types /away or /back, says they are stepping
  away, going afk, heading out, in a meeting, on their phone, or back at the desk, or asks to be
  kept posted on Webex while a task runs. Agent Relay posts progress and asks for approvals on its
  own; this skill only flips the mode and states the away-mode contract.
---

# Agent Relay: away mode

Agent Relay mirrors this session to the user's phone (Webex) through hooks. It posts tool use,
approvals and the final answer itself, at no cost to you. **Do not post progress yourself.**

## Turning it on: `/away`

Run, from the workspace root:

```bash
relay away on --workspace "$PWD"
```

Say in one line that away mode is on, then keep working under the contract below.

## Turning it off: `/back`

```bash
relay away off --workspace "$PWD"
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
5. **Use `ask_phone` only for real blockers**: a missing credential, or an action you must not take
   alone. It waits up to 15 minutes and returns `answered`, `timeout` or `denied`. On `timeout` or
   `denied`, take the safe path and continue with other work.
6. **Use `notify_phone` sparingly**, for one line the hooks cannot see (for example "starting a
   20-minute build").

## Never do these while away

Choose another path and note it in the status summary instead:

- force-push, push to `main`/`master`/`develop`, merge a PR, or delete a branch;
- `git reset --hard`, `git clean -fd`, or discard work you did not create in this session;
- publish a release, tag, package or image; run a migration against a shared environment;
- rotate or write credentials, or put a secret in a tracked file;
- `rm -rf` outside build output and temp folders;
- add a dependency nobody asked for, bump a major version, or change CI or deployment config;
- message anyone other than through Agent Relay.

## When blocked

Fix it; else work around it and say what was faked; else narrow the scope; else park it and take
the next independent piece. Never retry the same failing step more than twice: change approach.
Stop only when nothing else can proceed, and then say exactly what would resume the work.
