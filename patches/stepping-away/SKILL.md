---
name: stepping-away
description: >-
  Switches the agent into away-mode when the user steps away: never block on questions, decide with
  best judgement, record every decision. Agent Relay now posts progress to the user's phone through
  hooks, so this skill no longer posts to Webex itself. Use this skill whenever the user says they
  are stepping away, going afk, heading out, in a meeting, driving, on their phone, away from the
  machine, or asks to be kept posted or updated on Webex while a task runs. Triggers on phrases like
  "stepping away", "afk", "brb", "keep me updated", "post progress to webex", "ping me on webex",
  "I won't be at my desk", "run this while I'm out", or any request to work autonomously and report
  progress asynchronously.
---

# Stepping Away

The user is **not at the machine**. They are reading the phone, if anything.

**Agent Relay does the posting.** Its hooks mirror this session to Webex: tool use, approvals and the
final answer reach the phone without any Webex tool calls from you. Do not open a thread, post
updates, poll replies or attach files yourself. Replies from the phone arrive in this session as
messages.

## Start

Follow the [`agent-relay-away`](../agent-relay-away/SKILL.md) skill: run
`relay away on --workspace "$PWD"`, confirm in one line, then work under the contract below.

## The away-mode contract

1. **Never wait for input.** No clarifying questions, no confirmation prompts. Decide and continue.
2. **Never stop early.** Finish the task or reach a genuine hard stop. A hard stop is a missing
   credential or a destructive action — not ambiguity.
3. **Prefer reversible.** When unsure, take the option that is easy to undo, and say so.
4. **Log every decision** in your reply, one line each, so the phone shows it.
5. **End each turn with a status summary of at most 3 lines.**
6. **Use `ask_phone` only for real blockers**; on `timeout` or `denied`, take the safe path.

Decision rules, safety rails and the blocked-work ladder: [reference/away-mode.md](reference/away-mode.md).
Its notes about posting to the Webex thread and `sa.py` are superseded: Agent Relay posts, and the
decision log lives in your replies and commit messages.

## Ending

The user eventually comes back to the terminal. The final chat response must stand on its own: lead
with the outcome, list the decisions made for them and anything that still needs them. When they
say they are back, run `relay away off --workspace "$PWD"`.
