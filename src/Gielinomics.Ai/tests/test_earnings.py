"""Reading and ranking the money-making guide.

The failure these exist for: asked which skill is best for making money based on
GE prices, the model read the guide and answered "The wiki does not say which
skilling method makes the most money", citing the two pages that do. A second
run named High Level Alchemy, which is not in the top thirty. The page really
does arrive with no figures in it -- they are in a table, and page_text strips
tables -- so the reading and the ranking both happen in code.
"""

from __future__ import annotations

from reldo.earnings import (
    Method,
    best_per_skill,
    methods_from_table,
    rank,
    render,
)

# The live shape, as of writing: six columns, profit comma-formatted, the skill
# carried in Category as "Skilling/<Skill>".
GUIDE = [
    ["Method", "Hourly profit", "Skills", "Category", "Intensity", "Members"],
    ["Pickpocketing elves", "4,340,000", "Thieving 85+ Agility 50",
     "Skilling/Thieving", "High", ""],
    ["Crafting sunfire runes", "3,474,000", "Runecraft 98+", "Skilling/Runecraft", "High", ""],
    ["Pickpocketing master farmers", "230,000", "Thieving 38+", "Skilling/Thieving", "Low", ""],
]


def test_a_guide_table_parses():
    found = methods_from_table(GUIDE)
    assert [m.name for m in found] == [
        "Pickpocketing elves",
        "Crafting sunfire runes",
        "Pickpocketing master farmers",
    ]
    assert found[0].gp_per_hour == 4_340_000


def test_a_navbox_is_not_a_guide_table():
    """The bottom of the page is a table too, full of method names and no
    profit column. Reading it would add three hundred methods worth 0 gp/hr."""
    navbox = [
        ["vteMoney making guides", "vteMoney making guides"],
        ["Collecting", "Air talismans Ashes Bananas"],
    ]
    assert methods_from_table(navbox) == []


def test_a_profit_over_a_stated_time_is_not_an_hourly_rate():
    """The parent page's second table is Profit over Time -- 484,000 per 25
    minutes is not 484,000 an hour, and ranking the two together compares
    numbers that mean different things."""
    other = [
        ["Method", "Profit", "Time", "Skills", "Category", "Members"],
        ["Brewing chef's delight(m)", "484,000", "00:25:00", "Cooking 54", "Cooking", ""],
    ]
    assert methods_from_table(other) == []


def test_an_unrankable_profit_is_dropped_not_guessed():
    table = [
        ["Method", "Hourly profit", "Skills", "Category"],
        ["Something", "varies", "Mining 70", "Skilling/Mining"],
        ["Something else", "", "Mining 70", "Skilling/Mining"],
        ["A real one", "1,000", "Mining 70", "Skilling/Mining"],
    ]
    assert [m.name for m in methods_from_table(table)] == ["A real one"]


def test_the_skill_comes_from_the_wikis_own_filing():
    """Not from the Skills column, which lists everything a method benefits
    from: 'Thieving 85+ Agility 50' is one Thieving method, and taking the
    first name there is a coin toss."""
    (elves, *_) = methods_from_table(GUIDE)
    assert elves.skill == "Thieving"


def test_the_skills_column_is_the_fallback():
    """The parent page files some skilling methods by content rather than by
    skill, so Category is not always 'Skilling/<Skill>'."""
    method = Method(
        name="Brewing", gp_per_hour=1, skills="Cooking 54", category="Cooking (Brewing)"
    )
    assert method.skill == "Cooking"


def test_a_combat_method_has_no_skill():
    method = Method(name="Killing Yama", gp_per_hour=1, skills="", category="Combat/High")
    assert method.skill == ""


def test_best_per_skill_keeps_the_richest_of_each():
    """Which is what 'what skill is best' asks. A flat top-twenty is nine
    Thieving rows and reads as though nothing else earns."""
    best = best_per_skill(methods_from_table(GUIDE))
    assert [(m.skill, m.name) for m in best] == [
        ("Thieving", "Pickpocketing elves"),
        ("Runecraft", "Crafting sunfire runes"),
    ]


def test_rank_is_richest_first():
    assert [m.gp_per_hour for m in rank(methods_from_table(GUIDE))] == [
        4_340_000,
        3_474_000,
        230_000,
    ]


def test_render_leads_with_the_conclusion():
    """Handed a table alone the model picks a row it recognises -- it named
    High Level Alchemy. Naming the winner leaves it nothing to decide."""
    text = render(best_per_skill(methods_from_table(GUIDE)))
    assert text.startswith("ANSWER: Thieving is the best")
    assert "4,340,000 gp/hour" in text
    assert "live Grand Exchange prices" in text


def test_render_survives_an_empty_ranking():
    assert "No money-making methods" in render([])


# -- column matching is tolerant, but not so tolerant it reads the wrong column


def test_a_renamed_profit_column_is_still_found():
    """Matched on what the header contains, not the exact string the guide uses
    today. An exact match is one wiki edit away from finding nothing, and
    finding nothing here is silent."""
    for header in ("Hourly profit", "Profit per hour", "Profit/hr", "PROFIT (hourly)"):
        table = [
            ["Method", header, "Skills", "Category"],
            ["Pickpocketing elves", "4,340,000", "Thieving 85", "Skilling/Thieving"],
        ]
        assert [m.gp_per_hour for m in methods_from_table(table)] == [4_340_000], header


def test_a_profit_column_with_no_hour_in_it_is_still_refused():
    """The tolerance must not reach the parent page's Profit-over-Time table.
    484,000 per 25 minutes ranked as an hourly rate is two numbers that mean
    different things sorted against each other."""
    for header in ("Profit", "Total profit", "Profit per trip"):
        table = [
            ["Method", header, "Time", "Skills"],
            ["Brewing", "484,000", "00:25:00", "Cooking 54"],
        ]
        assert methods_from_table(table) == [], header


def test_a_renamed_method_column_is_still_found():
    table = [
        ["Activity", "Hourly profit", "Skills", "Category"],
        ["Pickpocketing elves", "4,340,000", "Thieving 85", "Skilling/Thieving"],
    ]
    assert [m.name for m in methods_from_table(table)] == ["Pickpocketing elves"]
