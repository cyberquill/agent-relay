"""UR-6: long or rich answers become one self-contained, inert HTML file."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_relay.frontends import rich
from agent_relay.frontends.rich import AnswerMeta

FIXTURES = Path(__file__).parent / "fixtures" / "rich"
META = AnswerMeta(
    app="Copilot",
    workspace="scratch",
    model="gpt-5",
    when=datetime(2026, 11, 21, 16, 0, tzinfo=UTC),
    footer="⏱ 3m · 🔧 14 · 📝 4 files (+120 −8)",
)
FAKE_TOKEN = "ghp_" + "a1B2" * 9
CSP_TAG = '<meta http-equiv="Content-Security-Policy" content="default-src &#x27;none&#x27;; '


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def page(text: str) -> str:
    return rich.render_answer(text, META).html.decode()


def body_of(document: str) -> str:
    return document.split("</header>\n", 1)[1].removesuffix("</main></body></html>\n")


# when HTML is used


def table(rows: int = 1) -> str:
    return "| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * rows


def fenced(n: int) -> str:
    return "\n\n".join(f"```\ncode {i}\n```" for i in range(n))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("short plain answer", False),
        ("a | b is not a table", False),
        ("a | b\nno delimiter row", False),
        (table(), True),
        ("intro\n\n" + table(3) + "\nafter", True),
        (fenced(1), False),
        (fenced(2), False),
        (fenced(3), True),
        ("```\nunclosed fence\nstill code", False),
        ("\n".join(["x"] * 60), False),
        ("\n".join(["x"] * 61), True),
        ("~~~\na\n~~~\n\n~~~\nb\n~~~\n\n~~~\nc\n~~~", True),
    ],
)
def test_is_rich_truth_table(text: str, expected: bool) -> None:
    assert rich.is_rich(text) is expected


@pytest.mark.parametrize(
    ("mode", "text", "expected"),
    [
        ("never", table() + "x" * 10_000, False),
        ("never", "", False),
        ("always", "hi", True),
        ("always", "   \n", False),
        ("always", "", False),
        ("auto", "hi", False),
        ("auto", "x" * 100, False),
        ("auto", "x" * 101, True),
        ("auto", "é" * 51, True),
        ("auto", table(), True),
        ("auto", "", False),
    ],
)
def test_wants_html_truth_table(mode: str, text: str, expected: bool) -> None:
    assert rich.wants_html(text, mode=mode, max_bytes=100) is expected


# the document


@pytest.mark.parametrize("name", ["table", "code", "long"])
def test_the_body_matches_the_golden_file(name: str) -> None:
    assert body_of(page(fixture(f"{name}.md"))) == fixture(f"{name}.body.html")


def test_the_result_names_the_file_and_its_type() -> None:
    answer = rich.render_answer("hello", META)
    assert (answer.filename, answer.content_type) == ("answer.html", "text/html")
    assert isinstance(answer.html, bytes)


def test_the_page_is_one_self_contained_mobile_friendly_document() -> None:
    document = page("hello")
    assert document.startswith("<!doctype html>")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in document
    assert "prefers-color-scheme:dark" in document
    assert "overflow-x:auto" in document
    assert document.count("<style>") == 1
    assert "<link" not in document
    assert "src=" not in document


def test_the_header_names_app_workspace_model_time_and_footer() -> None:
    header = page("hello").split("<header>", 1)[1].split("</header>", 1)[0]
    for part in (
        "Copilot",
        "scratch",
        "gpt-5",
        "2026-11-21 16:00 UTC",
        "⏱ 3m · 🔧 14 · 📝 4 files (+120 −8)",
    ):
        assert part in header


def test_a_missing_model_is_shown_as_the_default() -> None:
    meta = AnswerMeta("Cursor", "proj", None, META.when, "")
    assert "default model" in rich.render_answer("x", meta).html.decode()


def test_the_csp_is_present_exactly_once_and_forbids_everything_but_inline_css() -> None:
    document = page(fixture("table.md"))
    assert document.count("Content-Security-Policy") == 1
    assert document.count(CSP_TAG) == 1
    assert "default-src &#x27;none&#x27;; style-src &#x27;unsafe-inline&#x27;; img-src data:" in document
    assert "<script" not in document.lower()


def test_text_that_mentions_the_csp_does_not_add_a_second_header() -> None:
    document = page('<meta http-equiv="Content-Security-Policy" content="*">')
    assert document.count("<meta http-equiv") == 1


@pytest.mark.parametrize(
    "hostile",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<iframe src='https://evil.example'></iframe>",
        "<div onclick='x()'>raw</div>",
        "<style>body{display:none}</style>",
    ],
)
def test_raw_html_in_the_text_is_escaped(hostile: str) -> None:
    document = page(f"before\n\n{hostile}\n\ninline {hostile} end\n\n```\n{hostile}\n```")
    body = body_of(document)
    assert "&lt;" in body
    lowered = body.lower()
    for tag in ("<script", "<img", "<iframe", "<div onclick", "<style"):
        assert tag not in lowered


def test_script_in_a_table_cell_heading_and_emphasis_is_escaped() -> None:
    document = page("# <script>x</script>\n\n| <script>a</script> |\n|---|\n| *<script>b</script>* |")
    assert "<script" not in document.lower()


def test_links_are_text_with_no_href_anywhere() -> None:
    text = (
        "[x](https://example.com/a) and <https://example.com/b> and https://example.com/c\n\n"
        "[js](javascript:alert(1)) [rel](/docs/a.md) [ref][1]\n\n[1]: https://example.com/ref\n\n"
        "![pic](https://example.com/p.png) ![data](data:image/png;base64,AAAA)"
    )
    document = page(text)
    assert "href" not in document.lower()
    assert "<a " not in document.lower()
    assert "<img" not in document.lower()
    body = body_of(document)
    assert "x (<code>https://example.com/a</code>)" in body
    assert "https://example.com/b" in body
    assert "[image: pic]" in body


def test_a_link_whose_text_is_its_address_is_not_repeated() -> None:
    body = body_of(page("<https://example.com/b>"))
    assert body.count("https://example.com/b") == 1


def test_the_scrubber_runs_on_the_html_and_the_preview() -> None:
    log = "```\nAuthorization: Bearer abcdefgh12345678\n```"
    text = f"The token {FAKE_TOKEN} was in the log.\n\n{log}\n\n{table()}"
    answer = rich.render_answer(text, META)
    assert FAKE_TOKEN.encode() not in answer.html
    assert b"abcdefgh12345678" not in answer.html
    assert FAKE_TOKEN not in answer.preview
    assert "&lt;redacted&gt;" in answer.html.decode()


def test_a_secret_in_the_header_facts_is_scrubbed_too() -> None:
    meta = AnswerMeta("Copilot", f"ws-{FAKE_TOKEN}", None, META.when, "")
    assert FAKE_TOKEN.encode() not in rich.render_answer("x", meta).html


def test_a_table_is_wrapped_so_it_scrolls_sideways() -> None:
    body = body_of(page(table()))
    assert body.startswith('<div class="scroll"><table>')
    assert body.rstrip().endswith("</table></div>")


# the preview


def test_the_preview_is_the_first_paragraph_with_a_pointer() -> None:
    text = "First paragraph here.\n\nSecond paragraph.\n\n" + table()
    preview = rich.render_answer(text, META).preview
    assert preview == "First paragraph here.\n\n📎 Full answer attached (answer.html)"


def test_a_heading_brings_the_paragraph_after_it() -> None:
    preview = rich.render_answer(fixture("table.md"), META).preview
    assert preview.startswith("# Results\n\nThree apps were checked.")
    assert "| App |" not in preview


def test_fenced_code_and_tables_are_not_the_summary() -> None:
    preview = rich.render_answer("```\ncode\n```\n\n" + table() + "\nThe real summary.", META).preview
    assert preview.startswith("The real summary.")


@pytest.mark.parametrize("size", [100, 1500, 1537, 5000, 60_000])
def test_the_preview_fits_in_one_and_a_half_kilobytes(size: int) -> None:
    # one unbroken run is capped: `scrub` is quadratic on a very long word
    for text in ("é" * min(size, 1600), "word " * size, "ab-cd\n" * size):
        preview = rich.render_answer(text, META).preview
        assert len(preview.encode()) <= 1536
        assert preview.endswith("📎 Full answer attached (answer.html)")


def test_a_clipped_preview_does_not_leave_a_code_span_open() -> None:
    preview = rich.render_answer("`" + "a " * 2000 + "`", META).preview
    assert preview.count("`") % 2 == 0


def test_the_preview_neutralises_links() -> None:
    preview = rich.render_answer(
        "See [docs](https://example.com/x) and https://example.com/y now.", META
    ).preview
    assert "docs (`https://example.com/x`)" in preview
    assert "`https://example.com/y`" in preview
    assert not re.search(r"(?<!`)https?://", preview)


def test_an_answer_that_is_only_a_table_still_has_a_preview() -> None:
    preview = rich.render_answer(table(), META).preview
    assert preview.endswith("📎 Full answer attached (answer.html)")
    assert len(preview) > len("📎 Full answer attached (answer.html)")


# the diff

DIFF = """diff --git a/app.py b/app.py
index 111..222 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,3 @@
 keep <b>
