"""Files sent from the phone: downloaded safely, held for their text, and reported honestly."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_relay.core.inbox import HOLD_PREFIX, Inbox
from agent_relay.frontends.webex.attachments import AttachmentIntake
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.config import AttachmentsConfig
from agent_relay.models.events import Harness, Origin, utcnow
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import load_workspaces
from tests.frontends.conftest import Relay

NATIVE = Capabilities(
    cold_start=True, model_select=True, cancel=True, approve=True, question=True, attach_files=True
)


def configure(relay: Relay, enabled: bool = True, max_bytes: int = 20 * 1024 * 1024) -> Inbox:
    inbox = Inbox(relay.registry, AttachmentsConfig(enabled=enabled, max_bytes=max_bytes))
    relay.frontend.intake = AttachmentIntake(relay.frontend.api, inbox)
    relay.frontend.handlers.inbox = inbox
    return inbox


async def settle(relay: Relay, parent_id: str | None) -> None:
    """A mid-turn message is queued by default; these tests are about the turn it starts."""
    session = await relay.registry.session_by_thread(parent_id) if parent_id else None
    if session is not None:
        await relay.registry.set_session_status(session.session_key, SessionStatus.IDLE)


async def say(relay: Relay, text: str, parent_id: str | None) -> None:
    await settle(relay, parent_id)
    await relay.say(text, parent_id=parent_id)


async def send(
    relay: Relay,
    name: str,
    data: bytes,
    text: str = "",
    parent_id: str | None = None,
    *,
    mime: str = "text/plain",
    url: str | None = None,
    locked: int = 0,
    retry_after: str = "1",
    disposition: str | None = None,
    declare_length: bool = True,
    status: int = 200,
    location: str | None = None,
) -> str:
    message_id = relay.webex.user_sends_file(
        name,
        data,
        text,
        parent_id,
        mime=mime,
        url=url,
        locked=locked,
        retry_after=retry_after,
        disposition=disposition,
        declare_length=declare_length,
        status=status,
        location=location,
    )
    await settle(relay, parent_id)
    await relay.frontend.handle_message(await relay.frontend.api.get_message(message_id))
    await relay.router.join()
    return message_id


async def thread(relay: Relay) -> SessionRecord:
    await relay.say('/new opencode proj m1 "start"')
    return (await relay.service.sessions())[0]


def inbox_of(session: SessionRecord) -> Path:
    workspace = Path(load_workspaces().workspaces[0].path)
    return workspace / ".agent-relay" / "inbox" / session.session_key.replace(":", "_")


def delivered(relay: Relay) -> Sequence[Attachment]:
    return relay.adapter.prompt_attachments[-1]


# in a thread


async def test_text_with_a_file_reaches_the_session_with_the_file_and_says_how(relay: Relay) -> None:
    session = await thread(relay)
    await send(relay, "notes.txt", b"hello", "read this", session.webex_thread_id)
    assert relay.adapter.prompts[-1] == (session.session_key, "read this")
    (item,) = delivered(relay)
    assert Path(item.local_path).read_bytes() == b"hello"
    assert Path(item.local_path).parent.resolve() == inbox_of(session).resolve()
    assert (item.name, item.mime, item.size) == ("notes.txt", "text/plain", 5)
    assert relay.bot_texts()[-1] == "📎 notes.txt (as a path)"


async def test_an_app_that_takes_files_natively_is_reported_as_attached(relay: Relay) -> None:
    relay.adapter.capabilities = NATIVE
    session = await thread(relay)
    await send(relay, "shot.png", b"\x89PNG", "look", session.webex_thread_id, mime="image/png")
    assert delivered(relay)[0].mime == "image/png"
    assert relay.bot_texts()[-1] == "📎 shot.png (attached)"


async def test_a_files_only_message_waits_for_the_next_text(relay: Relay) -> None:
    session = await thread(relay)
    root = session.webex_thread_id
    before = len(relay.adapter.prompts)
    await send(relay, "a.txt", b"1", parent_id=root)
    await send(relay, "b.txt", b"2", parent_id=root)
    assert len(relay.adapter.prompts) == before
    assert relay.bot_texts()[-2:] == [
        "📎 Got 1 file; they'll go with your next message.",
        "📎 Got 1 file; they'll go with your next message.",
    ]
    await say(relay, "now use them", root)
    assert [a.name for a in delivered(relay)] == ["a.txt", "b.txt"]
    assert relay.bot_texts()[-1] == "📎 a.txt (as a path), b.txt (as a path)"
    await say(relay, "and again", root)
    assert delivered(relay) == ()
    assert await relay.registry.kv_prefix(HOLD_PREFIX) == {}


async def test_held_files_are_left_behind_when_their_time_runs_out(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await thread(relay)
    root = session.webex_thread_id
    await send(relay, "a.txt", b"1", parent_id=root)
    (kept,) = (p for p in inbox_of(session).iterdir())
    later = utcnow().replace(year=utcnow().year + 1)
    monkeypatch.setattr("agent_relay.core.inbox.utcnow", lambda: later)
    await say(relay, "late", root)
    assert delivered(relay) == ()
    assert not kept.exists()


async def test_a_command_with_a_file_runs_and_the_file_is_held(relay: Relay) -> None:
    session = await thread(relay)
    await send(relay, "a.txt", b"1", "/status", session.webex_thread_id)
    assert "model:" in relay.bot_texts()[-1]
    assert relay.bot_texts()[-2] == "📎 Got 1 file; they'll go with your next message."
    await say(relay, "use it", session.webex_thread_id)
    assert [a.name for a in delivered(relay)] == ["a.txt"]


async def test_a_file_named_by_the_sender_cannot_choose_where_it_lands(relay: Relay) -> None:
    session = await thread(relay)
    await send(
        relay,
        "x",
        b"1",
        "go",
        session.webex_thread_id,
        disposition='attachment; filename="../../escape.sh"',
    )
    await send(
        relay,
        "x",
        b"1",
        "go",
        session.webex_thread_id,
        disposition="attachment; filename*=UTF-8''na%C3%AFve.txt",
    )
    assert sorted(p.name for p in inbox_of(session).iterdir()) == ["escape.sh", "na_ve.txt"]
    assert not (inbox_of(session).parent.parent.parent / "escape.sh").exists()


async def test_a_download_without_a_name_gets_one_from_its_type(relay: Relay) -> None:
    session = await thread(relay)
    await send(relay, "x", b"1", "go", session.webex_thread_id, mime="image/png", disposition="inline")
    assert delivered(relay)[0].name == "file.png"


# limits and failures


async def test_a_file_over_the_cap_is_skipped_and_the_text_still_goes(relay: Relay) -> None:
    configure(relay, max_bytes=4096)
    session = await thread(relay)
    await send(relay, "big.zip", b"x" * 5000, "here", session.webex_thread_id)
    assert relay.adapter.prompts[-1] == (session.session_key, "here")
    assert delivered(relay) == ()
    assert relay.bot_texts()[-1] == "📎 Skipped big.zip (over 4 KB)"
    assert not inbox_of(session).exists() or list(inbox_of(session).iterdir()) == []


async def test_the_cap_holds_even_when_the_download_declares_no_length(relay: Relay) -> None:
    configure(relay, max_bytes=4096)
    session = await thread(relay)
    await send(relay, "big.zip", b"x" * 9000, "here", session.webex_thread_id, declare_length=False)
    assert delivered(relay) == ()
    assert relay.bot_texts()[-1] == "📎 Skipped big.zip (over 4 KB)"
    assert list(inbox_of(session).iterdir()) == []


async def test_one_big_file_does_not_cost_the_small_one_beside_it(relay: Relay) -> None:
    configure(relay, max_bytes=4096)
    session = await thread(relay)
    first = relay.webex.user_sends_file("ok.txt", b"1", "both", session.webex_thread_id)
    other = relay.webex.user_sends_file("big.bin", b"x" * 5000)
    relay.webex.messages[first].files.append(relay.webex.messages[other].files[0])
    await settle(relay, session.webex_thread_id)
    await relay.frontend.handle_message(await relay.frontend.api.get_message(first))
    await relay.router.join()
    assert [a.name for a in delivered(relay)] == ["ok.txt"]
    assert relay.bot_texts()[-1] == "📎 ok.txt (as a path); skipped big.bin (over 4 KB)"


async def test_a_file_still_being_scanned_is_retried_after_the_wait_webex_asks_for(relay: Relay) -> None:
    session = await thread(relay)
    await send(relay, "a.txt", b"1", "go", session.webex_thread_id, locked=2, retry_after="7")
    assert [s for s in relay.clock.slept if s == 7.0] == [7.0, 7.0]
    assert [a.name for a in delivered(relay)] == ["a.txt"]


async def test_a_file_that_stays_locked_is_given_up_on_after_a_bounded_number_of_tries(relay: Relay) -> None:
    session = await thread(relay)
    await send(relay, "a.txt", b"1", "go", session.webex_thread_id, locked=99, retry_after="9999")
    assert max(relay.clock.slept) == 30.0
    (content,) = relay.webex.contents.values()
    assert content.reads == 6
    assert delivered(relay) == ()
    assert relay.bot_texts()[-1] == "📎 Skipped file (Webex is still scanning it; send it again in a minute)"


async def test_a_refused_download_is_reported_not_raised(relay: Relay) -> None:
    session = await thread(relay)
    await send(relay, "a.txt", b"1", "go", session.webex_thread_id, status=403)
    assert relay.bot_texts()[-1] == "📎 Skipped a.txt (Webex refused the download (HTTP 403))"
    assert relay.adapter.prompts[-1][1] == "go"


async def test_the_bot_token_never_leaves_the_webex_api_host(relay: Relay) -> None:
    session = await thread(relay)
    await send(relay, "a.txt", b"1", "go", session.webex_thread_id, url="https://files.evil.example/steal")
    assert relay.bot_texts()[-1] == "📎 Skipped file (not a Webex file link)"
    await send(relay, "b.txt", b"1", "go", session.webex_thread_id, location="https://cdn.evil.example/steal")
    assert relay.bot_texts()[-1] == "📎 Skipped b.txt (Webex redirected the download)"
    assert not any("evil" in request for request in relay.webex.requests)
    assert relay.webex.authorized_hosts == {"webexapis.com": {"Bearer test-token"}}


async def test_a_look_alike_host_is_not_the_webex_host(relay: Relay) -> None:
    session = await thread(relay)
    await send(
        relay,
        "a.txt",
        b"1",
        "go",
        session.webex_thread_id,
        url="https://webexapis.com.evil.example/v1/contents/x",
    )
    assert relay.bot_texts()[-1] == "📎 Skipped file (not a Webex file link)"
    assert not any("evil" in request for request in relay.webex.requests)


async def test_attachments_can_be_turned_off(relay: Relay) -> None:
    configure(relay, enabled=False)
    session = await thread(relay)
    await send(relay, "a.txt", b"1", "go", session.webex_thread_id)
    assert any("Attachments are turned off" in text for text in relay.bot_texts())
    assert relay.adapter.prompts[-1] == (session.session_key, "go")
    assert delivered(relay) == ()
    (content,) = relay.webex.contents.values()
    assert content.reads == 0


async def test_a_session_with_no_workspace_cannot_take_files(relay: Relay) -> None:
    await relay.registry.create_session(
        SessionRecord(
            session_key="opencode:bare",
            harness=Harness.OPENCODE,
            harness_id="bare",
            origin=Origin.PHONE,
            webex_thread_id="thread-bare",
        )
    )
    await send(relay, "a.txt", b"1", "go", "thread-bare")
    assert any("no workspace folder" in text for text in relay.bot_texts())
    (content,) = relay.webex.contents.values()
    assert content.reads == 0


# at the top level


async def test_files_outside_a_thread_are_pointed_to_a_thread_or_new(relay: Relay) -> None:
    await send(relay, "a.txt", b"1")
    assert relay.bot_texts() == ["📎 Send files inside a session thread, or with /new."]
    await send(relay, "b.txt", b"1", "hello there")
    assert relay.bot_texts()[-1] == "📎 Send files inside a session thread, or with /new."
    assert len(relay.bot_texts()) == 2
    assert relay.adapter.started == []
    assert await relay.registry.kv_prefix(HOLD_PREFIX) == {}
    (content, _) = relay.webex.contents.values()
    assert content.reads == 0


async def test_new_with_a_file_starts_the_session_with_it(relay: Relay) -> None:
    await send(relay, "log.txt", b"boom", '/new opencode proj m1 "why did it fail"')
    assert relay.adapter.start_attachments[-1][0].name == "log.txt"
    session = (await relay.service.sessions())[0]
    item = relay.adapter.start_attachments[-1][0]
    assert Path(item.local_path).read_bytes() == b"boom"
    assert Path(item.local_path).parent.name.startswith("new-")
    assert Path(item.local_path).parent.parent.resolve() == inbox_of(session).parent.resolve()
    assert relay.bot_texts()[-1] == "📎 log.txt (as a path)"
    assert await relay.registry.kv_prefix(HOLD_PREFIX) == {}
    assert await relay.registry.kv_get(f"attach.dirs:{session.session_key}") is not None


async def test_files_sent_with_an_unfinished_new_wait_for_the_session_to_start(relay: Relay) -> None:
    await send(relay, "log.txt", b"boom", "/new opencode proj")
    assert relay.adapter.started == []
    await relay.say('/new opencode proj m1 "go"')
    assert [a.name for a in relay.adapter.start_attachments[-1]] == ["log.txt"]


async def test_a_new_session_that_fails_to_start_leaves_no_files(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_: object, **__: object) -> SessionRecord:
        raise RuntimeError("no")

    monkeypatch.setattr(relay.adapter, "start_session", refuse)
    await send(relay, "log.txt", b"boom", '/new opencode proj m1 "go"')
    workspace = Path(load_workspaces().workspaces[0].path)
    leftovers = [p for p in (workspace / ".agent-relay").rglob("*") if p.is_file()]
    assert leftovers == []


async def test_new_reports_a_skipped_file_instead_of_a_saved_one(relay: Relay) -> None:
    configure(relay, max_bytes=4096)
    await send(relay, "ok.txt", b"1", '/new opencode proj m1 "go"')
    assert relay.bot_texts()[-1] == "📎 ok.txt (as a path)"
    await send(relay, "big.bin", b"x" * 5000, '/new opencode proj m1 "again"')
    assert relay.bot_texts()[-1] == "📎 Skipped big.bin (over 4 KB)"
    assert relay.adapter.start_attachments[-1] == ()
