# Testing

Offline checks, the live end-to-end driver, and recorded live results. Which test proves which requirement:
[acceptance.md](acceptance.md).

## Offline: `uv run tox`

Must be green before every commit.

| Env | Runs |
|---|---|
| `format` | `ruff format --check` |
| `lint` | `ruff check` |
| `mypy` | `mypy --strict` |
| `pytest` | Unit and contract tests (fakes in `tests/fakes/`) |
| `ts` | `scripts/ts_check.sh`: build and test relay-bridge and the OpenCode plugin |

- Tests use a temporary `RELAY_HOME`; nothing touches the real home folder.
- Live tests are skipped unless `RELAY_LIVE=1`; that is the only allowed skip.
- `tests/test_no_stubs.py` fails on stub bodies and deferred-work markers; `tests/test_acceptance.py` checks
  [acceptance.md](acceptance.md) and the test map in [security.md](security.md).

## Live: the end-to-end driver

`scripts/phone_e2e.py` acts as **you** (the owner) through your Webex person token and checks the relay from the
phone's side.

**Before a run:**

- relayd up (`relay doctor` all ✅/ℹ️), `scratch` workspace registered, `relay install --apply` done.
- `webex-cli` signed in. The driver reads its token; never prints or stores it ([security.md](security.md)).

```bash
export RELAY_LIVE=1
.venv/bin/python scripts/phone_e2e.py --apps copilot,cursor,opencode,vscode \
    [--only 1,2,3] [--report /tmp/e2e.md] [--room-id <id>] [--turn-timeout 240] [--quiet 8] [--away-wait 180]
.venv/bin/python scripts/card_check.py [--only new_session,model_switch]   # each card posted once, expects HTTP 200, deleted
```

| Flag / rule | Detail |
|---|---|
| `--apps` | Any `/new` spelling (`copilot`, `cursor`, `cursor-ide`, `opencode`, `vscode`); desk-only harnesses (`copilot-cli`, hook-only `cursor`) refused |
| Without `RELAY_LIVE=1` | Exits 2, sends nothing |
| Exit codes | 0 no FAIL · 1 a FAIL (or nothing ran) · 2 bad arguments or setup |
| Enforced limits | Live sessions only in `scratch`; ≤ 3 per adapter per run; prompt `Reply with exactly: OK` except three fixed prompts (D123); the room must be a 1:1 with a bot |
| Clean-up | Deletes the messages it posted (relay threads and cards stay) |

**Order:** 1; then 2–7 per app, then 10 (last of the per-app ones); then 8 and 9. `--only 4` or `--only 10` also runs 2 (they need the session).

| # | Scenario | Passes when |
|---|---|---|
| 1 | `/help` | One reply listing `/new`; nothing else posted |
| 2 | `/new` card, submitted as a tap | Card deleted, thread root appears, exactly one final `OK`, nothing in between or at the top level |
| 3 | Reply in the thread | One final message |
| 4 | PNG + PDF in the thread | The agent names both files |
| 5 | Prompt yielding three tables | One message with `answer.html`; downloaded: CSP present, no `<script`, no `href=`, no external resource |
| 6 | `/model` card | Card deleted; thread says `Model → X from the next turn.`; root shows X |
| 7 | `echo relay-e2e-$((6*7))` | No approval card; reply contains `relay-e2e-42` |
| 8 | `/away`, a desk chat, `/back` | Desk chat's result opens a thread, a reply continues it, `/back` releases it. Needs **you** to run one desk turn in `.scratch` within `--away-wait` s (else SKIP). Always ends with `/back` |
| 9 | `/panic` | Confirm card submitted, everything stops; ends with `/away`, `/help`, `/back` (away left off) |
| 10 | `/stop` a long turn, then a follow-up | The chat still answers (D136) |

- If the person token cannot submit cards (`POST /attachment/actions` needs a scope; probed once), 2, 6 and 9 run typed
  forms (`2b`, `6b`, `9b`) and the card row is `SKIP (no scope: submit the card on the phone)`. Then check the
  `/new` and `/model` card layout on your phone once.

## Live results

### November round (2026-11-21)

`scripts/phone_e2e.py` as the owner (webex-cli person token), `.scratch` only, ≤ 3 per adapter. Several runs (each
failure fixed, app rerun); last result per cell.