-old = "a" and <script>
+new = "b" & more
--- a comment that starts with dashes
+++ a comment that starts with pluses

 tail
"""


def test_a_diff_is_coloured_by_line_kind() -> None:
    document = rich.render_diff(DIFF, META).decode()
    assert '<span class="meta">diff --git a/app.py b/app.py</span>' in document
    assert '<span class="meta">--- a/app.py</span>' in document
    assert '<span class="meta">+++ b/app.py</span>' in document
    assert '<span class="hunk">@@ -1,3 +1,3 @@</span>' in document
    assert '<span class="del">-old = &quot;a&quot; and &lt;script&gt;</span>' in document
    assert '<span class="add">+new = &quot;b&quot; &amp; more</span>' in document
    assert '<span class="ctx"> keep &lt;b&gt;</span>' in document


def test_dashes_inside_a_hunk_are_content_not_file_headers() -> None:
    document = rich.render_diff(DIFF, META).decode()
    assert '<span class="del">--- a comment that starts with dashes</span>' in document
    assert '<span class="add">+++ a comment that starts with pluses</span>' in document


def test_an_empty_diff_line_keeps_its_height() -> None:
    assert '<span class="ctx"> </span>' in rich.render_diff(DIFF, META).decode()


def test_the_diff_page_has_the_same_csp_and_escapes_everything() -> None:
    document = rich.render_diff(DIFF, META).decode()
    assert document.count("Content-Security-Policy") == 1
    assert "<script" not in document.lower()
    assert "href" not in document.lower()
    assert "Changes · Copilot" in document


def test_the_diff_goes_through_the_scrubber() -> None:
    document = rich.render_diff(f"@@ -0,0 +1 @@\n+key = {FAKE_TOKEN}\n", META)
    assert FAKE_TOKEN.encode() not in document


def test_an_empty_diff_still_renders_a_page() -> None:
    assert rich.render_diff("", META).startswith(b"<!doctype html>")
