"""Table-driven unlock lists. No network.

Fixtures are the real shape of the shipbuilding tables, colspan expansion and
all, because every bug found while writing this came from that shape rather
than from the filtering: a duplicated "Tier" header whose first column is an
empty icon cell, a Construction level column sitting next to the Sailing one,
and the same tier repeated once per ship class.
"""

from __future__ import annotations

from reldo.unlocks import Unlock, dedupe, from_table, render

HELM = [
    ["Tier", "Tier", "Sailing level", "Construction level", "Rapid resistance"],
    ["", "Bronze", "1", "1", "0"],
    ["", "Iron", "17", "14", "1 (Gentle)"],
    ["", "Steel", "38", "30", "1 (Gentle)"],
]


def test_filters_to_the_level_asked_for():
    got = from_table(HELM, "Sailing", 20, "Helm")
    assert [(u.name, u.level) for u in got] == [("Bronze", 1), ("Iron", 17)]


def test_the_boundary_level_is_included():
    assert [u.name for u in from_table(HELM, "Sailing", 17, "Helm")] == ["Bronze", "Iron"]
    assert [u.name for u in from_table(HELM, "Sailing", 16, "Helm")] == ["Bronze"]


def test_the_wrong_skill_column_is_not_used():
    """The killer bug this guards: these tables carry a Construction level too,
    and reading it answers a different question fluently. Iron helm is Sailing
    17 but Construction 14, so a Construction-14 filter must not return it."""
    got = from_table(HELM, "Construction", 14, "Helm")
    assert [(u.name, u.level) for u in got] == [("Bronze", 1), ("Iron", 14)]


def test_a_table_without_the_skill_column_yields_nothing():
    other = [["Tier", "Mining level"], ["Rune", "85"]]
    assert from_table(other, "Sailing", 99, "x") == []


def test_the_empty_icon_column_is_not_mistaken_for_the_name():
    """colspan expansion duplicates "Tier"; the first is the image cell and is
    empty in every row. Trusting the header alone named everything "" and the
    whole table filtered out with no error anywhere."""
    assert [u.name for u in from_table(HELM, "Sailing", 20, "Helm")] == ["Bronze", "Iron"]


def test_non_numeric_levels_are_skipped_not_crashed_on():
    ragged = [
        ["Tier", "Sailing level"],
        ["Sub-heading row", ""],
        ["Special", "N/A"],
        ["Wooden", "1"],
    ]
    assert [u.name for u in from_table(ragged, "Sailing", 20, "x")] == ["Wooden"]


def test_identical_rows_under_two_headings_collapse():
    """The overview section repeats the per-component tables verbatim."""
    overview = Unlock("Oak", 20, "Shipbuilding - Core boat parts", {"HP": "30"})
    specific = Unlock("Oak", 20, "Shipbuilding - Hull", {"HP": "30"})
    got = dedupe([overview, specific])
    assert len(got) == 1
    assert got[0].source == "Shipbuilding - Hull", "the specific heading should win"


def test_the_same_tier_per_ship_class_collapses_to_one_unlock():
    """The hull table has a variant per class, so Wooden at level 1 appears
    three times with different HP. That is one thing you can build."""
    got = dedupe([
        Unlock("Wooden", 1, "Shipbuilding - Hull", {"HP": "20"}),
        Unlock("Wooden", 1, "Shipbuilding - Hull", {"HP": "30"}),
        Unlock("Wooden", 1, "Shipbuilding - Hull", {"HP": "40"}),
    ])
    assert len(got) == 1


def test_the_same_name_in_different_categories_stays_separate():
    """A bronze keel and a bronze helm are both "Bronze" at level 1."""
    got = dedupe([
        Unlock("Bronze", 1, "Shipbuilding - Helm", {"Rapid resistance": "0"}),
        Unlock("Bronze", 1, "Shipbuilding - Keel", {"Armour": "100"}),
    ])
    assert len(got) == 2


def test_render_groups_by_source_and_sorts_by_level():
    out = render(dedupe(from_table(HELM, "Sailing", 20, "Helm")), "Sailing", 20)
    assert "Sailing level 20 or below (2 total)" in out
    assert out.index("Bronze") < out.index("Iron")


def test_render_says_so_when_nothing_matched():
    assert "Nothing found" in render([], "Sailing", 5)


# -- rows that state no level ------------------------------------------------

GARDEN = [
    ["Thieving Level", "Season", "Sq'irk fruit", "# of sq'irks"],
    ["N/A", "Winter", "Winter sq'irk", "5"],
    ["25", "Spring", "Spring sq'irk", "4"],
    ["45", "Autumn", "Autumn sq'irk", "3"],
    ["65", "Summer", "Summer sq'irk", "2"],
]


def test_a_row_stating_no_level_is_named_rather_than_dropped():
    """from_table drops it, which is right for "what can I do at 25" -- a row
    with no number is not an unlock at any level. It is wrong for "what level
    do I need", where a row stating none is the answer that you need none, and
    the Sorceress's Garden reported without Winter reads as a minimum of 25."""
    from reldo.unlocks import unlevelled

    assert unlevelled(GARDEN, "Thieving") == ["Winter"]


def test_the_levelled_rows_still_come_back_from_the_other_call():
    from reldo.unlocks import from_table

    rows = from_table(GARDEN, "Thieving", 99, "Sorceress's Garden")
    assert [(r.name, r.level) for r in rows] == [
        ("Spring", 25), ("Autumn", 45), ("Summer", 65)
    ]


def test_a_blank_cell_is_a_spacer_and_not_a_statement():
    table = [
        ["Thieving Level", "Season"],
        ["", "Section header"],
        ["25", "Spring"],
    ]
    from reldo.unlocks import unlevelled

    assert unlevelled(table, "Thieving") == []


def test_unlevelled_needs_the_skill_column_too():
    """Named apart from the from_table test above it, which asserts the same
    property of the other call -- one shadowed the other and pytest ran only
    the second."""
    from reldo.unlocks import unlevelled

    assert unlevelled(GARDEN, "Agility") == []
