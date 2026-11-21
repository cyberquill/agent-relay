---
name: away
description: Turn Agent Relay away mode on and work under the away-mode contract
---

Turn Agent Relay away mode on: run `relay away on`, confirm in one line, then follow the away-mode
contract in the `agent-relay-away` skill until the user runs `/back`. Decide instead of asking, prefer
reversible changes, log each decision in one line, end every turn with a status summary of at most 3
lines, send rich output as HTML with `send_phone_file`, and use `ask_phone` only for real blockers.
