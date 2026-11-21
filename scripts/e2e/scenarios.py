"""Scenarios 1 to 5 and 7. Each takes the shared `Env` (and the app under test) and returns a detail line,
or raises ScenarioFailed / ScenarioSkipped."""

from __future__ import annotations

from pathlib import Path

from scripts.e2e.cardkit import card_of, fill_new, has_card
from scripts.e2e.env import OK_PROMPT, OK_WORD, Env, Thread
from scripts.e2e.files import PDF_NAME, PNG_NAME, html_problems, write_samples
from scripts.e2e.model import Msg, ScenarioFailed
from scripts.e2e.watch import Watch

ANSWER_FILE = "answer.html"
# Fixed prompts (deviation D123): a long table must arrive as `answer.html`; the shell answer is computed,
# so the prompt text alone can never contain it.
TABLE_PROMPT = (
    "Without using any tools, reply with three markdown tables and nothing else: "
    "n, n squared and n cubed for n from 1 to 12; the first 12 primes with their index; "
    "the 12 months with their number of days."
)
SHELL_COMMAND = "echo relay-e2e-$((6*7))"
SHELL_PROMPT = (
    f"Run this exact shell command in the workspace and reply with only its output: `{SHELL_COMMAND}`"
)
SHELL_OUTPUT = "relay-e2e-42"
FILES_PROMPT = (
    "List the file names of the files attached to this conversation, comma separated, nothing else."
)


async def help_replies(env: Env, _app: str) -> str:
    top = await env.watch().start()
    await env.post("/help")
    first = await top.wait_for(lambda _m: True, env.settings.command_s, "the /help reply")
    await env.settle([top])
    if len(top.messages) != 1:
        shown = "; ".join(env.summary(m) for m in top.messages)
        raise ScenarioFailed(f"{len(top.messages)} messages after /help, expected one ({shown})")
    if "/new" not in first.body:
        raise ScenarioFailed(f"the /help reply does not list /new: {env.summary(first)}")
    return "one reply, nothing else"


async def _follow_new(env: Env, app: str, top: Watch, ignore: list[str]) -> str:
    """After a `/new` was sent: a thread root, then exactly one final message `OK`, nothing in between."""
    root = await top.wait_for(
        lambda m: not m.attachments and m.id not in ignore, env.settings.card_s, "the thread root"
    )
    if "scratch" not in root.body.lower():
        raise ScenarioFailed(f"the thread root does not name the workspace: {env.summary(root)}")
    top.ignore(root.id)
    thread = await env.watch(root.id).start(fresh=True)
    await thread.wait_for(lambda _m: True, env.settings.turn_s, "the final message")
    await env.settle([thread, top])
    final = env.expect_clean(thread.messages, top.messages)
    if not OK_WORD.search(final.body):
        raise ScenarioFailed(f"the final message is not OK: {env.summary(final)}")
    env.threads[app] = Thread(app, root)
    return "card deleted, root, one final message OK" if env.can_submit else "root, one final message OK"


async def new_session(env: Env, app: str) -> str:
    """`/new` card, submitted with workspace `scratch`, this app and the OK prompt."""
    env.budget.check(app)
    top = await env.watch().start()
    await env.post("/new")
    card_msg = await top.wait_for(lambda m: has_card(m, "new"), env.settings.card_s, "the /new card")
    top.ignore(card_msg.id)
    view = card_of(card_msg)
    assert view is not None
    inputs = fill_new(view, harness=app, prompt=OK_PROMPT)
    env.budget.charge(app)
    await env.user.submit_card(card_msg.id, inputs)
    await env.card_deleted(card_msg.id)
    return await _follow_new(env, app, top, [card_msg.id])


async def new_session_typed(env: Env, app: str) -> str:
    """The one-line form, for when the owner's token cannot submit cards."""
    env.budget.check(app)
    top = await env.watch().start()
    env.budget.charge(app)
    await env.post(f'/new {app} scratch "{OK_PROMPT}"')
    return await _follow_new(env, app, top, [])


def thread_of(env: Env, app: str) -> Thread:
    thread = env.threads.get(app)
    if thread is None:
        raise ScenarioFailed(f"no session thread for {app}")
    return thread


async def thread_reply(env: Env, app: str) -> str:
    thread = thread_of(env, app)

    async def send() -> Msg:
        return await env.post(OK_PROMPT, parent_id=thread.root.id)

    result = await env.turn(thread, send)
    if not OK_WORD.search(result.final.body):
        raise ScenarioFailed(f"the reply is not OK: {env.summary(result.final)}")
    return "one final message"


async def file_reply(env: Env, app: str) -> str:
    """One file with the question, per turn. A files-only message to a parked desk chat goes at once (the
    hooks cannot hold it), so a file sent alone first would start a turn with nothing to answer."""
    thread = thread_of(env, app)
    png, pdf = write_samples(env.tmp_dir)
    for path, name in ((png, PNG_NAME), (pdf, PDF_NAME)):

        async def send(path: Path = path) -> Msg:
            return await env.post(FILES_PROMPT, parent_id=thread.root.id, file=path)

        result = await env.turn(thread, send)
        if name not in result.final.body:
            raise ScenarioFailed(f"the agent did not name {name}: {env.summary(result.final)}")
    return "the agent named both files"


async def html_answer(env: Env, app: str) -> str:
    thread = thread_of(env, app)

    async def send() -> Msg:
        return await env.post(TABLE_PROMPT, parent_id=thread.root.id)

    result = await env.turn(thread, send)
    if not result.final.files:
        raise ScenarioFailed(f"the long table arrived without a file: {env.summary(result.final)}")
    download = await env.user.download(result.final.files[0])
    if download.name != ANSWER_FILE:
        raise ScenarioFailed(f"the file is named {download.name}, expected {ANSWER_FILE}")
    problems = html_problems(download.data)
    if problems:
        raise ScenarioFailed(f"{ANSWER_FILE} is not safe: {'; '.join(problems)}")
    return f"{ANSWER_FILE} {len(download.data) / 1024:.1f} KB, CSP present, no script, no href"


async def shell_without_approval(env: Env, app: str) -> str:
    thread = thread_of(env, app)

    async def send() -> Msg:
        return await env.post(SHELL_PROMPT, parent_id=thread.root.id)

    result = await env.turn(thread, send)  # expect_clean fails on any approval card
    if SHELL_OUTPUT not in result.final.body:
        raise ScenarioFailed(f"the command output {SHELL_OUTPUT} is missing: {env.summary(result.final)}")
    return "no approval card; the command ran"
