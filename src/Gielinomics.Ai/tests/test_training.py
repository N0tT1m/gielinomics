"""Training-plan tests. Pure functions over headings and section prose.

The fixtures are real: every heading and every quoted sentence below is copied
from the live Construction training guide, because the parsing this module does
is entirely about the wiki's actual formatting habits -- en dashes, "52/74"
split brackets, headings with and without a colon.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reldo.skills import xp_for_level
from reldo.training import (
    about_material,
    actions_for,
    brackets_for,
    guide_titles,
    header,
    materials_per_action,
    overlaps,
    parse_bracket,
    read_bracket,
    xp_per_action,
    xp_per_hour,
)

# Verbatim from Construction training.
OAK_LARDERS = "Levels 33–52/74: Oak larders"
OAK_LARDER_TEXT = (
    "From level 33 to 52, build oak larders in the Kitchen. Oak larders require "
    "8 oak planks to build, and they grant 480 experience each."
)
MAHOGANY = "Levels 52–99: Mahogany furniture"
MAHOGANY_TEXT = (
    "Mahogany tables in the dining room are the fastest way to use mahogany "
    "planks until level 77. Each mahogany table requires 6 mahogany planks and "
    "gives 840 experience. Players can gain up to around 900,000 experience per "
    "hour with mahogany tables."
)
CAPES = "Levels 50–99: Mounted mythical capes"
CAPES_TEXT = (
    "They require 3 teak planks each along with a mythical cape. Each mounted "
    "mythical cape gives 370 experience, granting more experience per teak plank "
    "than other teak furniture."
)
OAK_DOORS = "Levels 74–99: Oak doors"
OAK_DOORS_TEXT = (
    "Building an oak door requires 10 oak planks and grants 600 Construction "
    "experience."
)
HOMES = "Levels 1–99: Mahogany Homes"
HOMES_TEXT = (
    "Mahogany Homes is a construction minigame in which players repair and build "
    "furniture for residents, using oak, teak and mahogany planks by contract "
    "tier."
)
CRANE = "Levels 30–99 Fishing crane repair"


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Levels 1–33: Starting off", (1, 33)),
        (MAHOGANY, (52, 99)),
        (CRANE, (30, 99)),  # no colon
        ("Levels 1-99: Mahogany Homes", (1, 99)),  # plain hyphen
        ("Level 70—99: something", (70, 99)),  # em dash
    ],
)
def test_parses_the_bracket_forms_the_wiki_actually_uses(heading, expected):
    assert parse_bracket(heading) == expected


def test_split_bracket_takes_the_wider_endpoint():
    """"33-52/74" means this method runs to 52, or to 74 if you keep at it. Read
    narrowly, a plan to 70 silently drops the section that covers 53 upward --
    and oak larders are the cheap route somebody asking about planks wants."""
    assert parse_bracket(OAK_LARDERS) == (33, 74)


@pytest.mark.parametrize(
    "heading", ["General information and tips", "Efficiency", "Prices", "Servants"]
)
def test_sections_without_a_bracket_are_skipped(heading):
    assert parse_bracket(heading) is None


def test_overlap_is_inclusive_at_both_ends():
    assert overlaps((52, 99), 37, 70)
    assert overlaps((33, 74), 37, 70)
    assert overlaps((66, 74), 66, 66)
    assert not overlaps((1, 33), 37, 70)
    assert not overlaps((75, 99), 37, 70)


def test_reads_xp_and_materials_out_of_the_guide_prose():
    assert xp_per_action(OAK_LARDER_TEXT) == 480
    assert materials_per_action(OAK_LARDER_TEXT) == (8, "oak plank")


def test_says_nothing_rather_than_guessing_when_the_guide_states_no_rate():
    text = "Mahogany Homes is a minigame. Contracts vary by tier."
    assert xp_per_action(text) is None
    assert materials_per_action(text) is None


def test_the_plank_question():
    """"How many planks are needed to go from level 37 to 70 Construction?" --
    answered "the wiki does not give the XP per Oak plank", which was true of the
    prose extract and false of the page."""
    leg = read_bracket(OAK_LARDERS, OAK_LARDER_TEXT, 37, 70)
    assert leg is not None
    out = leg.render()
    assert "710,154 XP" in out  # xp_between(37, 70), exact
    assert "1,480 actions" in out  # ceil(710154 / 480)
    assert "11,840 oak planks" in out  # 1,480 x 8
    assert "does not give" not in out


def test_a_bracket_is_clipped_to_the_part_you_need():
    """The guide's own "1,760 planks" is for 33-52. Somebody at 37 does not need
    the four levels below them, and quoting the row whole answers a question
    that was not asked."""
    out = read_bracket(
        MAHOGANY, "Mahogany tables grant 840 experience each.", 37, 70
    ).render()
    assert "[levels 52-70 of this]" in out
    assert f"{xp_for_level(70) - xp_for_level(52):,} XP" in out


def test_brackets_outside_the_range_return_nothing():
    assert read_bracket("Levels 1–33: Starting off", OAK_LARDER_TEXT, 37, 70) is None
    assert read_bracket("General information", OAK_LARDER_TEXT, 37, 70) is None


def test_header_is_exact_and_leads_with_the_gap():
    out = header("Construction", xp_for_level(37), 70)
    assert "level 37" in out
    assert "level 70" in out
    assert "710,154 XP to go" in out


def test_header_handles_xp_that_is_already_past_the_target():
    out = header("Construction", xp_for_level(80), 70)
    assert "already" in out


def test_partial_levels_count_from_the_xp_not_the_level():
    """Somebody at 27,500 XP is level 37 with 27 XP banked. Planning from the
    start of level 37 overstates the gap by that much -- small here, and 300k at
    the top end."""
    out = header("Construction", 500_000, 70)
    assert "237,627 XP to go" in out  # 737,627 - 500,000


def test_actions_round_up():
    assert actions_for(1_000, 300) == 4  # not 3.33, and not 3


def test_guide_titles_try_the_plain_form_first():
    """Construction's guide is "Construction training"; Mining's is
    "Pay-to-play Mining training". Both are live and neither pattern wins."""
    assert guide_titles("Construction")[0] == "Construction training"
    assert "Pay-to-play Mining training" in guide_titles("mining")


def test_header_does_not_print_the_same_number_twice():
    """Sitting exactly on a level threshold, the gap and the from-the-start
    figure are identical, and saying both reads as broken arithmetic."""
    out = header("Construction", xp_for_level(37), 70)
    assert out.count("710,154") == 1
    assert "banked" not in out


def test_header_shows_banked_xp_when_there_is_some():
    out = header("Construction", 500_000, 70)
    assert "banked" in out


def test_single_material_is_not_pluralised_in_the_rate():
    out = read_bracket(
        "Levels 1–99: Something",
        "It requires 1 mahogany plank to build and grants 280 experience each.",
        37,
        70,
    ).render()
    assert "at 1 mahogany plank each" in out
    assert "at 1 mahogany planks each" not in out


# -- reading a count that is really per-action -------------------------------
# The guides state counts of three kinds in one voice: per action, per hour, and
# per level bracket. Only the first is a shopping list, and every case below is
# a sentence the live wiki actually contains.


def test_the_mahogany_question():
    """"How many mahogany planks from 52 to 84?" -- answered in teak, with no
    plank count at all, because the guide phrases this one bracket as "requires
    6 mahogany planks and gives 840 experience" rather than "to build"."""
    out = read_bracket(MAHOGANY, MAHOGANY_TEXT, 52, 84).render()
    assert "2,827,713 XP" in out  # xp_between(52, 84), exact
    assert "3,367 actions" in out  # ceil(2,827,713 / 840)
    assert "20,202 mahogany planks" in out  # 3,367 x 6
    assert "does not state" not in out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Construction, all four phrasings the one guide uses.
        ("Oak larders require 8 oak planks to build, and they grant 480 "
         "experience each.", (8, "oak plank")),
        ("Each mahogany table requires 6 mahogany planks and gives 840 "
         "experience.", (6, "mahogany plank")),
        ("They require 3 teak planks each along with a mythical cape.",
         (3, "teak plank")),
        (OAK_DOORS_TEXT, (10, "oak plank")),
        # Fletching and Crafting, where the count is a word and the verb is not
        # "require" at all.
        ("Fletching one shield requires two redwood logs, and gives 216 "
         "experience per shield.", (2, "redwood log")),
        ("Fletching battlestaves requires one piece of Celastrus bark, and "
         "yields 80 experience per bark.", (1, "celastrus bark")),
        ("Drift nets can be crafted from 2 jute fibre each.", (2, "jute fibre")),
    ],
)
def test_reads_per_action_materials_across_guides(text, expected):
    assert materials_per_action(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # A whole-bracket total in per-action grammar. Multiplied by an action
        # count it asks somebody to buy a billion essence.
        "Training Runecraft from level 91 to level 99 using this method requires "
        "792,400 pure essence and yields 1,584,800 nature runes.",
        # A level requirement, which fits "requires N X to build" exactly and is
        # not N of anything.
        "A bank chest space is available nearby, which requires 70 Construction "
        "to build in order to bank on the island.",
        # Totals announced as totals.
        "This will require a total of 215 leather, for a total cost of 41,925.",
        # A rate, not a cost.
        "This costs 200 numulites per day, or 20,000 numulites for permanent "
        "access.",
        "With high efficiency, players can make around 1,200 repair kits per hour.",
        # Per inventory is not per action either.
        "For this method, 3 giant seaweed and 18 buckets of sand will be needed "
        "per inventory.",
    ],
)
def test_counts_that_are_not_per_action_are_not_read_as_materials(text):
    """None covers "the guide does not say" and "this cannot be sure", and the
    two are deliberately not distinguished: a total mistaken for a per-action
    figure is worse than no figure, because everything downstream multiplies."""
    assert materials_per_action(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The guides state a rate led by a verb, or led by what one action is
        # for. Both forms are per-action and only the first was being read.
        ("Repeatedly casting Camelot Teleport offers around 80,000 experience "
         "per hour, with 55.5 experience per cast.", 55.5),
        ("The best way to get from level 1 to level 5 is by fletching arrow "
         "shafts, which give 5 experience per log.", 5),
        ("Each chest gives 200 experience per successful unlock.", 200),
        ("Each log gives 37.5 Woodcutting experience.", 37.5),
    ],
)
def test_xp_reads_a_rate_stated_as_what_one_action_is_for(text, expected):
    assert xp_per_action(text) == expected


