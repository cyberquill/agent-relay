"""Drives Cursor's composer through Playwright and turns DOM changes into reply text."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from agent_relay.adapters.base import AdapterUnavailableError
from agent_relay.adapters.cursor_cdp.selectors import SelectorSet

BINDING = "__relaySnapshot"
MENU_TIMEOUT_MS = 3_000
NEW_CHAT_WAIT_MS = 5_000
SELFTEST_HINT = "run `relay selftest cursor-cdp`"

OBSERVER_JS = """
([assistantCss, stopCss, binding]) => {
  if (window.__relayObserver) { window.__relayObserver.disconnect(); }
  let timer = null;
  const report = () => {
    timer = null;
    const replies = document.querySelectorAll(assistantCss);
    const last = replies.length ? replies[replies.length - 1] : null;
    window[binding]({
      count: replies.length,
      text: last ? (last.innerText || last.textContent || "") : "",
      busy: document.querySelector(stopCss) !== null,
      lastId: last ? (last.getAttribute("data-message-id") || "") : "",
      ids: Array.from(replies, (el) => el.getAttribute("data-message-id") || ""),
    });
  };
  const schedule = () => { if (timer === null) { timer = setTimeout(report, 50); } };
  const observer = new MutationObserver(schedule);
  observer.observe(document.body, {childList: true, subtree: true, characterData: true, attributes: true});
  window.__relayObserver = observer;
  report();
}
"""

SNAPSHOT_JS = """
([assistantCss, stopCss]) => {
  const replies = document.querySelectorAll(assistantCss);
  const last = replies.length ? replies[replies.length - 1] : null;
  return {
    count: replies.length,
    text: last ? (last.innerText || last.textContent || "") : "",
    busy: document.querySelector(stopCss) !== null,
    lastId: last ? (last.getAttribute("data-message-id") || "") : "",
    ids: Array.from(replies, (el) => el.getAttribute("data-message-id") || ""),
  };
}
"""


NOT_MODELS = frozenset({"more models"})


def option_label(text: str) -> str:
    """A model option's name: its first line (Cursor 3.31 adds an "Edit" action on the hovered row)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else ""


async def press(target: Locator) -> None:
    """A DOM `click()`: unlike Playwright's pointer click it needs no rendered frame, so it also works
    when the Cursor window is hidden, occluded or the screen is locked."""
    await target.evaluate("el => el.click()")


@dataclass(frozen=True)
class Snapshot:
    """The reply area at one moment. Cursor keeps only the rows near the viewport in the page, so older
    replies disappear as new ones appear: a reply is new by its message id, and by the count only when
    the page gives no ids."""

    count: int
    text: str
    busy: bool
    last_id: str = ""
    ids: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, raw: object) -> Snapshot:
        if not isinstance(raw, dict):
            return cls(0, "", False)
        count = raw.get("count")
        text = raw.get("text")
        last_id = raw.get("lastId")
        ids = raw.get("ids")
        return cls(
            count if isinstance(count, int) else 0,
            text if isinstance(text, str) else "",
            bool(raw.get("busy")),
            last_id if isinstance(last_id, str) else "",
            frozenset(i for i in ids if isinstance(i, str) and i) if isinstance(ids, list) else frozenset(),
        )


class TurnTracker:
    """Follows one turn: the reply is the last assistant message that was not there at `baseline`."""

    def __init__(self, baseline: Snapshot) -> None:
        self._base_count = baseline.count
        self._base_ids = baseline.ids
        self._text = ""
        self._seen_busy = False
        self.done = False

    @property
    def text(self) -> str:
        return self._text

    def _fresh(self, snap: Snapshot) -> bool:
        if snap.last_id:
            return snap.last_id not in self._base_ids
        return snap.count > self._base_count

    def began(self, snap: Snapshot) -> bool:
        """Whether Cursor acted on the send: a stop button was seen, or a new assistant message exists."""
        return self._seen_busy or snap.busy or self._fresh(snap)

    def feed(self, snap: Snapshot) -> str:
        """Returns the new reply text since the last snapshot; sets `done` when the turn ends."""
        if self.done:
            return ""
        text = snap.text if self._fresh(snap) else ""
        delta = text[len(self._text) :] if text.startswith(self._text) else ""
        self._text = text
        if snap.busy:
            self._seen_busy = True
        elif self._seen_busy or text.strip():
            self.done = True
        return delta


