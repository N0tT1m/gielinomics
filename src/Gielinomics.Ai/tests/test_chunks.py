"""Wikitext chunking tests. Pure functions, no network.

Chunking is where silent data loss hides: a cleaner that eats too much produces
an index full of empty strings, and a splitter that drops the tail loses the back
half of every long guide -- exactly the failure the 12k truncation cap already
caused once.
"""

from __future__ import annotations

import pytest

from reldo.chunks import (
    CHUNK_CHARS,
    Chunk,
    chunk_article,
    chunk_text,
    split_sections,
    strip_wikitext,
)

# -- markup cleaning -------------------------------------------------------


def test_piped_and_plain_links_keep_their_label():
    assert strip_wikitext("Use [[Tick manipulation|3-tick]] on [[granite]].") == (
        "Use 3-tick on granite."
    )


def test_templates_are_removed_including_nested_ones():
    """Infoboxes and navboxes are most of a page's bulk and none of its prose."""
    assert strip_wikitext("Before {{Infobox|a={{Nested|x}}|b=y}} after") == "Before after"


def test_unbalanced_template_does_not_eat_the_rest_of_the_page():
    out = strip_wikitext("Start {{unclosed and then real prose continues here")
    assert out.startswith("Start")


def test_refs_comments_files_and_tables_are_dropped():
    raw = (
        "Real prose.<ref>citation</ref><!-- hidden -->"
        "[[File:Thing.png|thumb|caption]]\n{| class=wikitable\n| cell |}\nMore prose."
    )
    out = strip_wikitext(raw)
    assert "citation" not in out and "hidden" not in out
    assert "Thing.png" not in out and "wikitable" not in out
    assert "Real prose." in out and "More prose." in out


def test_bold_italics_and_list_markers_are_flattened():
    assert strip_wikitext("*** '''Bold''' and ''italic''") == "Bold and italic"


def test_cleaning_is_not_so_aggressive_it_empties_a_normal_paragraph():
    raw = (
        "The [[abyssal tentacle]] is a {{Coins|1000000}} upgrade to the "
        "[[abyssal whip]], requiring 75 [[Attack]]."
    )
    out = strip_wikitext(raw)
    assert "abyssal tentacle" in out and "upgrade" in out and "75 Attack" in out


# -- section splitting -----------------------------------------------------


def test_lead_is_returned_as_the_empty_section():
    sections = split_sections("Lead prose.\n==Combat==\nfight stuff")
    assert sections[0][0] == ""
    assert "Lead prose." in sections[0][1]
    assert sections[1][0] == "Combat"


def test_page_without_headings_is_one_lead_section():
    assert split_sections("just prose") == [("", "just prose")]


def test_subsections_are_split_too_and_nothing_is_lost():
    raw = "lead\n==A==\nalpha\n===A1===\nsub\n==B==\nbeta"
    names = [n for n, _ in split_sections(raw)]
    assert names == ["", "A", "A1", "B"]
    joined = "".join(body for _, body in split_sections(raw))
    for content in ("lead", "alpha", "sub", "beta"):
        assert content in joined


# -- chunking --------------------------------------------------------------


def test_short_text_is_a_single_chunk():
    assert chunk_text("short") == ["short"]


def test_empty_text_yields_nothing():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_long_text_is_split_and_nothing_is_dropped():
    text = ". ".join(f"sentence number {i}" for i in range(400))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    # The tail must survive -- losing it is the bug the 12k cap already caused.
    assert "sentence number 399" in chunks[-1]


def test_chunks_respect_the_size_target():
    text = "word " * 3000
    for piece in chunk_text(text, size=500, overlap=50):
        assert len(piece) <= 600  # size plus a little slack for boundary seeking


def test_chunks_overlap_so_a_split_fact_survives_somewhere():
    text = "A" * 400 + "\n" + "B" * 400 + "\n" + "C" * 400
    chunks = chunk_text(text, size=500, overlap=100)
    assert len(chunks) >= 2
    assert sum(len(c) for c in chunks) > len(text) - 50  # coverage, allowing strip()


# -- whole articles --------------------------------------------------------


def test_chunk_article_labels_each_piece_with_its_section():
    raw = "Lead prose that is definitely long enough to survive the minimum.\n" \
          "==Strategy==\n" + ("It heals when you use the wrong attack style. " * 5)
    chunks = chunk_article("Vorkath", raw)
    assert {c.section for c in chunks} == {"", "Strategy"}
    assert all(c.title == "Vorkath" for c in chunks)


def test_navigational_sections_are_skipped():
    raw = "Lead prose long enough to be kept as a chunk of its own here.\n" \
          "==Trivia==\n" + ("Pointless fact. " * 20) + "\n==Gallery==\n" + ("img " * 40)
    assert {c.section for c in chunk_article("X", raw)} == {""}


def test_tiny_non_lead_sections_are_dropped():
    raw = "Lead prose long enough to be kept as a chunk of its own here.\n==Stub==\nx"
    assert {c.section for c in chunk_article("X", raw)} == {""}


def test_a_short_lead_is_kept_anyway():
    """min_chars must never make an article unfindable -- the lead index had
    one vector for every page, and losing that is a silent coverage regression."""
    chunks = chunk_article("Tiny page", "Short lead.")
    assert len(chunks) == 1
    assert chunks[0].section == ""
    assert "Short lead." in chunks[0].text


def test_an_all_infobox_article_still_gets_a_title_chunk():
    """~7% of the corpus is template-only. Those pages must stay searchable."""
    chunks = chunk_article("Infobox only", "{{Infobox item|name=x|value=1}}")
    assert len(chunks) == 1
    assert chunks[0].document() == "Infobox only. "


def test_document_prepends_title_and_section():
    """A Strategies passage says 'it heals when...' without naming the monster."""
    doc = Chunk("Vorkath", "Strategy", "It heals when you attack wrong.").document()
    assert doc.startswith("Vorkath - Strategy.")
    assert "It heals" in doc


def test_document_omits_the_separator_for_lead_chunks():
    assert Chunk("Vorkath", "", "A dragon.").document() == "Vorkath. A dragon."


def test_a_realistic_guide_produces_several_labelled_chunks():
    raw = (
        "Mining is a skill.\n"
        "==Levels 1-15: Copper==\n" + ("Mine copper ore at the mine. " * 40) + "\n"
        "==Levels 45-99: Granite==\n" + ("3-tick granite at the quarry. " * 40)
    )
    chunks = chunk_article("Mining training", raw)
    sections = {c.section for c in chunks}
    assert "Levels 45-99: Granite" in sections
    assert any("granite" in c.text.lower() for c in chunks)


@pytest.mark.parametrize("size", [200, CHUNK_CHARS])
def test_chunking_terminates_on_pathological_input(size):
    """No paragraph breaks, no sentence breaks -- must not loop forever."""
    assert len(chunk_text("x" * 5000, size=size, overlap=size // 2)) > 1