def test_xp_reads_a_rate_stated_with_its_skill_named():
    """Oak doors state their rate exactly once, as "grants 600 Construction
    experience", and a pattern wanting "experience" straight after the number
    loses the whole bracket -- rate, planks and all."""
    assert xp_per_action(OAK_DOORS_TEXT) == 600
    assert read_bracket(OAK_DOORS, OAK_DOORS_TEXT, 74, 84) is not None


@pytest.mark.parametrize(
    "text",
    [
        # Same sentence shape as a per-action figure, three orders of magnitude
        # out. Read as per-action it turns a 2.8M XP plan into twenty actions.
        "Tele-alching at Camelot gives up to 144,600 experience per hour.",
        "Players can gain up to around 480,000 experience per hour.",
        # Quest rewards sit inside the low brackets of several guides.
        "Notably, The Knight's Sword grants 12,725 experience and can be "
        "completed in less than 10 minutes.",
    ],
)
def test_hourly_and_one_off_figures_are_not_per_action_rates(text):
    assert xp_per_action(text) is None


# -- answering in the material that was asked about --------------------------


def test_the_stated_material_decides_not_the_heading():
    """Mounted mythical capes are a teak method under a heading naming neither
    teak nor planks, and mahogany furniture is not a teak method however much
    the two brackets overlap."""
    assert about_material(CAPES, CAPES_TEXT, "teak plank")
    assert not about_material(CAPES, CAPES_TEXT, "mahogany plank")
    assert about_material(MAHOGANY, MAHOGANY_TEXT, "mahogany plank")
    assert not about_material(MAHOGANY, MAHOGANY_TEXT, "teak plank")


