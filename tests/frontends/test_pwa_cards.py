"""The PWA draws every Adaptive Card element the Webex cards use, in a real Chromium, under the real CSP.

Each test connects the page to a running PWA, pushes a card through the hub, interacts with it and checks
what `/api/card` receives. The submit route is answered by the test, so no handler runs.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from playwright.async_api import Browser, Locator, Page, Route, async_playwright
from pydantic import JsonValue

from agent_relay.core.registry import Registry
from agent_relay.frontends.pwa.server import Pwa, build_pwa
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.cards.queue import queue_card
from agent_relay.models.config import PwaConfig
from tests.adapters.cdp_rig import chromium_binary
from tests.frontends.card_tools import actions_of
from tests.frontends.test_pwa import TOKEN, free_port, service

type Card = dict[str, JsonValue]


@dataclass
class Rig:
    pwa: Pwa
    page: Page
    submits: list[dict[str, JsonValue]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    async def show(self, card: Card) -> Locator:
        before = await self.page.locator(".card").count()
        await self.pwa.hub.post_top("a card", card)
        await self.page.wait_for_function("n => document.querySelectorAll('.card').length > n", arg=before)
        return self.page.locator(".card").last


@pytest.fixture
async def rig(registry: Registry) -> AsyncIterator[Rig]:
    svc, router = service(registry)
    port = free_port()
    pwa = build_pwa(PwaConfig(enabled=True, port=port), svc, router, TOKEN)
    assert pwa is not None
    task = asyncio.create_task(pwa.server.run())
    for _ in range(100):
        if pwa.server.started:
            break
        await asyncio.sleep(0.05)
    assert pwa.server.started
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(executable_path=str(chromium_binary()))
        page = await browser.new_page()
        page.set_default_timeout(8_000)
        found = Rig(pwa, page)

        async def answer(route: Route) -> None:
            body = json.loads(route.request.post_data or "{}")
            found.submits.append(body["inputs"])
            await route.fulfill(json={"outcome": "done"})

        await page.route("**/api/card", answer)
        page.on("pageerror", lambda error: found.problems.append(str(error)))
        page.on(
            "console",
            lambda message: found.problems.append(message.text) if message.type == "error" else None,
        )
        await page.goto(f"http://127.0.0.1:{port}/")
        await page.fill("#token", TOKEN)
        await page.click("#login-form button")
        await page.wait_for_selector("#status.on")
        yield found
        await browser.close()
    pwa.server.stop()
    await asyncio.wait_for(task, 5)
    await router.stop()
    csp = [p for p in found.problems if "Content Security Policy" in p or "Refused" in p]
    assert found.problems == [] and csp == []


async def test_new_session_sends_the_nonce_and_every_input(rig: Rig) -> None:
    agents = [
        ("opencode:default", "🟩 OpenCode · Default"),
        ("opencode:m60", "🟩 OpenCode · Model 60"),
        ("cursor_cli:default", "⬛ Cursor CLI · Default"),
    ]
    box = await rig.show(
        cards.new_session(
            [("proj", "proj"), ("other", "other")], "proj", agents, "opencode:default", None, nonce="n1"
        )
    )
    await box.get_by_label("Prompt").fill("hello")
    await box.get_by_label("Agent").select_option("opencode:m60")
    await box.get_by_label("Workspace").select_option("other")
    await box.get_by_role("button", name="Start").click()
    await rig.page.wait_for_function("() => document.querySelector('.card button:disabled')")
    assert rig.submits == [
        {
            "relay": "new",
            "nonce": "n1",
            "ws": "other",
            "agent": "opencode:m60",
            "prompt": "hello",
        }
    ]


async def test_new_session_shows_its_notes_and_the_prefilled_prompt(rig: Rig) -> None:
    box = await rig.show(
        cards.new_session(
            [("proj", "proj")],
            "proj",
            [("opencode:default", "🟩 OpenCode · Default")],
            "opencode:default",
            "run the tests",
            nonce="n2",
            unavailable=["Cursor CLI (down)"],
            files=2,
        )
    )
    assert await box.get_by_label("Prompt").input_value() == "run the tests"
    assert await box.get_by_text("Not available: Cursor CLI (down)").count() == 1
    assert await box.get_by_text("📎 2 files will be attached").count() == 1


async def test_queue_removes_one_item_and_sends_the_mode_in_scope(rig: Rig) -> None:
    nonces = {"mode": "m", "clear": "c", "remove:1": "r1", "remove:2": "r2"}
    box = await rig.show(queue_card("s1", "queue", [(1, "first"), (2, "second")], nonces=nonces))
    assert await box.locator(".columns").count() == 2
    assert await box.locator(".column.auto").count() == 2
    await box.locator("select").select_option("steer")
    await box.get_by_role("button", name="Remove").nth(1).click()
    await rig.page.wait_for_function("() => document.querySelector('.card button:disabled')")
    assert rig.submits == [
        {"relay": "queue", "op": "remove", "session_key": "s1", "nonce": "r2", "id": 2, "mode": "steer"}
    ]


async def test_a_question_sends_the_chosen_option_and_the_typed_answer_with_its_nonce(rig: Rig) -> None:
    box = await rig.show(cards.question("q1", "proj", "Which database?", ("pg", "sqlite"), nonce="nq"))
    await box.get_by_label("sqlite").check()
    await box.locator("textarea").fill("or neither")
    await box.get_by_role("button", name="Answer").click()
    await rig.page.wait_for_function("() => document.querySelector('.card button:disabled')")
    assert rig.submits == [
        {
            "relay": "question",
            "request_id": "q1",
            "nonce": "nq",
            "op": "answer",
            "choice": "sqlite",
            "custom": "or neither",
        }
    ]


GENERIC: Card = {
    "type": "AdaptiveCard",
    "version": "1.3",
    "body": [
        {"type": "TextBlock", "text": "<b>not html</b>", "weight": "Bolder", "wrap": False},
        {
            "type": "Container",
            "items": [
                {
                    "type": "FactSet",
                    "facts": [{"title": "Host", "value": "h1"}, {"title": "Disk", "value": "90%"}],
                },
                {"type": "Input.Text", "id": "note", "label": "Note", "value": "hi", "maxLength": 5},
                {
                    "type": "Input.ChoiceSet",
                    "id": "pick",
                    "style": "expanded",
                    "value": "b",
                    "choices": [
                        {"title": "A", "value": "a"},
                        {"title": "B", "value": "b"},
                        {"title": "C", "value": "c"},
                    ],
                },
                {
                    "type": "Input.ChoiceSet",
                    "id": "many",
                    "isMultiSelect": True,
                    "value": "x,z",
                    "choices": [
                        {"title": "X", "value": "x"},
                        {"title": "Y", "value": "y"},
                        {"title": "Z", "value": "z"},
                    ],
                },
                {
                    "type": "Input.ChoiceSet",
                    "id": "unset",
                    "style": "compact",
                    "choices": [{"title": "One", "value": "1"}],
                },
                {
                    "type": "Input.Toggle",
                    "id": "ok",
                    "title": "Agree",
                    "value": "no",
                    "valueOn": "yes",
                    "valueOff": "no",
                },
            ],
        },
        {"type": "TextBlock", "id": "extra", "text": "extra detail", "isVisible": False},
        {"type": "Image", "url": "https://example.invalid/x.png"},
        {
            "type": "ActionSet",
            "actions": [
                {"type": "Action.ToggleVisibility", "title": "More", "targetElements": ["extra"]},
                {"type": "Action.OpenUrl", "title": "Open", "url": "https://example.invalid"},
            ],
        },
    ],
    "actions": [{"type": "Action.Submit", "title": "Go", "data": {"relay": "t", "nonce": "g1"}}],
}


async def test_generic_elements_inputs_and_unknowns(rig: Rig) -> None:
    box = await rig.show(GENERIC)
    assert await box.get_by_text("<b>not html</b>").is_visible()  # text, never markup
    assert await box.locator("b").count() == 0
    facts = await box.locator(".facts").inner_text()
    assert "Host" in facts and "h1" in facts and "90%" in facts
    assert await box.locator(".nowrap").count() == 1
    assert await box.get_by_text("Unsupported element: Image").is_visible()
    assert await box.get_by_text("Unsupported action: Action.OpenUrl").is_visible()
    assert await box.get_by_text("extra detail").is_hidden()
    await box.get_by_role("button", name="More").click()
    assert await box.get_by_text("extra detail").is_visible()
    await box.get_by_label("Note").fill("123456789")
    assert await box.get_by_label("Note").input_value() == "12345"  # maxLength applies
    await box.get_by_label("C", exact=True).check()
    await box.get_by_label("X", exact=True).uncheck()
    await box.get_by_label("Y", exact=True).check()
    await box.get_by_label("Agree").check()
    await box.get_by_role("button", name="Go").click()
    await rig.page.wait_for_function("() => document.querySelector('.card button:disabled')")
    assert rig.submits == [
        {
            "relay": "t",
            "nonce": "g1",
            "note": "12345",
            "pick": "c",
            "many": "y,z",
            "unset": "",
            "ok": "yes",
        }
    ]


async def test_malformed_cards_draw_notes_and_never_throw(rig: Rig) -> None:
    broken: Card = {
        "type": "AdaptiveCard",
        "body": [None, "text", 7, {"type": "ColumnSet", "columns": "x"}],
        "actions": "x",
    }
    box = await rig.show(broken)
    assert await box.locator(".note").count() >= 3
    empty = await rig.show({"type": "AdaptiveCard"})
    assert await empty.locator("button").count() == 0
    plain = await rig.show({"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": "still fine"}]})
    assert await plain.get_by_text("still fine").is_visible()


async def test_the_webex_cards_draw_with_a_button_per_action(rig: Rig) -> None:
    built: list[Card] = [
        cards.panic_confirm(nonce="p"),
        cards.model_switch("s1", "m1", [("m1", "M1"), ("m2", "M2")], "m1", nonce="m"),
        cards.question("r", "proj", "Which?", ("a", "b"), nonce="z"),
        queue_card("s", "steer", [(1, "x")], nonces={"mode": "m", "clear": "c", "remove:1": "r"}),
    ]
    for card in built:
        box = await rig.show(card)
        assert await box.locator(".note").count() == 0
        for action in actions_of(card):
            title = str(action["title"])
            assert await box.locator("button").filter(has_text=title).count() >= 1, title