class Composer:
    def __init__(self, page: Page, selectors: SelectorSet) -> None:
        self.page = page
        self._selectors = selectors
        self._bound = False

    def locator(self, name: str) -> Locator:
        return self.page.locator(self._selectors.css(name))

    async def count(self, name: str) -> int:
        return await self.locator(name).count()

    async def _one(self, name: str) -> Locator:
        if await self.count(name) == 0:
            raise AdapterUnavailableError(f"Cursor's composer has no '{name}' element; {SELFTEST_HINT}")
        return self.locator(name).first

    async def new_chat(self) -> None:
        """Opens a fresh chat and returns once the previous chat's replies are gone from the page.

        Cursor swaps the chat a moment after the click. A baseline taken before that still counts the old
        chat's replies, so the new reply would never look new and the phone would get an empty answer."""
        await press(await self._one("new_chat"))
        try:
            await self.page.wait_for_function(
                "css => document.querySelectorAll(css).length === 0",
                arg=self._selectors.css("assistant_message"),
                timeout=NEW_CHAT_WAIT_MS,
                polling=100,  # not "raf": a hidden or occluded window never fires animation frames
            )
        except PlaywrightError:
            raise AdapterUnavailableError(
                f"Cursor's new chat still shows the previous replies; {SELFTEST_HINT}"
            ) from None

    async def _open_menu(self) -> list[Locator]:
        await press(await self._one("model_picker"))
        options = self.locator("model_option")
        try:
            await options.first.wait_for(state="visible", timeout=MENU_TIMEOUT_MS)
        except PlaywrightError:
            await self.page.keyboard.press("Escape")
            raise AdapterUnavailableError(f"Cursor's model menu did not open; {SELFTEST_HINT}") from None
        return await options.all()

    async def models(self) -> list[str]:
        labels = [option_label(await option.inner_text()) for option in await self._open_menu()]
        await self.page.keyboard.press("Escape")
        return [label for label in labels if label and label.casefold() not in NOT_MODELS]

    async def choose_model(self, label: str) -> None:
        wanted = label.strip().casefold()
        for option in await self._open_menu():
            if option_label(await option.inner_text()).casefold() == wanted:
                await press(option)
                return
        await self.page.keyboard.press("Escape")
        raise AdapterUnavailableError(f"Cursor does not offer the model {label}")

    async def submit(self, prompt: str) -> None:
        await (await self._one("composer")).evaluate("el => el.focus()")
        await self.page.keyboard.insert_text(prompt)
        await press(await self._one("send"))

    async def clear(self) -> None:
        """Empties the composer so text from a send that did nothing is not prefixed to the next prompt."""
        await (await self._one("composer")).evaluate(
            "el => { el.focus(); document.execCommand('selectAll'); document.execCommand('delete'); }"
        )

    async def stop(self) -> bool:
        if await self.count("stop") == 0:
            return False
        await press(self.locator("stop").first)
        return True

    async def snapshot(self) -> Snapshot:
        raw = await self.page.evaluate(
            SNAPSHOT_JS, [self._selectors.css("assistant_message"), self._selectors.css("stop")]
        )
        return Snapshot.parse(raw)

    async def observe(self, on_snapshot: Callable[[Snapshot], None]) -> None:
        """Calls `on_snapshot` (on the event loop) whenever the reply area or the stop button changes."""
        if not self._bound:

            def _binding(source: object, raw: object) -> None:
                on_snapshot(Snapshot.parse(raw))

            await self.page.expose_binding(BINDING, _binding)
            self._bound = True
        await self.page.evaluate(
            OBSERVER_JS, [self._selectors.css("assistant_message"), self._selectors.css("stop"), BINDING]
        )