def test_a_question_naming_no_material_still_matches_everything():
    """The filter narrows on request and never on its own -- a plan question
    that names no material wants the whole guide, as it always did."""
    assert about_material(CAPES, CAPES_TEXT, "")
    assert about_material(MAHOGANY, MAHOGANY_TEXT, "")


def test_a_bare_material_name_matches_every_material_of_that_kind():
    """"how many planks" names a kind, not a wood. Narrowing that to one wood
    would answer a question the asker was careful not to ask."""
    assert about_material(CAPES, CAPES_TEXT, "plank")
    assert about_material(MAHOGANY, MAHOGANY_TEXT, "plank")


def test_a_section_stating_no_material_is_matched_on_its_prose():
    """Mahogany Homes states no per-action count because it has none -- the
    contract tier decides. It is still a mahogany method, and dropping it would
    lose the one method that spans the whole range."""
    assert about_material(HOMES, HOMES_TEXT, "mahogany plank")
    assert about_material(HOMES, HOMES_TEXT, "teak plank")
    assert not about_material(HOMES, HOMES_TEXT, "yew log")


# -- brackets_for, over a guide that is not the wiki -------------------------


class FakeWiki:
    """The two calls brackets_for makes, over sections given as (heading, text)."""

    def __init__(self, title, sections):
        self._title = title
        self._sections = sections

    async def sections(self, title):
        if title != self._title:
            raise LookupError(title)
        return [
            SimpleNamespace(line=heading, index=str(i))
            for i, (heading, _) in enumerate(self._sections)
        ]

    async def section_text(self, title, index):
        return self._sections[int(index)][1]