| Check | Result |
|---|---|
| `card_check.py`: every card accepted (HTTP 200) | ✅ 14 / 14 |
| 1 `/help` | ✅ |
| 2 `/new` card, one final OK | ✅ Copilot (AHP), VS Code, Cursor CLI, OpenCode |
| 3 Reply in a thread | ✅ VS Code, Copilot, Cursor IDE (CDP), Cursor CLI, OpenCode |
| 4 Image + PDF reach the agent | ✅ VS Code, Copilot, Cursor IDE (CDP), Cursor CLI (after D131), OpenCode |
| 5 Table answer as `answer.html` (CSP, no script, no href) | ✅ VS Code, Copilot, Cursor IDE (CDP), Cursor CLI, OpenCode |
| 6 `/model` card, next turn uses it | ✅ VS Code (claude-haiku-4.5), Copilot (claude-sonnet-5.5), Cursor IDE (Grok 4.7 High), Cursor CLI, OpenCode |
| 7 Shell command, no approval card | ✅ VS Code, Copilot, Cursor IDE (CDP), Cursor CLI (`--force`), OpenCode (`always`) |
| 8 Away round trip | ✅ VS Code Copilot chat (owner, 20:14): `OK` reached the phone and parked, reply `BACK` delivered and answered, `/back` released it. Cosmetic: desk shows `OKBACK` in one bubble and not the phone message (D135, D137); root said `copilot_cli` (fixed offline by D140, not re-run). Not checked: Cursor, Copilot CLI |
| 9 `/panic` | ✅ Everything stopped, `/away` resumed, away left off |
| 10 `/stop`, chat still answers | ✅ VS Code, Copilot, Cursor IDE (CDP), Cursor CLI, OpenCode |
| Cursor CDP on Cursor 3.31.15 (selectors v4, D133, D134) | ✅ Self-test passes; 2–7 pass (`/new cursor-ide`, reply, image + PDF, `answer.html`, `/model` Grok 4.7 High, shell). Not run: 8 on a Cursor desk chat |
| Android look of `/new` and `/model` cards | Not run (owner) |

Later runs the same day (owner lifted the three-session cap for these apps):

- **VS Code and Copilot (AHP), 2–7: 12 / 12 ✅** after driver fixes. Earlier failures were the driver's:
  - counted the relay's `📨 delivered…` / `📨 Queued…` receipts as extra messages (`scripts/e2e/cardkit.py::is_receipt`);
  - sent a PNG alone (a files-only message to a parked chat goes at once, so the agent asked what to do; each file now goes with its question);
  - read the model from a root line whose 8-edit budget a long thread uses up (falls back to `/status`).
- **Cursor IDE (CDP) on 3.31.15, 2–7: 6 / 6 ✅** after selectors v4 (D133) and D134 (its own hooks made a second
  desk thread for every phone chat; the reply was missed once old rows left the page).

Open items for the owner:

1. After a VS Code restart, a Copilot thread made before it cannot be replied to (`Session was never created on the backend`, shown as "That failed (AhpRpcError)").
2. Scenario 8 needs the owner at the desk: see [TODO.md](../TODO.md).
3. Android, as above. AHP `autoApprove` is only indirectly confirmed (7 ran a tool with no card); a desk-session check of the three apps is still open.

### 2026-11-22: phone sessions in the Agent Host list (D138)

Driven by sending `/new copilot <workspace> "Reply with exactly: OK"` as the owner (`webex-cli dm`).

| Step | Result |
|---|---|
| First attempt, the plan's `default` chat URI | Failed: `subscribe failed (-32001): Resource not found: ahp-chat://default/…`. VS Code 1.147 names a session's default chat `ahp-chat:/<uuid>` in the snapshot (`defaultChat`); the `default` form does not exist |
| After the fix, `.scratch` | `OK` on the phone; `listSessions` shows `defaultChat` `ahp-chat:/5252f07f…`; parked: default chat had `activeTurn`; after `/back`: 1 turn with prompt and reply |
| `rms-oncall` (approved) | `OK`; listing carries `_meta.multiRoot.workspaceFile` = `…/VSCode_WS/rms-oncall.code-workspace` and the 11 folders as `workingDirectories` |
| Not checked | The session in the open window's Chat view (no display access). Look for the two `reply with OK` sessions in the `.scratch` and `rms-oncall` windows |

Live sessions used: 3 `copilotcli` sessions in the Agent Host (one failed before any turn, one probe with no prompt, two relay runs).

### October round smoke (2026-10-04, `scripts/smoke_live.py`, since deleted)

It checked approval cards, which no longer exist. Every step passed (✅); "human: y" means confirmed on the phone.

| UTC | App | Steps | Session |
|---|---|---|---|
| 06:55 | VS Code Local | register, stream, approve, park, reply, back | `vscode_local:phone-6f3b92d31b1f41f2a34e8ef424aa0bd0` |
| 07:14 | VS Code Copilot harness | register, stream, approve, park, reply, back | `vscode_copilot:f077b1e0-6b86-4ade-a287-823a5f51b634` |
| 07:22 | OpenCode | register, stream, approve, idle, reply | `opencode:ses_efa3584c7ffe8OcQqD0xpE3otB` |
| 07:23 | Cursor CLI | register, stream, approve, idle, reply | `cursor_cli:221381c9-a008-4f1a-afc5-50645d66fb3d` |
| 08:41 | Cursor IDE (hooks and parking) | register, stream, approve, park, reply, back | `cursor:0ebc1785-3f0c-432b-a495-4a4d4ee23c25` |
