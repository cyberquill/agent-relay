"""Every card builder, with maximal inputs, stays inside the Adaptive Card subset Webex renders."""

from __future__ import annotations

import copy
import pkgutil

import pytest
from pydantic import JsonValue

from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.cards.common import CARD_LIMIT_BYTES, card, text
from tests.frontends.card_samples import NONCE, maximal_cards
from tests.frontends.card_spec import check_card

SAMPLES = maximal_cards()


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_card_builder_passes_the_webex_subset_with_maximal_inputs(name: str) -> None:
    assert check_card(SAMPLES[name]) == []


# Modules of the cards package that build no card of their own.
NO_BUILDERS = {"common", "root"}
COVERED = {"new_session", "model_switch", "dashboard", "panic_confirm", "question", "queue", "ws"}


def test_every_card_module_has_samples_here() -> None:
    modules = {m.name for m in pkgutil.iter_modules(cards.__path__)} - NO_BUILDERS
    assert modules == COVERED, "a card module was added or removed: update card_samples.py and COVERED"


def _first(card_: dict[str, JsonValue], kind: str) -> dict[str, JsonValue]:
    body = card_["body"]
    assert isinstance(body, list)
    return next(b for b in body if isinstance(b, dict) and b.get("type") == kind)


def test_a_valid_card_and_the_smallest_one_pass() -> None:
    minimal = card([text("hi")], [{"type": "Action.Submit", "title": "Go", "data": {"relay": "x"}}])
    assert check_card(minimal) == []


def test_action_toggle_visibility_is_refused() -> None:
    bad = card([text("hi")], [{"type": "Action.ToggleVisibility", "title": "Show", "targetElements": ["a"]}])
    assert any("Action.ToggleVisibility" in p for p in check_card(bad))


def test_a_newer_card_version_is_refused() -> None:
    bad = copy.deepcopy(SAMPLES["panic_confirm"])
    bad["version"] = "1.4"
    assert any("above 1.3" in p for p in check_card(bad))


def test_an_unknown_element_and_an_unknown_action_are_refused() -> None:
    bad = card([{"type": "Table", "rows": []}], [{"type": "Action.Execute", "title": "Run"}])
    problems = check_card(bad)
    assert any("'Table'" in p for p in problems) and any("Action.Execute" in p for p in problems)


def test_duplicate_input_ids_are_refused_across_nested_cards() -> None:
    field: JsonValue = {"type": "Input.Text", "id": "same"}
    nested: dict[str, JsonValue] = card([field], [])
    bad = card([field], [{"type": "Action.ShowCard", "title": "More", "card": nested}])
    assert any("duplicate input id 'same'" in p for p in check_card(bad))


def test_a_choice_set_needs_choices_and_a_preset_among_them() -> None:
    empty = card([{"type": "Input.ChoiceSet", "id": "c", "choices": []}], [])
    assert any("no choices" in p for p in check_card(empty))
    off = card(
        [{"type": "Input.ChoiceSet", "id": "c", "value": "z", "choices": [{"title": "A", "value": "a"}]}], []
    )
    assert any("preset" in p for p in check_card(off))


def test_an_oversized_card_is_refused() -> None:
    blocks: list[JsonValue] = [text("y" * 2_000) for _ in range(CARD_LIMIT_BYTES // 2_000 + 2)]
    assert any("Webex allows" in p for p in check_card(card(blocks, [])))


def test_a_card_without_a_schema_is_refused() -> None:
    bad = copy.deepcopy(SAMPLES["panic_confirm"])
    del bad["$schema"]
    assert any("$schema" in p for p in check_card(bad))


def test_the_new_card_uses_no_show_card_and_stays_small() -> None:
    sample = SAMPLES["new_session"]
    assert "Action.ShowCard" not in str(sample) and "ToggleVisibility" not in str(sample)
    assert cards.message_bytes("New session", sample) < 12_000
    agent = _first_choice(sample, "agent")
    assert len(agent) == cards.AGENT_LIMIT


def _first_choice(sample: dict[str, JsonValue], field: str) -> list[JsonValue]:
    body = sample["body"]
    assert isinstance(body, list)
    for block in body:
        if isinstance(block, dict) and block.get("id") == field:
            choices = block["choices"]
            assert isinstance(choices, list)
            return choices
    raise AssertionError(field)


def test_the_nonce_every_sample_carries_is_present() -> None:
    for name, sample in SAMPLES.items():
        assert NONCE in str(sample), name