def construction_guide():
    return FakeWiki(
        "Construction training",
        [
            ("Levels 33–52/74: Oak larders", OAK_LARDER_TEXT),
            (MAHOGANY, MAHOGANY_TEXT),
            (CAPES, CAPES_TEXT),
            (HOMES, HOMES_TEXT),
        ],
    )


async def test_a_named_material_drops_the_brackets_about_other_materials():
    legs, source = await brackets_for(
        construction_guide(), "Construction", 52, 84, material="mahogany plank"
    )
    assert source == "Construction training"
    assert [leg.heading for leg in legs] == [MAHOGANY, HOMES]
    assert legs[0].materials == 20_202  # 3,367 tables x 6
    assert legs[0].material == "mahogany plank"


async def test_no_material_named_leaves_every_bracket_in_place():
    legs, _ = await brackets_for(construction_guide(), "Construction", 52, 84)
    assert len(legs) == 4


async def test_a_material_the_guide_has_no_bracket_for_returns_nothing():
    """Rather than the unfiltered list. An answer about teak to a question about
    yew logs is the failure the material argument exists to stop, and returning
    nothing sends the question to somebody who might do better with it."""
    legs, source = await brackets_for(
        construction_guide(), "Construction", 52, 84, material="yew log"
    )
    assert legs == []
    assert source == ""


# -- the other rate, the one per hour ----------------------------------------

GRANITE = "Levels 45–99: Granite"
GRANITE_TEXT = (
    "At level 99, the theoretical tick-perfect maximum is approximately 134,000 "
    "experience per hour before accounting for the charged ring's extra-resource "
    "effect. The table uses an efficient long-term benchmark of 126,000 "
    "experience per hour. Experienced players making occasional mistakes can "
    "generally expect approximately 120,000–125,000 experience per hour. Without "
    "tick manipulation, players can gain around 63,000 experience per hour at "
    "best."
)


def test_a_benchmark_rate_beats_a_theoretical_maximum():
    """Four rates in one section, spanning a factor of two, and they are not
    four opinions about one number. The first stated is the tick-perfect
    maximum; planning with it promises a rate almost nobody sustains."""
    found = xp_per_hour(GRANITE_TEXT)
    assert found is not None
    rate, note = found
    assert rate == 126_000
    assert "long-term benchmark" in note


def test_the_sentence_comes_back_with_the_number():
    """A rate is a claim about equipment, attention and tick manipulation, and
    the guide says which in the same breath. Handing the number over alone
    loses the half that says whether it applies to the asker."""
    _, note = xp_per_hour(
        "Without tick manipulation, players can gain around 63,000 experience "
        "per hour at best."
    )
    assert note.startswith("Without tick manipulation")


def test_a_stated_range_reads_as_its_lower_bound():
    """Erring towards the grind being longer is the side to be wrong on when
    somebody is deciding whether to start it."""
    found = xp_per_hour("Players can expect 120,000–125,000 experience per hour.")
    assert found is not None and found[0] == 120_000


def test_a_section_stating_no_hourly_rate_reads_as_none():
    assert xp_per_hour(OAK_LARDER_TEXT.replace("480 experience each", "some xp")) is None


def test_per_action_and_per_hour_do_not_read_each_other():
    """The two patterns are mirrors and both sit in the same prose. Each must
    refuse the other's sentence, or a 2.8M XP plan becomes twenty actions."""
    hourly = "Players can gain up to around 480,000 experience per hour."
    assert xp_per_action(hourly) is None
    assert xp_per_hour(hourly) == (480_000, hourly)

    each = "Oak larders grant 480 experience each."
    assert xp_per_hour(each) is None
    assert xp_per_action(each) == 480


def test_a_method_is_matched_on_the_heading_and_not_the_prose():
    """Four brackets of the Mining guide mention granite -- iron ore says to
    switch to it at 45, crashed stars compares against it -- and exactly one is
    about it. about_material would keep all four."""
    from reldo.training import about_method

    assert about_method(GRANITE, "granite")
    assert not about_method("Levels 15–45/70: Iron ore", "granite")
    assert not about_method("Levels 60–99: Crashed stars", "granite")
    # No method named is every method, as with materials.
    assert about_method("Levels 15–45/70: Iron ore", "")


def test_the_hours_come_off_the_bracket_that_states_the_rate():
    leg = read_bracket(GRANITE, GRANITE_TEXT, 45, 99)
    assert leg.xp_hour == 126_000
    assert round(leg.hours(), 1) == 103.0  # 12,972,919 / 126,000
